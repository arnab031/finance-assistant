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

You NEVER compute numbers. You NEVER invent names, categories, or values that
are not in the data below. Your only job is to describe WHAT should be computed.
A SQL compiler does the rest.

{coverage}

{dates}

RULES
{rules}

Put anything you were unsure about into `ambiguities`."""


# Each example fixes a failure observed in the zero-shot baseline probe.

def build_system(reg: SemanticRegistry) -> str:
    """Rules come from the active profile: the two datasets have genuinely
    different vocabularies, and offering a model metrics that do not exist in
    the loaded schema is the fastest way to get a wrong answer."""
    from api.profiles.base import get_profile

    return SYSTEM.format(coverage=reg.prompt_context(), dates=prompt_block(),
                         rules=get_profile().prompt_rules.strip())


def build_user(question: str, previous: QuerySpec | None = None) -> str:
    from api.profiles.base import get_profile

    parts: list[str] = []
    for q, spec in get_profile().fewshot:
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

    A GROUP-BY IS NOT A TIME REFERENCE. "Break down spending by month" contains
    the word "month", which _TEMPORAL_RE matches, so this guard used to bail out
    and let an invented window through - measured, the model answered with
    period "2026 to date", silently dropping December 2025 and 9,241.00 from a
    total the question asked to be complete. The word names the axis to group
    ON, not a window to filter BY: the axis is already in spec.group_by, so a
    temporal token that is only a requested dimension is discounted here. Any
    real anchor ("last month", "in June", "2026") still matches and still
    protects a genuine period.
    """
    if spec.period is None:
        return spec

    anchors = [m.group(0).lower() for m in _TEMPORAL_RE.finditer(question)]
    grouped = {d.lower() for d in spec.group_by}
    if [a for a in anchors if a not in grouped]:
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


# "over"/"above"/"more than" are strict; "at least"/"minimum"/"or more" are not.
_MIN_STRICT = re.compile(r"\b(over|above|more than|greater than|exceed\w*)\b", re.I)
_MAX_STRICT = re.compile(r"\b(under|below|less than|smaller than)\b", re.I)


def _set_bound_strictness(spec: QuerySpec, question: str) -> QuerySpec:
    """Decide < vs <= from the words the user actually used.

    The model emits the number; this decides the comparator. Asked to list
    "debits over 50000" against data holding a debit of exactly 50000, an
    inclusive bound returned three rows where two were correct - the kind of
    error that never looks like one.
    """
    if spec.filters.min_amount is None and spec.filters.max_amount is None:
        return spec

    lo = spec.filters.min_amount is not None and bool(_MIN_STRICT.search(question))
    hi = spec.filters.max_amount is not None and bool(_MAX_STRICT.search(question))
    if not (lo or hi):
        return spec

    return spec.patched(
        filters=spec.filters.model_copy(
            update={"min_amount_exclusive": lo, "max_amount_exclusive": hi}
        ).model_dump()
    )


_ACCT_DIGITS = re.compile(r"\d[\d\s-]{7,}\d")
# Recovery only fires when the question actually says "account". Two bare dates
# sitting side by side ("between 2025-12-03 2026-06-24") are 16 digits under the
# pattern above, and injecting those as an account filter would answer zero for
# a question that has a real total - a wrong number, not an error.
_ACCT_WORD = re.compile(r"\ba/?c\b|\baccounts?\b|\bacct\b", re.I)


_MAX_ACCOUNTS = 5


def _has_account_digits(question: str) -> list[str]:
    """Digit runs in the question that could be an account number, normalised.

    A date range or an amount can also be a long digit run, so the length bound
    does the discriminating: "2026-06-24" reduces to 8 digits and is rejected.
    """
    found = [re.sub(r"\D", "", m.group(0)) for m in _ACCT_DIGITS.finditer(question)]
    return [d for d in found if 9 <= len(d) <= 20]


def _normalise_account_numbers(spec: QuerySpec, question: str) -> QuerySpec:
    """Reduce an account number to digits, and recover one the model dropped.

    Two failures this prevents, both of which end in a WRONG NUMBER rather than
    an error. People write account numbers the way they read them - "A/C
    3012 3456 7890 12", "30123456789012." at the end of a sentence - and the
    stored column is bare digits, so an unnormalised filter matches nothing and
    the answer is a confident zero for an account that has activity.

    Worse is the second case: the model puts the digits in the WRONG field, or
    omits them entirely while still answering `aggregate`. The filter then never
    applies and the reply is the whole dataset's total presented as one
    account's - the silent-unfiltered failure api/profiles/base.py:Filt warns
    about. So when the question plainly contains an account number and no
    account filter survived extraction, it is put back here.
    """
    from api.profiles.base import get_profile

    if "account_numbers" not in get_profile().filters:
        return spec

    digits = _has_account_digits(question)

    cleaned = [re.sub(r"\D", "", v) for v in spec.filters.account_numbers]
    cleaned = [v for v in cleaned if v]

    # PROVENANCE. A value may only be filtered on if its digits are in the
    # question the user just asked. The few-shot carries an account number, the
    # model can hallucinate one, and text quoted inside a question could smuggle
    # one in - each would silently answer about an account nobody named. Both
    # sides are compared digits-only, so "3012 3456 7890 12" still matches.
    invented = [v for v in cleaned if v not in digits]
    if invented:
        log.info("dropping account number(s) %s not present in the question", invented)
    cleaned = [v for v in cleaned if v in digits]

    # A cap, because a list filter has no maxItems the decoder reliably honours.
    # One request carrying fifty candidate numbers alongside group_by "account"
    # would return a labelled row per hit - batch existence probing, where a
    # single guess is sealed by v_txn's inner join.
    if len(cleaned) > _MAX_ACCOUNTS:
        log.info("capping %d account numbers at %d", len(cleaned), _MAX_ACCOUNTS)
        cleaned = cleaned[:_MAX_ACCOUNTS]

    if not cleaned and digits and spec.intent != "unsupported" \
            and _ACCT_WORD.search(question):
        log.info("recovering account number(s) %s the model dropped", digits)
        cleaned = digits
    if cleaned == list(spec.filters.account_numbers):
        return spec
    return spec.patched(
        filters=spec.filters.model_copy(
            update={"account_numbers": cleaned}
        ).model_dump(),
    )


# "account number" as the THING being asked for, with no number supplied.
_ACCT_DISCLOSE = re.compile(
    r"\b(show|list|display|print|reveal|tell me|give me|what(?:'s| is| are)?|which)\b"
    r"[^?]{0,60}\baccount\s*(?:number|no\.?|nos\.?)s?\b",
    re.I,
)


def _refuse_account_number_disclosure(spec: QuerySpec, question: str) -> QuerySpec:
    """Filtering by a number the asker already has is fine. Handing them numbers
    they do not is not, and that line is now enforced here rather than trusted
    to the model.

    The prompt used to refuse every question containing "account number", which
    is why a plain filtered sum was declined. Loosening it to allow filtering
    also loosens it for display - and prof.list_columns selects t.account_number
    while resolve_rows() hands the USER the real value, so an intent=list answer
    prints full account numbers in the breakdown table. Only the model's own
    judgement stood between "list every account number" and that table.

    The test is possession: a question naming account numbers WITHOUT supplying
    one is asking to be given them. With one, the value came from the asker and
    filtering by it discloses nothing.
    """
    if not _ACCT_DISCLOSE.search(question):
        return spec
    if spec.filters.account_numbers or _has_account_digits(question):
        return spec

    reason = ("Account numbers are masked and cannot be listed or shown in "
              "full. Ask about a specific account by its number and that is "
              "answerable.")
    if spec.intent == "unsupported":
        return spec.patched(reasoning=reason)
    log.info("refusing account-number disclosure: %r", question)
    return spec.patched(intent="unsupported", reasoning=reason,
                        ambiguities=[*spec.ambiguities, reason])


def _force_unsupported_for_absent_concepts(
    spec: QuerySpec, question: str
) -> QuerySpec:
    """Decline questions about domains this schema provably does not contain.

    The prompt already says there are no vendors. Asked "how much did we spend
    on vendor payouts last month?" the model agreed there were none, then put
    "vendor" into counterparty_like and answered with every debit in August -
    a confident number for a question the data cannot answer. Nothing about the
    result looked wrong, which is what makes it the worst failure mode here.

    So the check is deterministic and lives on the profile. A schema knows what
    it does not hold; that knowledge should not depend on a 7B model's mood.
    """
    from api.profiles.base import get_profile

    prof = get_profile()
    for pattern, reason in prof.absent_concepts:
        if re.search(pattern, question, re.I):
            if spec.intent == "unsupported":
                # Already declining, but attach the specific reason anyway:
                # ask.py surfaces it to the user, and "there is no budget data
                # here" beats a generic list of everything the schema lacks.
                return spec.patched(reasoning=reason)
            log.info("forcing unsupported: %r matched %r", question, pattern)
            return spec.patched(
                intent="unsupported",
                reasoning=reason,
                ambiguities=[*spec.ambiguities, reason],
            )
    return spec


def sanity_pass(spec: QuerySpec, question: str) -> QuerySpec:
    spec = _correct_subyear_basis(spec, question)
    spec = _strip_invented_period(spec, question)
    spec = _strip_unasked_status_filters(spec, question)
    spec = _set_bound_strictness(spec, question)
    spec = _normalise_account_numbers(spec, question)
    spec = _refuse_account_number_disclosure(spec, question)
    return _force_unsupported_for_absent_concepts(spec, question)


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
