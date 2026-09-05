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


_WEAK_TEMPORAL = frozenset({
    "year", "quarter", "month", "week", "day",
    "last", "past", "previous", "this", "current", "recent", "prior",
})
_ANCHORED_NOUN = re.compile(
    r"\b(last|past|previous|this|current|recent|prior|next|coming)\s+(?:\w+\s+)?"
    r"(year|quarter|month|week|day)s?\b", re.I)


def _coerce_missing_fiscal_basis(spec: QuerySpec, question: str) -> QuerySpec:
    """A fiscal-year basis on a dataset with no fiscal year cannot compile.

    "Which quarter had the highest spend?" made the model reach for
    date_basis=fiscal_year - quarters sound fiscal - and the compiler answered
    with "I couldn't turn that into a query", which reads as a broken system
    for a question about calendar quarters. The profile knows it has no such
    column; the basis is coerced to the payment date and the year, if any, is
    kept as a calendar window rather than dropped.
    """
    from api.profiles.base import get_profile

    if spec.date_basis != "fiscal_year" or "fiscal_year" in get_profile().dimensions:
        return spec
    log.info("coercing fiscal_year basis -> payment_date (dataset has no fiscal year)")
    changes: dict = {"date_basis": "payment_date", "fiscal_year": None, "compare_fiscal_year": None}
    fy = str(spec.fiscal_year or "")
    if fy.isdigit() and spec.period is None:
        changes["period"] = {"start": f"{fy}-01-01", "end": f"{int(fy)+1}-01-01", "label": fy}
    if spec.intent == "compare" and not spec.compare_period:
        changes["intent"] = "aggregate"
    return spec.patched(**changes)


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

    # A bare period noun or determiner is not a window on its own. "Which day
    # had the most spending?", "each day", "by month", "rank the quarters", and
    # "regarding THIS account number" all match _TEMPORAL_RE and used to keep
    # an invented period alive - "which day" came back with zero rows. They
    # anchor only when qualified: "last month", "this quarter", "past week".
    # Everything else the regex knows - a year, a month name, Q2, FY, since/
    # between/during, ytd, today - is a real anchor on its own.
    anchors = [m.group(0).lower() for m in _TEMPORAL_RE.finditer(question)]
    strong = [a for a in anchors if a not in _WEAK_TEMPORAL]
    if strong or _ANCHORED_NOUN.search(question):
        return spec

    log.info("dropped invented period %r (question has no time reference)",
             spec.period.label)
    # A compare whose windows were both invented is not a compare at all: with
    # them gone QuerySpec rejects intent=compare, and the question never asked
    # for two periods. It becomes the plain aggregate it always was.
    return spec.patched(
        intent="aggregate" if spec.intent == "compare" else spec.intent,
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

# Reasons set by the deterministic correctors below. api/routes/ask.py shows a
# decline's own reasoning only when it recognises the string - otherwise the
# generic note - so every corrector-authored reason is registered here.
FORCED_REASONS: set[str] = set()

ACCOUNT_DISCLOSURE = (
    "Account numbers are masked and cannot be listed or shown in full. Ask "
    "about a specific account by its number and that is answerable.")
FORCED_REASONS.add(ACCOUNT_DISCLOSURE)

ACCOUNT_UNMATCHED = (
    "The account number in that request could not be matched to the question, "
    "so nothing was filtered. Give the full account number and it is answerable.")
FORCED_REASONS.add(ACCOUNT_UNMATCHED)

# A digit run introduced as a reference is not an account number, however long
# it is. "the account transactions with reference 1715499972" put that reference
# into account_numbers (matching nothing, so "no transactions" for a reference
# that exists) AND counted as possession of an account number, disarming the
# disclosure guard. Two signals are checked: the words before the run, and
# whether the model already filed the same digits under another filter.
_REF_CONTEXT = re.compile(
    r"\b(reference|ref\.?|ref\s*no\.?|utr|transaction\s*id|txn\s*id)\b[^\d]{0,25}$", re.I)


def _unclaimed_account_digits(spec: QuerySpec, question: str) -> list[str]:
    """Account-shaped digit runs in the question that are NOT a reference."""
    claimed: set[str] = set()
    for v in [*spec.filters.reference_id,
              spec.filters.description_like or "", spec.filters.counterparty_like or ""]:
        d = re.sub(r"\D", "", v)
        if 9 <= len(d) <= 20:
            claimed.add(d)
    out = []
    for m in _ACCT_DIGITS.finditer(question):
        before = question[: m.start()]
        for d in _has_account_digits(m.group(0)):
            if d in claimed or _REF_CONTEXT.search(before):
                continue
            out.append(d)
    return out


def _mask(v: str) -> str:
    """Logs are a surface too: last four digits only."""
    return f"…{v[-4:]}" if len(v) > 4 else "…"


def _has_account_digits(question: str) -> list[str]:
    """Digit runs in the question that could be an account number, normalised.

    A date range or an amount can also be a long digit run, so the length bound
    does the discriminating: "2026-06-24" reduces to 8 digits and is rejected.
    """
    out: list[str] = []
    for m in _ACCT_DIGITS.finditer(question):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        # "50200013729069 50200099284137" is one 28-digit run to the regex and
        # used to be discarded whole. Anything over the bound is split on
        # whitespace and each piece judged on its own; a grouped single number
        # ("3012 3456 7890 12") is 14 digits and never reaches this branch.
        pieces = [digits] if len(digits) <= 20 else [re.sub(r"\D", "", p) for p in raw.split()]
        out += [d for d in pieces if 9 <= len(d) <= 20]
    return out


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

    digits = _unclaimed_account_digits(spec, question)

    cleaned = [re.sub(r"\D", "", v) for v in spec.filters.account_numbers]
    cleaned = [v for v in cleaned if v]

    # PROVENANCE. A value may only be filtered on if its digits are in the
    # question the user just asked. The few-shot carries an account number, the
    # model can hallucinate one, and text quoted inside a question could smuggle
    # one in - each would silently answer about an account nobody named. Both
    # sides are compared digits-only, so "3012 3456 7890 12" still matches.
    invented = [v for v in cleaned if v not in digits]
    if invented:
        log.info("dropping account number(s) %s not present in the question",
                 [_mask(v) for v in invented])
    cleaned = [v for v in cleaned if v in digits]

    # A cap, because a list filter has no maxItems the decoder reliably honours.
    # One request carrying fifty candidate numbers alongside group_by "account"
    # would return a labelled row per hit - batch existence probing, where a
    # single guess is sealed by v_txn's inner join.
    if not cleaned and digits and spec.intent != "unsupported" \
            and _ACCT_WORD.search(question):
        log.info("recovering account number(s) %s the model dropped",
                 [_mask(v) for v in digits])
        cleaned = digits

    # The model wanted an account filter, provenance rejected every value it
    # offered, and the question holds no number to recover. Running on would
    # answer the WHOLE dataset under the guise of one account - the silent
    # failure this function exists to prevent - so it fails loudly instead.
    if invented and not cleaned and spec.intent != "unsupported" \
            and _ACCT_WORD.search(question):
        log.info("refusing: account filter requested but no number in the question")
        return spec.patched(intent="unsupported", reasoning=ACCOUNT_UNMATCHED,
                            ambiguities=[*spec.ambiguities, ACCOUNT_UNMATCHED],
                            filters=spec.filters.model_copy(
                                update={"account_numbers": []}).model_dump())

    # After recovery, not before it - a recovered list used to skip the cap.
    if len(cleaned) > _MAX_ACCOUNTS:
        log.info("capping %d account numbers at %d", len(cleaned), _MAX_ACCOUNTS)
        cleaned = cleaned[:_MAX_ACCOUNTS]
    if cleaned == list(spec.filters.account_numbers):
        return spec
    return spec.patched(
        filters=spec.filters.model_copy(
            update={"account_numbers": cleaned}
        ).model_dump(),
    )


# "account number" as the THING being asked for, with no number supplied.
_ACCT_PHRASE = r"\baccount\s*(?:number|no\.?|nos\.?)s?\b"
_ACCT_DISCLOSE = re.compile(
    r"\b(show|list|display|print|reveal|tell me|give me|what(?:'s| is| are)?|which)\b"
    r"[^?]{0,60}" + _ACCT_PHRASE,
    re.I,
)
# "account numbers please" has no verb to match; on a LIST it is still a request
# to be handed them.
_ACCT_NOUN = re.compile(_ACCT_PHRASE, re.I)


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
    asks = _ACCT_DISCLOSE.search(question) or (
        spec.intent == "list" and _ACCT_NOUN.search(question))
    if not asks:
        return spec
    # Possession means an account-shaped number that is not a reference: a
    # 10-digit transaction reference used to count, and disarmed the guard.
    if spec.filters.account_numbers or _unclaimed_account_digits(spec, question):
        return spec

    reason = ACCOUNT_DISCLOSURE
    if spec.intent == "unsupported":
        return spec.patched(reasoning=reason)
    log.info("refusing account-number disclosure: %r",
             re.sub(r"\d{5,}", lambda m: _mask(m.group(0)), question))
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


# Direction NOUNS - the thing being asked about, not a verb like "spend".
_CREDIT_WORDS = re.compile(r"\b(credits?|deposits?|inflows?|money in|receipts?)\b", re.I)
_DEBIT_WORDS = re.compile(r"\b(debits?|withdrawals?|outflows?|money out|payouts?)\b", re.I)
# Metrics that carry no direction of their own, so a direction word in the
# question can only be honoured through the transaction_type filter.
_NEUTRAL_METRICS = frozenset({"avg_amount", "max_amount", "txn_count", "gross_amount"})


def _apply_direction_words(spec: QuerySpec, question: str) -> QuerySpec:
    """Honour "credits"/"debits" when the metric cannot.

    Measured: "List all the credits" came back intent=list with NO
    transaction_type filter and all ten rows - debits included - presented as
    the credits. "What is the average debit amount?" averaged every transaction.
    The list intent has no metric to encode direction and avg/max/count are
    neutral, so the only place the word can land is the filter, and the model
    skips it. A question naming BOTH directions ("split between credits and
    debits") is a breakdown, not a filter, and is left alone.
    """
    from api.profiles.base import get_profile

    if spec.filters.transaction_type or "transaction_type" not in get_profile().filters:
        return spec
    credit, debit = bool(_CREDIT_WORDS.search(question)), bool(_DEBIT_WORDS.search(question))
    if credit == debit:                       # neither, or both
        return spec
    if spec.intent != "list" and spec.metric not in _NEUTRAL_METRICS:
        return spec                           # debit_amount etc. already carry it
    kind = "credit" if credit else "debit"
    log.info("direction word -> transaction_type=[%s]", kind)
    return spec.patched(filters=spec.filters.model_copy(
        update={"transaction_type": [kind]}).model_dump())


_SPLIT_BY_TYPE = re.compile(
    r"\b(split|break ?down|breakdown|separate[d]?)\b[^?]{0,40}\b(credits?\s+(and|vs\.?|versus)\s+debits?|"
    r"debits?\s+(and|vs\.?|versus)\s+credits?|by (transaction )?type|by direction)\b", re.I)


def _apply_split_by_type(spec: QuerySpec, question: str) -> QuerySpec:
    """"Split between credits and debits" is a group_by transaction_type.

    The model does this on its own when it is the ONLY dimension ("Show the
    split between credits and debits" passes) and drops it as soon as another
    dimension is present: "For each bank, show the split between credits and
    debits" came back group_by=['bank'], the split the question is about gone
    from the answer entirely. Same failure shape as the month dimension that
    vanished from "by bank for each month". Deterministic, additive, and only
    when the phrase is present and the dimension is not.
    """
    from api.profiles.base import get_profile

    if spec.intent != "aggregate" or "transaction_type" not in get_profile().dimensions:
        return spec
    if "transaction_type" in spec.group_by or not _SPLIT_BY_TYPE.search(question):
        return spec
    log.info("split-by-type phrase -> group_by += transaction_type")
    return spec.patched(group_by=[*spec.group_by, "transaction_type"])


_ENTITY_WORDS = re.compile(r"\bentit(?:y|ies)\b", re.I)
_SMALLEST_WORDS = re.compile(r"\b(smallest|lowest|minimum|least)\b[^?]{0,30}\b(transaction|payment|amount|debit|credit)s?\b", re.I)
_LARGEST_WORDS = re.compile(r"\b(largest|biggest|highest|maximum|max)\b[^?]{0,30}\b(single )?(transaction|payment|debit|credit)s?\b", re.I)
_SUM_METRICS = frozenset({"debit_amount", "credit_amount", "net_amount", "gross_amount", "avg_amount"})


def _apply_count_and_extreme_words(spec: QuerySpec, question: str) -> QuerySpec:
    """Two words the model reads as their neighbours.

    "distinct entities" came back as account_count - an entity is not an
    account, several accounts share one - and "smallest transaction" as a spend
    total because no MIN metric existed. Both are unambiguous in the question.
    """
    from api.profiles.base import get_profile

    metrics = get_profile().metrics
    if spec.intent != "aggregate":
        return spec
    if _ENTITY_WORDS.search(question) and spec.metric == "account_count" \
            and "entity_count" in metrics:
        log.info("'entities' -> entity_count (model chose account_count)")
        return spec.patched(metric="entity_count")
    if _SMALLEST_WORDS.search(question) and "min_amount" in metrics \
            and spec.metric not in ("min_amount", "txn_count", "account_count", "entity_count"):
        log.info("'smallest' -> min_amount (model chose %s)", spec.metric)
        return spec.patched(metric="min_amount")
    # The mirror. "The largest transaction at each bank" came back as
    # debit_amount grouped by bank - every bank's SPEND, presented as its
    # largest transaction. "Largest transaction" is a MAX, the prompt says so,
    # and only a sum metric is corrected so "how many large payments" is safe.
    if _LARGEST_WORDS.search(question) and "max_amount" in metrics and spec.metric in _SUM_METRICS:
        log.info("'largest transaction' -> max_amount (model chose %s)", spec.metric)
        return spec.patched(metric="max_amount")
    return spec


_WHICH_PERIOD = re.compile(
    r"\b(which|what)\s+(day|date|month|quarter|week|year|bank|counterparty|account|program)\b"
    r"[^?]{0,40}\b(most|highest|largest|biggest|top|lowest|least|smallest)\b", re.I)
_RANK_BY = re.compile(r"\brank(?:ing)?\s+(?:the\s+)?(days?|dates?|months?|quarters?|weeks?|banks?|counterpart(?:y|ies)|accounts?|programs?)\b", re.I)
# "on each day", "per bank": the axis with no superlative - group, keep the sort.
_EACH_NOUN = re.compile(r"\b(each|every|per)\s+(day|date|month|quarter|week|bank|counterparty|account|program)\b", re.I)
_NOUN_TO_DIM = {"day": "day", "date": "day", "month": "month", "quarter": "quarter",
                "week": "day", "year": "month", "bank": "bank", "counterparty": "counterparty",
                "counterparties": "counterparty", "account": "account", "program": "program"}


def _apply_which_extreme(spec: QuerySpec, question: str) -> QuerySpec:
    """"Which month had the highest spend?" is a grouped aggregate, highest
    first. The model answered it five ways in one pass - an ungrouped total, a
    compare, a MAX of one transaction, a compile error - and never the way the
    question reads. Deterministic: the noun is the axis, the superlative is
    the sort."""
    from api.profiles.base import get_profile

    m = _WHICH_PERIOD.search(question) or _RANK_BY.search(question) or _EACH_NOUN.search(question)
    if not m:
        return spec
    noun = m.group(2 if m.re in (_WHICH_PERIOD, _EACH_NOUN) else 1).lower().rstrip("s")
    noun = {"countie": "county", "counterpartie": "counterparty", "counterpart": "counterparty"}.get(noun, noun)
    dim = _NOUN_TO_DIM.get(noun)
    if not dim or dim not in get_profile().dimensions:
        return spec
    changes: dict = {}
    if spec.intent not in ("aggregate",):
        changes["intent"] = "aggregate"
        # A compare spec carries a second window; an aggregate must not.
        changes["compare_period"] = None
        changes["compare_fiscal_year"] = None
    if dim not in spec.group_by:
        changes["group_by"] = [dim, *spec.group_by]
    if spec.metric in ("max_amount", "min_amount") and m.re is _WHICH_PERIOD \
            and not (_LARGEST_WORDS.search(question) or _SMALLEST_WORDS.search(question)):
        # "which date did we spend the most" is a per-day SUM, not one txn -
        # but "the largest transaction at each bank" IS a MAX, and used to be
        # converted back to a spend total here after the extreme-words rule
        # had correctly set it.
        changes["metric"] = "debit_amount"
    if m.re is not _EACH_NOUN:            # "each day" asks for the axis, not a ranking
        lowest = bool(re.search(r"\b(lowest|least|smallest)\b", question, re.I))
        if spec.sort_desc == lowest:
            changes["sort_desc"] = not lowest
        if spec.order != "value":
            changes["order"] = "value"      # a time axis is otherwise chronological
    if not changes:
        return spec
    log.info("'which %s ... most' -> %s", noun, changes)
    return spec.patched(**changes)


# ---- a stated period wins ---------------------------------------------------
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_STATED_MONTH = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+((?:19|20)\d{2})\b", re.I)
_STATED_QUARTER = re.compile(r"\bq([1-4])\s*((?:19|20)\d{2})\b", re.I)
_STATED_YEAR = re.compile(r"(?<![\d-])((?:19|20)\d{2})(?![\d-])")
_BOUND_WORDS = re.compile(r"\b(over|above|more than|greater than|at least|under|below|less than|up to|at most|between)\b", re.I)


def _stated_windows(question: str) -> list[dict]:
    """Absolute periods written in the question, as Period dicts."""
    from datetime import date
    out = []
    for m in _STATED_MONTH.finditer(question):
        mon, yr = _MONTHS[m.group(1).lower()[:3]], int(m.group(2))
        nxt = date(yr + (mon == 12), (mon % 12) + 1, 1)
        out.append({"start": date(yr, mon, 1).isoformat(), "end": nxt.isoformat(),
                    "label": f"{date(yr, mon, 1):%B %Y}"})
    for m in _STATED_QUARTER.finditer(question):
        q, yr = int(m.group(1)), int(m.group(2)); mon = 3 * (q - 1) + 1
        nxt = date(yr + (q == 4), (mon + 3 - 1) % 12 + 1, 1)
        out.append({"start": date(yr, mon, 1).isoformat(), "end": nxt.isoformat(), "label": f"Q{q} {yr}"})
    if not out:
        for m in _STATED_YEAR.finditer(question):
            yr = int(m.group(1))
            out.append({"start": f"{yr}-01-01", "end": f"{yr + 1}-01-01", "label": str(yr)})
    return out


def _recover_stated_period(spec: QuerySpec, question: str) -> QuerySpec:
    """The period the user WROTE beats the one the model chose.

    The inverse of the invented-period bug, and measured just as often: with a
    bank or a counterparty in the question the model drops the month entirely
    ("HDFC in December 2025" answered for all time), attaches amount bounds of
    zero that were never asked for, or files "May 2026" under description_like
    as if it were narration text. A month-and-year in the question is not
    ambiguous, so it is parsed here and imposed. Compare questions carry two
    windows of their own and are left to the model.
    """
    if spec.intent in ("compare", "unsupported"):
        return spec
    stated = _stated_windows(question)
    changes: dict = {}
    filters = spec.filters.model_dump()

    # A month that landed in a text filter is a period, not a search term.
    for key in ("description_like", "counterparty_like"):
        v = filters.get(key)
        # Only when the filter IS the period - "May 2026" - not when a month
        # merely appears inside real narration text ("June 2026 festival").
        rest = _STATED_QUARTER.sub("", _STATED_MONTH.sub("", v or ""))
        if v and rest != v and len(re.sub(r"[^a-z]", "", rest.lower())) < 3:
            filters[key] = None; changes["filters"] = filters
            log.info("dropped %s=%r: that is a period", key, v)

    # Zero bounds nobody asked for: "over 0" is not a filter, it is noise that
    # excludes nothing today and reads as intent on the provenance panel.
    if not _BOUND_WORDS.search(question):
        for key in ("min_amount", "max_amount"):
            if filters.get(key) is not None and float(filters[key]) == 0:
                filters[key] = None; changes["filters"] = filters
                log.info("dropped invented %s=0", key)

    # "from 1 May to 30 June 2026" names two months but only one carries the
    # year, so it parsed as a single stated window and OVERRODE the model's
    # correct May-June range. Two month names means a range or a comparison:
    # the model's window stands.
    month_mentions = len(re.findall(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", question, re.I))
    if len(stated) == 1 and month_mentions <= 1:
        w = stated[0]
        cur = spec.period
        if cur is None or (str(cur.start), str(cur.end)) != (w["start"], w["end"]):
            log.info("stated period %r wins over %r", w["label"], cur.label if cur else None)
            changes["period"] = w
            changes["date_basis"] = "payment_date"
            changes["fiscal_year"] = None
    if not changes:
        return spec
    return spec.patched(**changes)


_THROUGHPUT_WORDS = re.compile(
    r"\b(in and out|throughput|total transacted|transacted in total|gross)\b", re.I)


def _apply_throughput_words(spec: QuerySpec, question: str) -> QuerySpec:
    """"In and out" means both directions added, and the prompt says so; the
    model still answered "how much money moved in and out" as NET (47,004
    against 546,616). The metric fork already treats these phrases as settling
    the gross/net question, so setting gross here just makes the two agree."""
    from api.profiles.base import get_profile

    if spec.intent != "aggregate" or "gross_amount" not in get_profile().metrics:
        return spec
    if spec.metric == "gross_amount" or not _THROUGHPUT_WORDS.search(question):
        return spec
    log.info("throughput phrase -> gross_amount (model chose %s)", spec.metric)
    return spec.patched(metric="gross_amount")


def _normalise_raw(raw: dict) -> dict:
    """Repair, BEFORE strict validation, the contradictions QuerySpec rejects.

    A spec the validator refuses never reaches sanity_pass, so the correctors
    there cannot help; the request dies as "could not produce a valid
    QuerySpec". Measured on "Which quarter had the highest spend?": the model
    emitted intent=compare with no second window - a compare of one thing -
    and two strict attempts failed the same way. The honest reading of that
    output is an aggregate, so that is what it becomes. Nothing here invents a
    field; it only drops or renames what cannot stand.
    """
    if not isinstance(raw, dict):
        return raw
    raw = dict(raw)
    if raw.get("intent") == "compare" and not raw.get("compare_period") \
            and not raw.get("compare_fiscal_year"):
        raw["intent"] = "aggregate"
    if raw.get("intent") != "compare":
        raw.pop("compare_period", None); raw.pop("compare_fiscal_year", None)
    if raw.get("date_basis") == "fiscal_year" and not raw.get("fiscal_year"):
        raw["date_basis"] = "payment_date"
    return raw


def sanity_pass(spec: QuerySpec, question: str) -> QuerySpec:
    spec = _coerce_missing_fiscal_basis(spec, question)
    spec = _correct_subyear_basis(spec, question)
    spec = _strip_invented_period(spec, question)
    spec = _strip_unasked_status_filters(spec, question)
    spec = _set_bound_strictness(spec, question)
    spec = _apply_direction_words(spec, question)
    spec = _apply_split_by_type(spec, question)
    spec = _apply_count_and_extreme_words(spec, question)
    spec = _apply_which_extreme(spec, question)
    spec = _recover_stated_period(spec, question)
    spec = _apply_throughput_words(spec, question)
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
        return sanity_pass(QuerySpec.model_validate(_normalise_raw(result.data)), question), result
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
        return sanity_pass(QuerySpec.model_validate(_normalise_raw(retry.data)), question), retry
    except ValidationError as second_error:
        # Last resort: coerce rather than fail. The strict validator raises on a
        # basis/period contradiction so the retry above gets a chance to resolve
        # it properly; if the model insists, dropping the conflicting field is
        # still a better answer than no answer, and the coercion is recorded in
        # `ambiguities` so it shows up in the provenance panel.
        try:
            spec = QuerySpec.model_validate(_normalise_raw(retry.data), context={"lenient": True})
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
