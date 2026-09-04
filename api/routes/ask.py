"""
POST /api/ask - the main pipeline, streamed as SSE.

Phase 4 scope: understand -> compile -> execute -> emit rows.
Ambiguity resolution (Phase 5) and narration (Phase 6) slot in at the marked
points without changing the event protocol.

Every frame is `data: {json}\\n\\n`; the client discriminates on the `type`
field. No named SSE events, because EventSource cannot POST anyway and the
discriminated union is what the frontend reducer wants.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.compile import CompileError, compile_query
from api.config import settings
from api.db import db
from api.extract import ExtractionFailed, extract
from api.narrate import narrate
from api.resolve.ambiguity import AmbiguityResolver
from api.schema import (
    AskRequest,
    ClarifyRequest,
    DoneEvent,
    ErrorEvent,
    NoteEvent,
    RowsEvent,
    SpecEvent,
    TokenEvent,
    VerifiedEvent,
    SqlEvent,
    StageEvent,
)

log = logging.getLogger("tbx.ask")
router = APIRouter()


def jsonable(value: Any) -> Any:
    """Decimal -> str deliberately: JSON numbers are float64, and money that has
    been kept exact all the way through NUMERIC should not lose that in
    transport. The frontend formats from the string."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return value


def frame(event: BaseModel) -> str:
    return f"data: {json.dumps(jsonable(event.model_dump()))}\n\n"


@router.post("/api/ask")
async def ask(body: AskRequest, request: Request) -> StreamingResponse:
    return StreamingResponse(
        _pipeline(body, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _pipeline(body: AskRequest, request: Request) -> AsyncIterator[str]:
    t_start = time.perf_counter()
    app = request.app
    reg = app.state.registry
    llm = app.state.llm
    message_id = f"msg_{uuid.uuid4().hex[:12]}"

    spec = None
    cq = None
    extract_ms = 0
    sql_ms = 0
    row_count = 0
    clarified = False
    error: str | None = None

    try:
        # ---- 1. understand (LLM call #1) --------------------------------
        yield frame(StageEvent(stage="understanding"))
        spec, llm_result = await extract(body.question, llm, reg)
        extract_ms = llm_result.latency_ms
        yield frame(SpecEvent(spec=spec))

        # ---- 2. check ---------------------------------------------------
        yield frame(StageEvent(stage="checking"))

        if spec.intent == "unsupported":
            yield frame(NoteEvent(
                kind="coverage",
                text="That isn't answerable from this data. It holds vendor "
                     "payments, reconciliation status, chart of accounts, "
                     "vendors, departments and funds — no employee, payroll, "
                     "budget or forecast data.",
            ))
            yield frame(DoneEvent(message_id=message_id, confidence="high"))
            return

        # Coverage guardrail: outside the window is "no data", never $0.00.
        if spec.period is not None and not reg.covers(spec.period.start, spec.period.end):
            yield frame(NoteEvent(
                kind="coverage",
                text=f"No data for {spec.period.label or 'that period'}. "
                     f"Coverage runs {reg.earliest} to {reg.latest}.",
            ))
            yield frame(DoneEvent(message_id=message_id, confidence="high"))
            return

        if spec.period is not None:
            frac = reg.coverage_fraction(spec.period.start, spec.period.end)
            if frac < 0.99:
                yield frame(NoteEvent(
                    kind="coverage",
                    text=f"Partial coverage: the data spans about {frac:.0%} of "
                         f"{spec.period.label or 'that period'}.",
                ))

        # ---- ambiguity: detect -> probe -> decide (AMBIGUITY.md) ---------
        resolver = AmbiguityResolver(db, reg)
        decision = await resolver.resolve(
            spec, body.question, settled=await _load_settled(body.thread_id))
        spec = decision.spec

        if decision.needs_user:
            clarified = True
            request.app.state.pending[decision.clarify.ambiguity_id] = decision.clarify
            yield frame(decision.clarify.to_event())
            yield frame(DoneEvent(message_id=message_id, confidence="medium"))
            return

        for note in decision.notes:
            yield frame(note)

        # ---- 3. compile + execute ---------------------------------------
        async for chunk, meta in _execute(
            spec, reg, message_id, llm=llm, question=body.question,
            notes=decision.notes,
        ):
            if meta:
                cq, sql_ms, row_count = meta
            yield chunk

    except ExtractionFailed as exc:
        error = str(exc)
        log.warning("extraction failed: %s | raw=%s", exc, exc.raw[:300])
        yield frame(ErrorEvent(
            code="extraction_failed",
            message="I couldn't turn that into a query I can run. Try rephrasing?",
        ))
    except CompileError as exc:
        error = str(exc)
        log.warning("compile failed: %s", exc)
        yield frame(ErrorEvent(code="compile_failed", message=str(exc)))
    except Exception as exc:  # noqa: BLE001 - the stream must always terminate
        error = f"{type(exc).__name__}: {exc}"
        log.exception("pipeline error")
        yield frame(ErrorEvent(
            code="internal", message="Something went wrong running that query.",
            recoverable=False,
        ))
    finally:
        total_ms = int((time.perf_counter() - t_start) * 1000)
        try:
            await db.execute(
                """INSERT INTO query_log
                   (thread_id, question, spec, sql_text, row_count, sql_ms,
                    llm_extract_ms, model, error, clarified)
                   VALUES (%(thread)s, %(q)s, %(spec)s, %(sql)s, %(rows)s,
                           %(sql_ms)s, %(ex_ms)s, %(model)s, %(err)s, %(clarified)s)""",
                {
                    "thread": body.thread_id,
                    "q": body.question,
                    "spec": json.dumps(jsonable(spec.model_dump())) if spec else None,
                    "sql": cq.sql if cq else None,
                    "rows": row_count,
                    "sql_ms": sql_ms,
                    "ex_ms": extract_ms,
                    "model": getattr(llm, "name", None),
                    "err": error,
                    "clarified": clarified,
                },
            )
        except Exception:  # noqa: BLE001 - logging must never break the response
            log.exception("query_log insert failed")
        log.info("ask done in %sms (extract %s, sql %s, rows %s)",
                 total_ms, extract_ms, sql_ms, row_count)


async def _execute(spec, reg, message_id, llm=None, question="", notes=()):
    """Compile, run, narrate, verify, emit. Shared by /api/ask and /api/clarify
    so a clarified answer takes exactly the same path as an unambiguous one."""
    yield frame(StageEvent(stage="querying")), None

    cq = compile_query(spec, reg)
    yield frame(SqlEvent(sql=cq.sql, params=jsonable(cq.params))), None

    columns, rows, sql_ms = await db.fetch_timed(cq.sql, cq.params)
    row_count = len(rows)

    yield frame(RowsEvent(
        result_id=f"res_{uuid.uuid4().hex[:12]}",
        columns=columns,
        rows=jsonable(rows[: settings.max_rows_to_client]),
        row_count=row_count,
        elapsed_ms=sql_ms,
        truncated=row_count > settings.max_rows_to_client,
    )), (cq, sql_ms, row_count)

    # ---- narrate + verify -------------------------------------------------
    confidence = "high"
    if llm is not None:
        yield frame(StageEvent(stage="explaining")), None
        text, verdict, _ms = await narrate(
            question, spec, columns, rows, llm, [n.text for n in notes]
        )
        # Emitted in chunks for the typing effect. The text is already verified -
        # nothing unchecked ever reaches the client.
        for i in range(0, len(text), 24):
            yield frame(TokenEvent(text=text[i:i + 24])), None
        yield frame(VerifiedEvent(
            ok=verdict.ok,
            numbers_checked=verdict.numbers_checked,
            unverified=verdict.unverified,
        )), None
        if not verdict.ok:
            confidence = "low"
            yield frame(NoteEvent(
                kind="assumption",
                text="Some figures could not be traced to the query result, so "
                     "this answer was replaced with a summary built directly "
                     "from the data.",
            )), None

    yield frame(DoneEvent(message_id=message_id, confidence=confidence)), None


@router.post("/api/clarify")
async def clarify(body: ClarifyRequest, request: Request) -> StreamingResponse:
    """Resume an ambiguous question with the interpretation the user picked.
    No new extraction call - the specs were already built and probed."""
    return StreamingResponse(
        _clarify_pipeline(body, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _clarify_pipeline(body: ClarifyRequest, request: Request):
    app = request.app
    reg = app.state.registry
    message_id = f"msg_{uuid.uuid4().hex[:12]}"

    amb = app.state.pending.get(body.ambiguity_id)
    if amb is None:
        yield frame(ErrorEvent(
            code="unknown_ambiguity",
            message="That question has expired. Please ask it again.",
        ))
        return

    chosen = amb.by_key(body.chosen_key)
    if chosen is None:
        yield frame(ErrorEvent(code="unknown_option",
                               message=f"Unknown option {body.chosen_key!r}."))
        return

    # Remember the choice so the same trigger is not asked twice this session.
    if body.thread_id:
        await _settle(body.thread_id, amb.kind, amb.trigger, body.chosen_key)

    yield frame(SpecEvent(spec=chosen.spec))
    yield frame(NoteEvent(kind="assumption",
                          text=f"Using: {chosen.label} ({chosen.detail})."))
    try:
        async for chunk, _meta in _execute(
            chosen.spec, reg, message_id, llm=app.state.llm,
            question=f'{chosen.label}',
        ):
            yield chunk
    except CompileError as exc:
        yield frame(ErrorEvent(code="compile_failed", message=str(exc)))


async def _settle(thread_id: str, kind: str, trigger: str, key: str) -> None:
    """Sticky resolution, scoped to (kind, trigger) so settling one vendor does
    not silently settle a different one."""
    try:
        await db.execute(
            """INSERT INTO chat_threads (thread_id, settled)
               VALUES (%(t)s, %(s)s::jsonb)
               ON CONFLICT (thread_id) DO UPDATE
               SET settled = chat_threads.settled || EXCLUDED.settled""",
            {"t": thread_id, "s": json.dumps({f"{kind}:{trigger}": key})},
        )
    except Exception:  # noqa: BLE001
        log.exception("could not persist clarification choice")


async def _load_settled(thread_id: str | None) -> dict:
    if not thread_id:
        return {}
    row = await db.fetchone(
        "SELECT settled FROM chat_threads WHERE thread_id = %(t)s", {"t": thread_id})
    return (row or {}).get("settled") or {}
