"""
Request tracing.

One object accumulates everything a request did, and writes it once at the end.
Both /api/ask and /api/clarify use it, so a clarified answer is as observable as
an ordinary one - previously the clarify path logged nothing at all and those
answers simply vanished.

What gets recorded is chosen to make a regression diagnosable without reading
transcripts. Every flag below corresponds to a failure this build has actually
produced:

    repaired          extraction contradicted itself (fiscal basis + a period)
    coerced           two strict failures, fell back to lenient validation
    sanity_corrected  the model ignored the prompt ("last quarter" -> whole FY)
    template_used     narration hallucinated twice, deterministic text shipped
    unverified        figures in the prose absent from the result rows
    ambiguity_kind    which detector interrupted the user
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from api.db import Database

log = logging.getLogger("tbx.trace")


def jsonable(value: Any) -> Any:
    """Decimal -> str deliberately: money kept exact through NUMERIC should not
    become float64 in transport or in the log."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return value


def new_thread_id() -> str:
    return f"t_{uuid.uuid4().hex[:12]}"


@dataclass
class RequestTrace:
    question: str
    thread_id: str
    resumed: bool = False

    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:12]}")
    _t0: float = field(default_factory=time.perf_counter)

    # what happened
    spec: Any = None
    sql: str | None = None
    row_count: int = 0
    rows_sample: list[list[Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    narration: str = ""
    notes: list[str] = field(default_factory=list)

    # timings
    extract_ms: int = 0
    narrate_ms: int = 0
    sql_ms: int = 0

    # model accounting
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    # correctness + behaviour flags
    verified: bool | None = None
    unverified: list[str] = field(default_factory=list)
    repaired: bool = False
    coerced: bool = False
    sanity_corrected: list[str] = field(default_factory=list)
    template_used: bool = False
    clarified: bool = False
    ambiguity_kind: str | None = None
    confidence: str = "high"
    error: str | None = None

    @property
    def total_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def note(self, text: str) -> None:
        self.notes.append(text)

    async def persist(self, db: Database) -> None:
        """Write query_log + the conversation turns. Never raises - losing a log
        row must not break a response that already succeeded."""
        try:
            await self._write(db)
        except Exception:  # noqa: BLE001
            log.exception("trace persist failed (question=%r)", self.question[:80])

    async def _write(self, db: Database) -> None:
        spec_json = json.dumps(jsonable(self.spec.model_dump())) if self.spec else None
        intent = getattr(self.spec, "intent", None)

        await db.execute(
            """INSERT INTO chat_threads (thread_id) VALUES (%(t)s)
               ON CONFLICT (thread_id) DO NOTHING""",
            {"t": self.thread_id},
        )

        row = await db.fetchone(
            """
            INSERT INTO query_log (
                thread_id, question, spec, sql_text, row_count, sql_ms,
                llm_extract_ms, llm_narrate_ms, input_tokens, output_tokens,
                model, verified, clarified, error, repaired, coerced,
                sanity_corrected, template_used, unverified, ambiguity_kind,
                intent, resumed, total_ms
            ) VALUES (
                %(thread)s, %(q)s, %(spec)s, %(sql)s, %(rows)s, %(sql_ms)s,
                %(ex_ms)s, %(nr_ms)s, %(in_tok)s, %(out_tok)s,
                %(model)s, %(verified)s, %(clarified)s, %(err)s, %(repaired)s,
                %(coerced)s, %(sanity)s, %(template)s, %(unver)s, %(amb)s,
                %(intent)s, %(resumed)s, %(total_ms)s
            ) RETURNING id
            """,
            {
                "thread": self.thread_id, "q": self.question, "spec": spec_json,
                "sql": self.sql, "rows": self.row_count, "sql_ms": self.sql_ms,
                "ex_ms": self.extract_ms, "nr_ms": self.narrate_ms,
                "in_tok": self.input_tokens, "out_tok": self.output_tokens,
                "model": self.model, "verified": self.verified,
                "clarified": self.clarified, "err": self.error,
                "repaired": self.repaired, "coerced": self.coerced,
                "sanity": self.sanity_corrected, "template": self.template_used,
                "unver": self.unverified, "amb": self.ambiguity_kind,
                "intent": intent, "resumed": self.resumed,
                "total_ms": self.total_ms,
            },
        )
        query_log_id = row["id"] if row else None

        # Conversation record. Doubles as the "sample questions and answers"
        # submission deliverable - export it rather than writing it by hand.
        seq = await db.scalar(
            "SELECT COALESCE(MAX(seq), 0) FROM chat_messages WHERE thread_id = %(t)s",
            {"t": self.thread_id},
        ) or 0

        if not self.resumed:
            await db.execute(
                """INSERT INTO chat_messages (message_id, thread_id, seq, role, question)
                   VALUES (%(id)s, %(t)s, %(seq)s, 'user', %(q)s)
                   ON CONFLICT (message_id) DO NOTHING""",
                {"id": f"usr_{uuid.uuid4().hex[:12]}", "t": self.thread_id,
                 "seq": seq + 1, "q": self.question},
            )
            seq += 1

        await db.execute(
            """INSERT INTO chat_messages (
                   message_id, thread_id, seq, role, question, spec, sql_text,
                   result, narration, verified, confidence, latency_ms, query_log_id
               ) VALUES (
                   %(id)s, %(t)s, %(seq)s, 'assistant', %(q)s, %(spec)s, %(sql)s,
                   %(result)s, %(narr)s, %(verified)s, %(conf)s, %(ms)s, %(qid)s
               ) ON CONFLICT (message_id) DO NOTHING""",
            {
                "id": self.message_id, "t": self.thread_id, "seq": seq + 1,
                "q": self.question, "spec": spec_json, "sql": self.sql,
                "result": json.dumps(jsonable({
                    "columns": self.columns,
                    "rows": self.rows_sample[:20],
                    "row_count": self.row_count,
                    "notes": self.notes,
                })),
                "narr": self.narration or None, "verified": self.verified,
                "conf": self.confidence, "ms": self.total_ms, "qid": query_log_id,
            },
        )
