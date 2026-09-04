"""
LLM call #1 - question -> QuerySpec.

This is the ONLY generative step before computation. Everything downstream is
deterministic, so the quality of this file is the quality of the assistant.

Three things carry the weight, in order of impact:

  1. Schema-constrained decoding (`extraction_schema()` via Ollama's `format`).
     Guarantees shape. Measured 5/5 valid JSON.
  2. Few-shot examples. Guarantee *meaning* - the baseline probe was ~2/5
     semantically correct with none. Each example below targets a failure that
     was actually observed, not an imagined one.
  3. Pre-resolved date windows (api/dates.py), so the model selects a period
     rather than deriving one.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from api.dates import prompt_block, resolved_windows
from api.llm.base import LLM, LLMResult
from api.registry import SemanticRegistry
from api.schema import QuerySpec, extraction_schema

log = logging.getLogger("tbx.extract")

SYSTEM = """You convert finance questions into a QuerySpec JSON object.

You NEVER compute numbers. You NEVER invent vendor names, categories, or departments.
Your only job is to describe WHAT should be computed. A SQL compiler does the rest.

{coverage}

{dates}

RULES

metric
  "spend", "spent", "paid", "payouts"        -> amount_paid
  "committed", "owed", "outstanding balance" -> amount_total
  "pending"                                  -> amount_pending
  "how many transactions"                    -> txn_count
  "how many vendors"                         -> vendor_count

date_basis
  ONLY these three phrasings mean "fiscal_year": "FY2026", "fiscal 2026",
  "fiscal year 2026". For those, set date_basis "fiscal_year" and set
  fiscal_year to the YEAR ONLY, e.g. "2026". Never invent start/end dates for a
  fiscal year; omit period entirely.

  EVERYTHING ELSE means "payment_date". Set period {{start, end}} from the
  resolved windows above.

  IF THE QUESTION MENTIONS NO TIME AT ALL, OMIT period ENTIRELY. Do not default
  to this year or any other window. "How much is still pending?" and "What was
  our total spend?" cover ALL the data; adding a period silently answers a
  narrower question than the one asked.

  "quarter", "last quarter", "this quarter", "Q1".."Q4", "month", "last month",
  "week" are NOT fiscal phrasings. A fiscal_year holds a whole year and CANNOT
  express a quarter or a month, so choosing it for those questions is always
  wrong. Use payment_date with the resolved window.

filters.vendor_query
  Fill ONLY when the question names a specific company.
  These are NOT company names - leave vendor_query out entirely:
      "vendor payouts", "vendors", "suppliers", "top vendors", "payments",
      "payouts", "transactions", "spend"

intent
  aggregate  - one number, optionally grouped
  list       - individual transactions
  compare    - two periods side by side (set compare_period or compare_fiscal_year)
  reconcile  - anything about reconciliation status
  anomaly    - unusually large payments vs a vendor's own history
  unsupported- the data cannot answer it. The database contains ONLY vendor
               payments: transactions, payouts, reconciliation, chart of
               accounts, vendors, departments, funds. It has NO employees,
               headcount, payroll, budgets, forecasts, revenue, or inventory.

Put anything you were unsure about into `ambiguities`."""


# Each example fixes a failure observed in the zero-shot baseline probe.
FEWSHOT: list[tuple[str, dict[str, Any]]] = [
    # Baseline put vendor_query="payouts" here, which would filter to nothing.
    ("How much did we spend on vendor payouts last month?",
     {"intent": "aggregate", "metric": "amount_paid", "group_by": [],
      "date_basis": "payment_date",
      "period": {"start": "2026-08-01", "end": "2026-09-01", "label": "August 2026"},
      "reasoning": "'vendor payouts' is the subject, not a company name"}),

    # Baseline put the entire question into vendor_query.
    ("Top 5 vendors by spend in the last 12 months",
     {"intent": "aggregate", "metric": "amount_paid", "group_by": ["vendor"],
      "date_basis": "payment_date",
      "period": {"start": "2025-09-01", "end": "2026-09-01", "label": "last 12 months"},
      "sort_desc": True, "limit": 5,
      "reasoning": "ranking vendors; no specific company named"}),

    # Baseline produced an Indian fiscal year here.
    ("How much did we pay McKesson in FY2026?",
     {"intent": "aggregate", "metric": "amount_paid", "group_by": [],
      "date_basis": "fiscal_year", "fiscal_year": "2026",
      "filters": {"vendor_query": "McKesson"},
      "reasoning": "fiscal basis, so year only and no period"}),

    # Baseline answered this as an aggregate instead of refusing.
    ("What is our headcount?",
     {"intent": "unsupported", "metric": "amount_paid", "group_by": [],
      "date_basis": "payment_date",
      "reasoning": "no employee or headcount data in this database"}),

    ("Which transactions are still unreconciled?",
     {"intent": "reconcile", "metric": "amount_total", "group_by": [],
      "date_basis": "payment_date",
      "filters": {"reconciliation_status": ["Unreconciled"]},
      "ambiguities": ["'unreconciled' could mean only Unreconciled, or every "
                      "status that is not Reconciled"]}),

    ("How does August 2026 compare to the month before?",
     {"intent": "compare", "metric": "amount_paid", "group_by": [],
      "date_basis": "payment_date",
      "period": {"start": "2026-08-01", "end": "2026-09-01", "label": "August 2026"},
      "compare_period": {"start": "2026-07-01", "end": "2026-08-01", "label": "July 2026"}}),

    ("Break down spend by category last month",
     {"intent": "aggregate", "metric": "amount_paid", "group_by": ["category"],
      "date_basis": "payment_date",
      "period": {"start": "2026-08-01", "end": "2026-09-01", "label": "August 2026"},
      "limit": 50}),

    ("Show me the 10 largest payments in August 2026",
     {"intent": "list", "metric": "amount_paid", "group_by": [],
      "date_basis": "payment_date",
      "period": {"start": "2026-08-01", "end": "2026-09-01", "label": "August 2026"},
      "sort_desc": True, "limit": 10}),

    # "quarter" reads as a fiscal word to a 7B: this exact question returned
    # date_basis "fiscal_year" with fiscal_year "2026", answering $16.8B for a
    # quarter worth $5.1B. A fiscal year cannot express a quarter at all.
    ("Spend by fund type last quarter",
     {"intent": "aggregate", "metric": "amount_paid", "group_by": ["fund_type"],
      "date_basis": "payment_date",
      "period": {"start": "2026-04-01", "end": "2026-07-01", "label": "Q2 2026"},
      "reasoning": "a quarter is a date window, never a fiscal_year"}),

    # No status word in the question, so no status filter in the spec. The model
    # volunteered payment_status ["Paid"] here unprompted, which silently
    # excluded reversals and moved the answer by $127M.
    ("How much did we spend last quarter?",
     {"intent": "aggregate", "metric": "amount_paid", "group_by": [],
      "date_basis": "payment_date",
      "period": {"start": "2026-04-01", "end": "2026-07-01", "label": "Q2 2026"},
      "reasoning": "no status was mentioned, so no status filter"}),
]


def build_system(reg: SemanticRegistry) -> str:
    return SYSTEM.format(coverage=reg.prompt_context(), dates=prompt_block())


def build_user(question: str, previous: QuerySpec | None = None) -> str:
    parts: list[str] = []
    for q, spec in FEWSHOT:
        parts.append(f"Q: {q}\nA: {json.dumps(spec, separators=(',', ':'))}")
    if previous is not None:
        parts.append(
            "The user is following up on this previous query:\n"
            f"{previous.model_dump_json(exclude_defaults=True)}\n"
            "Carry forward anything they did not change."
        )
    parts.append(f"Q: {question}\nA:")
    return "\n\n".join(parts)


class ExtractionFailed(RuntimeError):
    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


# --------------------------------------------------------------------------
# Post-extraction sanity pass
#
# Prompting a 7B is necessary but not sufficient. These two corrections are
# deterministic, so they hold regardless of whether the model followed
# instructions on any given run.
# --------------------------------------------------------------------------

# Words describing a window shorter than a year. A fiscal_year value cannot
# express any of them, so pairing them with a fiscal basis is provably wrong -
# not ambiguous, wrong.
_SUBYEAR_RE = re.compile(
    r"\b(quarter|q[1-4]|month|monthly|week|weekly|day|daily|ytd|"
    r"year to date|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\b",
    re.I,
)

# Question words that legitimately justify a status filter.
# Note the leading \w* on the reconcil- stem: "\breconcil\w*" does NOT match
# "unreconciled", because the stem starts mid-word there. That silently dropped
# the status filter on the problem statement's own reference question.
_STATUS_WORDS = re.compile(
    r"\b(\w*reconcil\w*|\w*matched|paid|unpaid|pending|reversed|retainage|"
    r"held|disputed|outstanding|open|cleared|settled|status)\b",
    re.I,
)


def _correct_subyear_basis(spec: QuerySpec, question: str) -> QuerySpec:
    """Rescue a sub-year question that was routed to the fiscal basis.

    Measured failure: "Spend by fund type last quarter" produced
    date_basis='fiscal_year', fiscal_year='2026' - answering $16.8B for a
    quarter worth $5.1B, a 3.3x overstatement with nothing to flag it. The model
    reads "quarter" as a fiscal term ("fiscal quarter, so year only", in its own
    reasoning field).

    There is no contradiction for the schema validator to catch: the spec is
    internally consistent, just answering a different question. Only the pairing
    of the question with the spec reveals it, which is why the check lives here.
    """
    if spec.date_basis != "fiscal_year" or not _SUBYEAR_RE.search(question):
        return spec

    windows = resolved_windows()
    lowered = question.lower()
    # Longest phrase first: "last 12 months" must beat "last month".
    for phrase in sorted(windows, key=len, reverse=True):
        if phrase in lowered:
            w = windows[phrase]
            log.info("basis corrected: fiscal_year -> payment_date (%s)", phrase)
            return spec.patched(
                date_basis="payment_date",
                fiscal_year=None,
                compare_fiscal_year=None,
                period=w.as_period(),
                ambiguities=[
                    *spec.ambiguities,
                    f'Read "{phrase}" as {w.label}. A fiscal year cannot express '
                    f"a period shorter than a year.",
                ],
            )

    # Sub-year wording we could not resolve. Leave the spec alone rather than
    # guess, but make the mismatch visible instead of silent.
    return spec.patched(ambiguities=[
        *spec.ambiguities,
        "Question mentions a period shorter than a year, but the answer covers "
        f"the whole of FY{spec.fiscal_year}.",
    ])


# Any token that gives a question a time bound. Absence of ALL of these means
# the question is unbounded and a period must not be invented.
# "FY2026" is one token, so \bfy\b and \b(?:19|20)\d{2}\b both miss it - there
# is no word boundary between the letters and the digits. The year alternative
# uses digit lookarounds instead, and fy is matched with its number attached.
_TEMPORAL_RE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?!\d)"
    r"|\bfy\s*\d{2,4}\b"
    r"|\b(last|past|previous|this|current|recent|since|between|before|after|"
    r"during|ytd|year to date|today|yesterday|now|"
    r"year|quarter|month|week|day|q[1-4]|fy|fiscal|financial)\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.I,
)


def _strip_invented_period(spec: QuerySpec, question: str) -> QuerySpec:
    """Remove a date window the question never asked for.

    Measured on the golden set: asked "What was our total spend across all the
    data?", the model returned period "2026 to date" - $18.4B instead of
    $34.3B. Same for "How much is still pending?" and every unbounded
    reconciliation question. Five of twelve canary failures were this one bug.

    An invented narrowing is worse than an invented widening: the number looks
    plausible, so nothing about the answer signals that a filter was applied.
    """
    if spec.period is None or _TEMPORAL_RE.search(question):
        return spec

    log.info("dropped invented period %r (question has no time reference)",
             spec.period.label)
    return spec.patched(
        period=None,
        compare_period=None,
        ambiguities=[
            *spec.ambiguities,
            "No period was requested, so the answer covers all available data.",
        ],
    )


def _strip_unasked_status_filters(spec: QuerySpec, question: str) -> QuerySpec:
    """Drop status filters the question never asked for.

    The model volunteered payment_status=['Paid'] on "Spend by fund type last
    quarter". That silently excludes reversals and moved the answer by $127M -
    a quiet error riding along with a loud one.
    """
    if _STATUS_WORDS.search(question):
        return spec
    if not (spec.filters.payment_status or spec.filters.reconciliation_status):
        return spec

    dropped = [*spec.filters.payment_status, *spec.filters.reconciliation_status]
    log.info("dropped unrequested status filter(s): %s", dropped)
    return spec.patched(
        filters=spec.filters.model_copy(
            update={"payment_status": [], "reconciliation_status": []}
        ).model_dump(),
        ambiguities=[
            *spec.ambiguities,
            f"Ignored a status filter the question did not ask for ({', '.join(dropped)}).",
        ],
    )


def sanity_pass(spec: QuerySpec, question: str) -> QuerySpec:
    spec = _correct_subyear_basis(spec, question)
    spec = _strip_invented_period(spec, question)
    return _strip_unasked_status_filters(spec, question)


async def extract(
    question: str,
    llm: LLM,
    reg: SemanticRegistry,
    previous: QuerySpec | None = None,
) -> tuple[QuerySpec, LLMResult]:
    """Question -> validated QuerySpec. Repairs once before giving up."""
    system = build_system(reg)
    user = build_user(question, previous)
    schema = extraction_schema()

    result = await llm.complete_json(system, user, schema)

    try:
        return sanity_pass(QuerySpec.model_validate(result.data), question), result
    except ValidationError as first_error:
        # Python deletes the `as` variable when the except block exits, so the
        # message has to be captured HERE. Referring to `first_error` below
        # raised UnboundLocalError - a crash that only reached users on the
        # repair path, which is why 49 passing tests never touched it. The
        # incident view caught it on the first real canary run.
        first_message = _brief(first_error)
        log.info("spec invalid, repairing: %s", first_message)

    repair_user = (
        f"{user}{json.dumps(result.data, separators=(',', ':'))}\n\n"
        f"That was rejected: {first_message}\n"
        "Return a corrected object. Remember: with date_basis 'fiscal_year' set "
        "fiscal_year and omit period; with 'payment_date' set period and omit "
        "fiscal_year."
    )
    retry = await llm.complete_json(system, repair_user, schema)
    retry.input_tokens += result.input_tokens
    retry.output_tokens += result.output_tokens
    retry.latency_ms += result.latency_ms
    retry.repaired = True

    try:
        return sanity_pass(QuerySpec.model_validate(retry.data), question), retry
    except ValidationError as second_error:
        # Last resort: coerce rather than fail. The strict validator raises on a
        # basis/period contradiction so the retry above gets a chance to resolve
        # it properly; if the model insists, dropping the conflicting field is
        # still a better answer than no answer, and the coercion is recorded in
        # `ambiguities` so it shows up in the provenance panel.
        try:
            spec = QuerySpec.model_validate(retry.data, context={"lenient": True})
        except ValidationError:
            raise ExtractionFailed(
                f"could not produce a valid QuerySpec: {_brief(second_error)}",
                raw=retry.raw,
            ) from second_error

        retry.coerced = True
        log.warning("spec coerced after two strict failures: %s", _brief(second_error))
        return sanity_pass(spec, question), retry


def _brief(err: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in err.errors()[:3]
    )
