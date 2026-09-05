"""
Operations surface: canary runs, scorecard, incidents, replay.

The scorecard is the point. It is the measured answer to "which model should we
ship?", which is a scored deliverable in the brief - and an answer backed by 50
questions with SQL ground truth is worth more than an assertion.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from api.config import settings
from api.clock import rows_with_instants, with_instants
from api.db import db, json_column
from api.observability import jsonable
from eval.harness import load_questions, run_eval

log = logging.getLogger("tbx.ops")
router = APIRouter()


def _frame(kind: str, payload: dict) -> str:
    return f"data: {json.dumps(jsonable({'type': kind, **payload}))}\n\n"


@router.get("/api/eval/questions")
async def questions():
    qs = load_questions()
    return {
        "total": len(qs),
        "by_grade": {g: sum(1 for q in qs if q["grade"] == g)
                     for g in ("numeric", "spec", "behaviour")},
        "questions": [
            {"id": q["id"], "question": q["question"], "grade": q["grade"],
             "expect": q.get("expect"), "note": q.get("note")}
            for q in qs
        ],
    }


@router.get("/api/models")
async def models():
    """What the canary can be run against.

    The scorecard only means something if a second row can be produced, and the
    row is per MODEL, not per provider - two Ollama models are a real comparison
    without an API key. Served rather than hardcoded in the UI so the picker
    cannot offer something the daemon does not have pulled.
    """
    from api.llm.ollama import list_models

    try:
        available = await list_models()
    except Exception as exc:  # noqa: BLE001 - the picker must still render
        log.warning("could not list ollama models: %s", exc)
        # The configured model is the one thing we know is intended, so the
        # dropdown degrades to it rather than to empty.
        return {"models": [settings.ollama_model],
                "default": settings.ollama_model, "error": str(exc)}

    declared = settings.eval_model_names
    # Declared order wins, but only for models the daemon can actually load.
    # A name in EVAL_MODELS that was never pulled is reported separately rather
    # than dropped: silently shrinking the picker is how a typo in .env becomes
    # ten minutes of wondering where the model went.
    offered = [m for m in declared if m in available]
    missing = [m for m in declared if m not in available]
    if not offered:
        offered = available or [settings.ollama_model]
    return {"models": offered, "default": settings.ollama_model,
            "missing": missing}


@router.post("/api/eval/run")
async def eval_run(request: Request) -> StreamingResponse:
    """Streams each question's verdict. 50 questions is several minutes, so a
    spinner would hide which half is failing."""
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - empty body is fine
        pass

    provider = body.get("provider") or settings.llm_provider
    model_name = body.get("model") or None
    only = body.get("only")
    notes = body.get("notes", "")

    async def stream() -> AsyncIterator[str]:
        # The harness drives this API over HTTP, so the model it measures is
        # whichever one app.state.llm holds - swapping that reference IS how a
        # run targets something other than the default. It is process-wide for
        # the duration, so a chat request arriving mid-run is answered by the
        # model under test. Acceptable on a single-operator ops page, and the
        # reason the swap is put back in `finally` rather than at the end.
        llm = request.app.state.llm
        original_provider = settings.llm_provider
        original_model = settings.ollama_model
        swapped_llm = None

        try:
            # Reject an unpulled name BEFORE running. Without this the request
            # is accepted, all 40 questions fail in milliseconds, and the
            # scorecard gains a 0% row for a model that was never there.
            if model_name and provider == "ollama":
                from api.llm.ollama import list_models

                try:
                    available = await list_models()
                except Exception as exc:  # noqa: BLE001 - daemon down
                    yield _frame("error", {"message": f"Ollama unreachable: {exc}"})
                    return
                if model_name not in available:
                    yield _frame("error", {
                        "message": f"Model {model_name!r} is not pulled. "
                                   f"Available: {', '.join(available) or 'none'}",
                    })
                    return

            # Targeting a different provider OR model is how a second scorecard
            # row gets filled.
            if provider != original_provider or (
                model_name and model_name != original_model
            ):
                from api.llm.base import get_llm

                settings.llm_provider = provider
                if model_name:
                    settings.ollama_model = model_name
                try:
                    swapped_llm = get_llm()
                    request.app.state.llm = swapped_llm
                except Exception as exc:  # noqa: BLE001
                    settings.llm_provider = original_provider
                    settings.ollama_model = original_model
                    swapped_llm = None
                    yield _frame("error", {
                        "message": f"Cannot use provider {provider!r}: {exc}",
                    })
                    return

            model = (settings.ollama_model if settings.llm_provider == "ollama"
                     else settings.anthropic_model)

            async for kind, payload in run_eval(db, "http://localhost:8000",
                                                model, only, notes):
                if kind == "result":
                    yield _frame("result", {
                        "question_id": payload.question_id,
                        "question": payload.question,
                        "grade": payload.grade,
                        "passed": payload.passed,
                        "expected": payload.expected,
                        "actual": payload.actual,
                        "detail": payload.detail,
                        "latency_ms": payload.latency_ms,
                    })
                else:
                    yield _frame(kind, payload)
                await asyncio.sleep(0)
        except Exception as exc:  # noqa: BLE001
            log.exception("eval run failed")
            yield _frame("error", {"message": str(exc)})
        finally:
            if swapped_llm is not None:
                settings.llm_provider = original_provider
                settings.ollama_model = original_model
                request.app.state.llm = llm
                # One httpx client per run would otherwise be left open.
                await swapped_llm.close()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/api/eval/scorecard")
async def scorecard():
    """One row per model - the bake-off table, straight from measurement."""
    rows = await db.fetch("SELECT * FROM v_model_scorecard")
    history = await db.fetch(
        """SELECT run_id, model, started_at, n_total, n_passed, duration_ms
           FROM eval_runs WHERE finished_at IS NOT NULL
           ORDER BY started_at DESC LIMIT 12""")
    return {"models": rows_with_instants(rows),
            "history": rows_with_instants(history)}


@router.get("/api/eval/runs/{run_id}")
async def eval_run_detail(run_id: str):
    run = await db.fetchone("SELECT * FROM eval_runs WHERE run_id = %(r)s", {"r": run_id})
    if not run:
        return {"error": "unknown run"}
    results = await db.fetch(
        """SELECT question_id, question, grade, passed, expected, actual, detail,
                  latency_ms, spec
           FROM eval_results WHERE run_id = %(r)s
           ORDER BY passed ASC, question_id""", {"r": run_id})
    return {"run": with_instants(run), "results": [dict(r) for r in results]}


@router.get("/api/incidents")
async def incidents(limit: int = 25):
    rows = await db.fetch(
        "SELECT * FROM v_incidents LIMIT %(l)s", {"l": min(limit, 100)})
    return {"incidents": rows_with_instants(rows)}


@router.get("/api/replay/{query_log_id}")
async def replay(query_log_id: int, request: Request):
    """Re-extract a logged question and diff the spec.

    This isolates a regression to a layer, which no amount of staring at logs
    does:

        spec same, numbers differ  -> the DATA changed
        spec differs               -> the MODEL or prompt drifted
        spec same, numbers same    -> narration drifted

    It works only because there is a typed intermediate to diff. A text-to-SQL
    design has nothing comparable to compare.
    """
    row = await db.fetchone(
        "SELECT id, question, spec, sql_text, row_count, model, verified "
        "FROM query_log WHERE id = %(i)s", {"i": query_log_id})
    if not row:
        return {"error": "unknown query_log id"}

    from api.compile import compile_query
    from api.extract import extract
    from api.schema import QuerySpec

    reg = request.app.state.registry
    llm = request.app.state.llm

    try:
        fresh_spec, _ = await extract(row["question"], llm, reg)
    except Exception as exc:  # noqa: BLE001
        return {"question": row["question"], "error": f"re-extraction failed: {exc}"}

    old = json_column(row["spec"], default={})
    new = json.loads(fresh_spec.model_dump_json())

    ignore = {"reasoning", "ambiguities"}
    diff = {
        k: {"then": old.get(k), "now": new.get(k)}
        for k in set(old) | set(new)
        if k not in ignore and old.get(k) != new.get(k)
    }

    fresh_rows = fresh_error = None
    try:
        cq = compile_query(QuerySpec.model_validate(new), reg)
        _, rows_now, _ = await db.fetch_timed(cq.sql, cq.params)
        fresh_rows = len(rows_now)
    except Exception as exc:  # noqa: BLE001
        fresh_error = str(exc)

    if diff:
        verdict = "MODEL_DRIFT: the same question now extracts a different spec"
    elif fresh_rows is not None and fresh_rows != row["row_count"]:
        verdict = "DATA_CHANGED: identical spec, different row count"
    elif row["verified"] is False:
        verdict = "NARRATION_DRIFT: spec and data unchanged; the prose failed verification"
    else:
        verdict = "STABLE: reproduces exactly"

    return {
        "query_log_id": query_log_id,
        "question": row["question"],
        "verdict": verdict,
        "spec_diff": diff,
        "row_count": {"then": row["row_count"], "now": fresh_rows},
        "model": {"then": row["model"], "now": getattr(llm, "name", None)},
        "compile_error": fresh_error,
    }
