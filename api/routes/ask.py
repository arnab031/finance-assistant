"""
POST /api/ask and /api/clarify - the pipeline, streamed as SSE.

Every frame is `data: {json}\\n\\n`; the client discriminates on the `type`
field. No named SSE events, because EventSource cannot POST anyway and the
discriminated union is what the frontend reducer wants.

Both entry points share `_execute` and one `RequestTrace`, so a clarified answer
is compiled, verified and logged exactly like an ordinary one.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.compile import CompileError, compile_query
from api.config import settings
from api.crypto import assert_no_plaintext, placeholderise, resolve_rows
from api.db import db, json_column
from api.extract import ExtractionFailed, extract
from api.llm.ollama import ModelUnavailable
from api.narrate import narrate, template_answer
from api.observability import RequestTrace, jsonable, new_thread_id
from api.profiles.base import get_profile
from api.resolve.ambiguity import AmbiguityResolver
from api.schema import (
    AskRequest,
    ClarifyRequest,
    DoneEvent,
    ErrorEvent,
    NoteEvent,
    RowsEvent,
    SpecEvent,
    SqlEvent,
    StageEvent,
    ThreadEvent,
    TokenEvent,
    VerifiedEvent,
)

log = logging.getLogger("tbx.ask")
router = APIRouter()


def _done(trace: RequestTrace, confidence: str) -> DoneEvent:
    """Every exit reports its timings, not just the happy path: a decline that
    took four seconds of model time should say so as plainly as an answer."""
    return DoneEvent(
        message_id=trace.message_id,
        confidence=confidence,
        total_ms=trace.total_ms,
        extract_ms=trace.extract_ms,
        sql_ms=trace.sql_ms,
        narrate_ms=trace.narrate_ms,
    )


def frame(event: BaseModel) -> str:
    return f"data: {json.dumps(jsonable(event.model_dump()))}\n\n"


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# --------------------------------------------------------------------------
# /api/ask
# --------------------------------------------------------------------------


def _llm_for(request: Request, model: str | None):
    """The app's own client unless the caller named a different model.

    The name is CHECKED against EVAL_MODELS rather than passed through. An
    unchecked string would let a request name any model, and Ollama answers an
    unpulled name with a 404 that fails every stage - so this is the same closed
    set the compiler gives dimensions, applied to model choice.
    """
    default = request.app.state.llm
    if not model or model == settings.ollama_model:
        return default
    # Per-model clients are an Ollama concept; other providers keep the default.
    if settings.llm_provider != "ollama" or model not in settings.eval_model_names:
        log.info("ignoring unknown model %r, using the default", model)
        return default

    from api.llm.base import get_llm_for

    return get_llm_for(model)


@router.post("/api/ask")
async def ask(body: AskRequest, request: Request) -> StreamingResponse:
    return StreamingResponse(_pipeline(body, request),
                             media_type="text/event-stream", headers=SSE_HEADERS)


async def _pipeline(body: AskRequest, request: Request) -> AsyncIterator[str]:
    reg = request.app.state.registry
    llm = _llm_for(request, body.model)

    # A thread is always created. Without one the ambiguity resolver cannot load
    # settled choices, so a client that omits it silently loses stickiness -
    # which is exactly what happened to 106 of the first 132 logged requests.
    trace = RequestTrace(question=body.question,
                         thread_id=body.thread_id or new_thread_id())
    trace.model = getattr(llm, "name", None)
    yield frame(ThreadEvent(thread_id=trace.thread_id))

    try:
        # ---- 1. understand (LLM call #1) --------------------------------
        yield frame(StageEvent(stage="understanding"))
        spec, llm_result = await extract(body.question, llm, reg)
        trace.spec = spec
        trace.extract_ms = llm_result.latency_ms
        trace.input_tokens += llm_result.input_tokens
        trace.output_tokens += llm_result.output_tokens
        trace.repaired = getattr(llm_result, "repaired", False)
        trace.coerced = getattr(llm_result, "coerced", False)
        trace.sanity_corrected = [
            a for a in spec.ambiguities if "Read \"" in a or "Ignored a status" in a
        ]
        yield frame(SpecEvent(spec=spec))

        # ---- 2. check ---------------------------------------------------
        yield frame(StageEvent(stage="checking"))

        if spec.intent == "unsupported":
            # Read from the ACTIVE PROFILE. This was hardcoded to the
            # vendor_payments wording, so every decline on the bank dataset told
            # the user it holds "vendor payments, chart of accounts, departments
            # and funds" - a description of a different database entirely. The
            # canary never caught it because it grades the intent, not the text.
            prof = get_profile()
            # When the absent-concept guard fired it knows WHICH domain is
            # missing, which is more useful than the generic list.
            specific = {reason for _, reason in prof.absent_concepts}
            text = (spec.reasoning if spec.reasoning in specific
                    else prof.unsupported_note)
            trace.note(text)
            yield frame(NoteEvent(kind="coverage", text=text))
            yield frame(_done(trace, "high"))
            return

        if spec.period is not None and not reg.covers(spec.period.start, spec.period.end):
            text = (f"No data for {spec.period.label or 'that period'}. "
                    f"Coverage runs {reg.earliest} to {reg.latest}.")
            trace.note(text)
            yield frame(NoteEvent(kind="coverage", text=text))
            yield frame(_done(trace, "high"))
            return

        if spec.period is not None:
            frac = reg.coverage_fraction(spec.period.start, spec.period.end)
            if frac < 0.99:
                text = (f"Partial coverage: the data spans about {frac:.0%} of "
                        f"{spec.period.label or 'that period'}.")
                trace.note(text)
                yield frame(NoteEvent(kind="coverage", text=text))

        # ---- 3. ambiguity: detect -> probe -> decide ---------------------
        resolver = AmbiguityResolver(db, reg)
        decision = await resolver.resolve(
            spec, body.question, settled=await _load_settled(trace.thread_id))
        before = spec
        spec = trace.spec = decision.spec

        # Re-emit when resolution rewrote the spec. The first SpecEvent is sent
        # before the resolver runs, so the provenance panel was showing the
        # PRE-resolution spec while the SQL below ran the resolved one - it read
        # "gross_amount" next to a query summing signed_amount. The panel is the
        # trust surface; a metric there that was not the one executed is worse
        # than no panel. Persistence was always correct (trace.spec), only the
        # stream was stale.
        if spec != before:
            yield frame(SpecEvent(spec=spec))

        if decision.needs_user:
            trace.clarified = True
            trace.ambiguity_kind = decision.clarify.kind
            trace.confidence = "medium"
            request.app.state.pending[decision.clarify.ambiguity_id] = decision.clarify
            yield frame(decision.clarify.to_event())
            yield frame(_done(trace, "medium"))
            return

        for note in decision.notes:
            trace.note(note.text)
            yield frame(note)

        # ---- 4. compile, execute, narrate, verify ------------------------
        async for chunk in _execute(spec, reg, trace, llm, body.question):
            yield chunk

    except ExtractionFailed as exc:
        trace.error = str(exc)
        log.warning("extraction failed: %s | raw=%s", exc, exc.raw[:300])
        yield frame(ErrorEvent(code="extraction_failed",
                               message="I couldn't turn that into a query I can run. "
                                       "Try rephrasing?"))
    except ModelUnavailable as exc:
        trace.error = str(exc)
        log.error("%s", exc)
        yield frame(ErrorEvent(code="model_unavailable", message=str(exc),
                               recoverable=False))
    except CompileError as exc:
        trace.error = str(exc)
        log.warning("compile failed: %s", exc)
        yield frame(ErrorEvent(code="compile_failed", message=str(exc)))
    except Exception as exc:  # noqa: BLE001 - the stream must always terminate
        trace.error = f"{type(exc).__name__}: {exc}"
        log.exception("pipeline error")
        yield frame(ErrorEvent(code="internal",
                               message="Something went wrong running that query.",
                               recoverable=False))
    finally:
        await trace.persist(db)
        log.info("ask %sms (extract %s, sql %s, narrate %s, rows %s, verified %s)",
                 trace.total_ms, trace.extract_ms, trace.sql_ms,
                 trace.narrate_ms, trace.row_count, trace.verified)


# --------------------------------------------------------------------------
# /api/clarify
# --------------------------------------------------------------------------


@router.post("/api/clarify")
async def clarify(body: ClarifyRequest, request: Request) -> StreamingResponse:
    """Resume an ambiguous question with the reading the user picked.
    No new extraction call - the specs were built and probed already."""
    return StreamingResponse(_clarify_pipeline(body, request),
                             media_type="text/event-stream", headers=SSE_HEADERS)


async def _clarify_pipeline(body: ClarifyRequest, request: Request) -> AsyncIterator[str]:
    reg = request.app.state.registry
    llm = _llm_for(request, body.model)
    amb = request.app.state.pending.get(body.ambiguity_id)

    if amb is None:
        yield frame(ErrorEvent(code="unknown_ambiguity",
                               message="That question has expired. Please ask it again."))
        return

    chosen = amb.by_key(body.chosen_key)
    if chosen is None:
        yield frame(ErrorEvent(code="unknown_option",
                               message=f"Unknown option {body.chosen_key!r}."))
        return

    trace = RequestTrace(question=f"[{amb.trigger}] {chosen.label}",
                         thread_id=body.thread_id or new_thread_id(),
                         resumed=True)
    trace.model = getattr(llm, "name", None)
    trace.spec = chosen.spec
    trace.ambiguity_kind = amb.kind

    try:
        await _settle(trace.thread_id, amb.kind, amb.trigger, body.chosen_key)

        yield frame(SpecEvent(spec=chosen.spec))
        note = f"Using: {chosen.label} ({chosen.detail})."
        trace.note(note)
        yield frame(NoteEvent(kind="assumption", text=note))

        async for chunk in _execute(chosen.spec, reg, trace, llm, chosen.label):
            yield chunk
    except CompileError as exc:
        trace.error = str(exc)
        yield frame(ErrorEvent(code="compile_failed", message=str(exc)))
    except Exception as exc:  # noqa: BLE001
        trace.error = f"{type(exc).__name__}: {exc}"
        log.exception("clarify pipeline error")
        yield frame(ErrorEvent(code="internal",
                               message="Something went wrong running that query.",
                               recoverable=False))
    finally:
        await trace.persist(db)


# --------------------------------------------------------------------------
# Shared tail
# --------------------------------------------------------------------------


def _is_empty_aggregate(columns, rows) -> bool:
    """One row whose every measure is NULL or a zero count = nothing matched.

    Not `len(rows) == 0`: SUM() over an empty set yields a row, not no rows.
    A genuine zero-valued result keeps a non-zero count, so it is not caught.
    """
    if len(rows) != 1:
        return False
    row = dict(zip(columns, rows[0]))
    if "value" not in row:
        return False
    count = row.get("txn_count")
    return row["value"] is None and (count is None or count == 0)


async def _execute(spec, reg, trace: RequestTrace, llm, question: str) -> AsyncIterator[str]:
    """Compile, run, narrate, verify, emit. Shared so a clarified answer takes
    exactly the same path - and is traced identically - to an unambiguous one."""
    yield frame(StageEvent(stage="querying"))

    cq = compile_query(spec, reg)
    trace.sql = cq.sql
    yield frame(SqlEvent(sql=cq.sql, params=jsonable(cq.params)))

    columns, raw_rows, sql_ms = await db.fetch_timed(cq.sql, cq.params)

    # An ungrouped aggregate over NOTHING still returns one row - (NULL, 0) -
    # so every `if not rows` check in this codebase is blind to it, and the
    # model reads that row as the number zero. Measured, before this note
    # existed: "The sum of all transactions regarding the account number
    # 30123456789012 is Rs 0." Verified, high confidence, green badge - and
    # false. Zero means "these netted to nothing"; the truth was "no such
    # transactions". Said plainly here rather than left to the narrator.
    #
    # The wording is deliberately the SAME whether the account exists with no
    # activity or does not exist at all. Distinguishing them would be a kinder
    # message and an account-number oracle: this endpoint is unauthenticated,
    # so a nicer error would let anyone walk the number space and learn which
    # accounts are real.
    if _is_empty_aggregate(columns, raw_rows):
        # Collapsed to no rows, not merely annotated. Left in place the row
        # reaches the breakdown table as "— | 0" under a "1 row" caption, the
        # narrator reads a 0 it can legitimately quote, and trace.row_count = 1
        # so v_health never classes it as an empty result. Dropping it makes
        # every one of those agree with the note.
        raw_rows = []
        text = ("No transactions match that. The filters are valid - the data "
                "simply holds nothing for them, which is not the same as a "
                "total of zero.")
        trace.note(text)
        yield frame(NoteEvent(kind="coverage", text=text))

    # Decrypt, then immediately swap the plaintext for a placeholder. Everything
    # downstream - the model, the trace, the stored message - works in
    # placeholder space; real values are substituted only at the final render.
    rows, pmap = placeholderise(columns, raw_rows)

    # The trace is persisted, so it must hold placeholders, not plaintext.
    trace.columns, trace.rows_sample = columns, jsonable(rows[:20])
    trace.row_count, trace.sql_ms = len(rows), sql_ms

    # The USER may see real values; this is their own data.
    yield frame(RowsEvent(
        result_id=f"res_{uuid.uuid4().hex[:12]}",
        columns=columns,
        rows=jsonable(resolve_rows(columns, rows, pmap)[: settings.max_rows_to_client]),
        row_count=len(rows),
        elapsed_ms=sql_ms,
        truncated=len(rows) > settings.max_rows_to_client,
    ))

    # ---- narrate + verify ------------------------------------------------
    yield frame(StageEvent(stage="explaining"))
    # The MODEL may not. Asserted rather than assumed: a mask that silently
    # stops working looks entirely normal until someone reads the output.
    assert_no_plaintext(rows, pmap, "the narration prompt")
    text, verdict, narrate_ms = await narrate(
        question, spec, columns, rows, llm, trace.notes, pmap=pmap)

    trace.narration = text          # placeholder form; never persist plaintext
    trace.narrate_ms = narrate_ms
    trace.verified = verdict.ok
    trace.unverified = verdict.unverified
    trace.template_used = text == template_answer(question, columns, rows)

    # Final render: substitute real values, AFTER verification has run.
    display = pmap.resolve(text) if pmap else text
    for i in range(0, len(display), 24):
        yield frame(TokenEvent(text=display[i:i + 24]))

    yield frame(VerifiedEvent(ok=verdict.ok,
                              numbers_checked=verdict.numbers_checked,
                              unverified=verdict.unverified))

    if not verdict.ok:
        trace.confidence = "low"
        msg = ("Some figures could not be traced to the query result, so this "
               "answer was replaced with a summary built directly from the data.")
        trace.note(msg)
        yield frame(NoteEvent(kind="assumption", text=msg))
    elif trace.template_used:
        trace.confidence = "medium"

    yield frame(_done(trace, trace.confidence))


async def _settle(thread_id: str, kind: str, trigger: str, key: str) -> None:
    """Sticky resolution, scoped to (kind, trigger) so settling one vendor does
    not silently settle a different one."""
    try:
        await db.execute(
            """INSERT INTO chat_threads (thread_id, settled)
               VALUES (%(t)s, CAST(%(s)s AS JSON))
               ON DUPLICATE KEY UPDATE
                   settled = JSON_MERGE_PATCH(settled, CAST(%(s)s AS JSON))""",
            {"t": thread_id, "s": json.dumps({f"{kind}:{trigger}": key})},
        )
    except Exception:  # noqa: BLE001
        log.exception("could not persist clarification choice")


async def _load_settled(thread_id: str | None) -> dict:
    if not thread_id:
        return {}
    row = await db.fetchone(
        "SELECT settled FROM chat_threads WHERE thread_id = %(t)s", {"t": thread_id})
    settled = json_column((row or {}).get("settled"), default={})
    return settled if isinstance(settled, dict) else {}
