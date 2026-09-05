"""
The canary: run every golden question and check the answer against SQL.

This is the only thing in the system that answers "is the number RIGHT?".
Everything in /api/metrics is a proxy - useful, but it cannot tell a confident
wrong answer from a confident right one. Ground truth can.

Ground truth is recomputed from `truth_sql` on every run rather than stored as a
literal, so the set stays valid when the dataset is swapped.

    ./.venv/bin/python -m eval.harness                 # run against the API
    ./.venv/bin/python -m eval.harness --model haiku   # bake-off comparison
    ./.venv/bin/python -m eval.harness --only q041,q042
"""

from __future__ import annotations

import json
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, AsyncIterator

import httpx
import yaml

from api.db import Database

EVAL_DIR = pathlib.Path(__file__).parent


def questions_path() -> pathlib.Path:
    """questions.<profile>.yaml if it exists, else the default set.

    The two datasets ask genuinely different questions - one has vendors and
    reconciliation, the other has credits and debits - so a shared file would be
    mostly inapplicable to whichever profile is active.
    """
    from api.profiles.base import get_profile

    specific = EVAL_DIR / f"questions.{get_profile().name}.yaml"
    return specific if specific.exists() else EVAL_DIR / "questions.yaml"

# A cent. Money is NUMERIC end to end, so anything looser would hide a real bug.
TOLERANCE = Decimal("0.01")


def load_questions(only: list[str] | None = None) -> list[dict]:
    qs = yaml.safe_load(questions_path().read_text())
    return [q for q in qs if not only or q["id"] in only]


@dataclass
class Result:
    question_id: str
    question: str
    grade: str
    passed: bool
    expected: str = ""
    actual: str = ""
    detail: str = ""
    spec: dict | None = None
    latency_ms: int = 0


@dataclass
class Answer:
    """Everything the SSE stream told us about one question."""
    spec: dict | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    clarify_kind: str | None = None
    clarified: bool = False
    error: str | None = None
    latency_ms: int = 0

    def values(self) -> list[Decimal]:
        if "value" not in self.columns:
            return []
        i = self.columns.index("value")
        out = []
        for r in self.rows:
            try:
                out.append(Decimal(str(r[i])))
            except Exception:  # noqa: BLE001
                pass
        return out


async def ask(client: httpx.AsyncClient, api: str, question: str) -> Answer:
    a = Answer()
    t0 = time.perf_counter()
    async with client.stream("POST", f"{api}/api/ask",
                             json={"question": question}) as r:
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            e = json.loads(line[6:])
            t = e["type"]
            if t == "spec":
                a.spec = e["spec"]
            elif t == "rows":
                a.columns, a.rows = e["columns"], e["rows"]
            elif t == "note":
                a.notes.append(e["text"])
            elif t == "clarify":
                a.clarified, a.clarify_kind = True, e["kind"]
            elif t == "error":
                a.error = e["message"]
    a.latency_ms = int((time.perf_counter() - t0) * 1000)
    return a


# ---------------------------------------------------------------- graders


def _fmt(v: Any) -> str:
    return f"{v:,.2f}" if isinstance(v, Decimal) else str(v)


async def grade_numeric(q: dict, a: Answer, db: Database) -> Result:
    base = Result(q["id"], q["question"], "numeric", False,
                  spec=a.spec, latency_ms=a.latency_ms)

    truth_raw = await db.scalar(q["truth_sql"])
    if truth_raw is None:
        base.detail = "truth_sql returned NULL"
        return base
    truth = Decimal(str(truth_raw))
    base.expected = _fmt(truth)

    if a.error:
        base.actual, base.detail = "error", a.error
        return base
    if a.clarified:
        base.actual, base.detail = "clarified", f"asked about {a.clarify_kind}"
        return base

    mode = q.get("compare", "first")
    values = a.values()
    if mode == "rows":
        got = Decimal(len(a.rows))
    elif mode == "sum":
        got = sum(values, Decimal(0))
    else:
        if not values:
            base.actual, base.detail = "no rows", "assistant returned no value"
            return base
        got = values[0]

    base.actual = _fmt(got)
    base.passed = abs(got - truth) <= TOLERANCE
    if not base.passed:
        base.detail = f"off by {abs(got - truth):,.2f}"
    return base


def grade_spec(q: dict, a: Answer) -> Result:
    base = Result(q["id"], q["question"], "spec", False,
                  spec=a.spec, latency_ms=a.latency_ms)
    want = q["expect_spec"]
    base.expected = json.dumps(want, separators=(",", ":"))

    if a.spec is None:
        base.actual, base.detail = "no spec", a.error or "no spec emitted"
        return base

    flat = dict(a.spec)
    flat.update({k: v for k, v in (a.spec.get("filters") or {}).items()})

    mismatches = []
    got_view = {}
    for key, expected in want.items():
        got = flat.get(key)
        if key == "fiscal_year" and got is not None:
            got = str(got)
        got_view[key] = got
        if isinstance(expected, list):
            ok = list(got or []) == expected
        elif expected is None:
            ok = got in (None, [], "")
        else:
            ok = got == expected
        if not ok:
            mismatches.append(f"{key}={got!r} != {expected!r}")

    base.actual = json.dumps(got_view, separators=(",", ":"), default=str)
    base.passed = not mismatches
    base.detail = "; ".join(mismatches)
    return base


def grade_behaviour(q: dict, a: Answer) -> Result:
    want = q["expect"]
    base = Result(q["id"], q["question"], "behaviour", False,
                  expected=want, spec=a.spec, latency_ms=a.latency_ms)
    notes = " ".join(a.notes).lower()
    intent = (a.spec or {}).get("intent")

    if want == "must_clarify":
        base.actual = f"clarify:{a.clarify_kind}" if a.clarified else (
            "answered" if a.rows else f"intent:{intent}")
        base.passed = a.clarified
        if a.clarified and q.get("expect_kind"):
            base.passed = a.clarify_kind == q["expect_kind"]
            if not base.passed:
                base.detail = f"asked about {a.clarify_kind}, wanted {q['expect_kind']}"
        elif not a.clarified:
            base.detail = "answered without asking"

    elif want == "must_refuse_no_data":
        refused = "no data" in notes and not a.rows
        base.actual = "refused" if refused else ("answered" if a.rows else "other")
        base.passed = refused
        if a.rows:
            base.detail = "returned rows for a period outside coverage"

    elif want == "must_report_no_transactions":
        # Neither existing kind fits. must_refuse_no_data needs "no data" in the
        # notes AND zero rows; an ungrouped aggregate over nothing returns ONE
        # row of (NULL, 0), so both halves are false. Numeric grading is worse:
        # the honest result is NULL, and COALESCE-ing it to 0 would make the
        # canary go green on "you spent Rs 0" - the dishonest answer this
        # question exists to catch.
        #
        # Both halves are required. The note alone would pass even if the filter
        # never applied (an empty database says the same thing), so the spec is
        # checked too.
        told = "no transactions match" in notes
        # ANY populated filter, not only account_numbers: "how much did we pay
        # Amazon" is the same shape - a valid filter that matches nothing - and
        # deserves the same grade. Booleans are excluded so the *_exclusive
        # flags, which are always present, cannot count as a filter.
        spec = a.spec or {}
        filters = spec.get("filters") or {}
        # A period is scope too: "spend in February 2026" (in coverage, no rows)
        # narrows the data exactly as a filter does, and the pipeline's own
        # _has_scope counts it. Without this the honest answer was ungradeable.
        filtered = any(v not in (None, [], "", False) and not isinstance(v, bool)
                       for v in filters.values()) \
            or spec.get("period") is not None or bool(spec.get("fiscal_year"))
        base.actual = ("reported-none" if told else "silent") + (
            "/filtered" if filtered else "/UNFILTERED")
        base.passed = told and filtered
        if not told:
            base.detail = "did not say the result was empty - a bare 0 reads as real"
        elif not filtered:
            base.detail = "no filter in the spec; an empty answer was not scoped to anything"

    elif want == "must_be_unsupported":
        base.actual = f"intent:{intent}" if intent else "no spec"
        base.passed = intent == "unsupported" and not a.rows
        if not base.passed:
            base.detail = "should have declined; the schema has no such data"

    else:
        base.detail = f"unknown expectation {want!r}"
    return base


async def grade(q: dict, a: Answer, db: Database) -> Result:
    if q["grade"] == "numeric":
        return await grade_numeric(q, a, db)
    if q["grade"] == "spec":
        return grade_spec(q, a)
    return grade_behaviour(q, a)


# ---------------------------------------------------------------- runner


async def preflight(api: str, model: str) -> str | None:
    """Return a reason the run cannot proceed, or None.

    A canary against a model that was never pulled fails all 50 questions in
    about two seconds and records a 0% row. That row is worse than no row: read
    off the scorecard it says the model performs terribly, when in fact it was
    never called. Refusing to start keeps the bake-off table honest.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            health = (await c.get(f"{api}/api/health")).json()
    except Exception as exc:  # noqa: BLE001
        return f"backend unreachable at {api}: {exc}"

    ollama = health.get("checks", {}).get("ollama", {})
    if not ollama.get("ok", True):
        installed = ", ".join(ollama.get("models", [])) or "none"
        want = ollama.get("want", model)
        return (f"model {want!r} is not installed (have: {installed}). "
                f"Run: ollama pull {want}")

    if not health.get("checks", {}).get("database", {}).get("ok"):
        return "database is not reachable"
    return None


async def run_eval(
    db: Database, api: str, model: str, only: list[str] | None = None,
    notes: str = "",
) -> AsyncIterator[tuple[str, Any]]:
    """Yields ('result', Result) per question, then ('done', summary).

    Streamed so the UI can tick through - 50 questions is several minutes and a
    spinner tells you nothing about which half is failing.
    """
    blocked = await preflight(api, model)
    if blocked:
        yield "error", {"message": blocked}
        return

    questions = load_questions(only)
    run_id = f"ev_{uuid.uuid4().hex[:10]}"
    t0 = time.perf_counter()

    await db.execute(
        """INSERT INTO eval_runs (run_id, model, n_total, notes)
           VALUES (%(r)s, %(m)s, %(n)s, %(no)s)""",
        {"r": run_id, "m": model, "n": len(questions), "no": notes or None},
    )
    yield "start", {"run_id": run_id, "total": len(questions), "model": model}

    passed = 0
    async with httpx.AsyncClient(timeout=300) as client:
        for q in questions:
            try:
                answer = await ask(client, api, q["question"])
                result = await grade(q, answer, db)
            except Exception as exc:  # noqa: BLE001 - one bad question must not kill the run
                result = Result(q["id"], q["question"], q["grade"], False,
                                actual="exception", detail=f"{type(exc).__name__}: {exc}")
            passed += result.passed

            await db.execute(
                """INSERT INTO eval_results (run_id, question_id, question, grade,
                       passed, expected, actual, detail, spec, latency_ms)
                   VALUES (%(r)s, %(q)s, %(qt)s, %(g)s, %(p)s, %(e)s, %(a)s,
                           %(d)s, %(s)s, %(ms)s)
                   ON DUPLICATE KEY UPDATE run_id = run_id""",
                {"r": run_id, "q": result.question_id, "qt": result.question,
                 "g": result.grade, "p": result.passed, "e": result.expected,
                 "a": result.actual, "d": result.detail,
                 "s": json.dumps(result.spec) if result.spec else None,
                 "ms": result.latency_ms},
            )
            yield "result", result

    duration = int((time.perf_counter() - t0) * 1000)
    await db.execute(
        """UPDATE eval_runs SET finished_at = now(), n_passed = %(p)s,
               duration_ms = %(d)s WHERE run_id = %(r)s""",
        {"p": passed, "d": duration, "r": run_id},
    )
    yield "done", {"run_id": run_id, "passed": passed, "total": len(questions),
                   "duration_ms": duration, "model": model}
