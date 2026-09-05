"""
THE CONTRACT.

Everything in this system is defined by this file:
  - `QuerySpec` is what the LLM produces and the SQL compiler consumes.
  - `Event` is what the backend streams and the frontend renders.

Both are exported to JSON Schema by `api/schema_export.py`:
  - QuerySpec's *extraction* subset constrains Ollama's `format` parameter
  - the Event union generates `web/lib/types.ts`

Change this file and both sides move together. Change it casually and they drift.
Freeze it before building anything else.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args

from pydantic import BaseModel, Field, ValidationInfo, model_validator

if TYPE_CHECKING:
    from api.profiles.base import Filt

# --------------------------------------------------------------------------
# Closed vocabularies
#
# Every one of these is a closed set on purpose. A hallucinated dimension or
# metric fails pydantic validation and never reaches the database. This is
# guardrail layer 1 (PLAN.md §4) and it costs nothing.
# --------------------------------------------------------------------------

# Dimensions and metrics are DATASET-SPECIFIC, so they cannot be Literal types -
# the organizers' schema has banks and credit/debit where the stand-in has
# vendors and a chart of accounts. They stay closed sets, just closed against
# the active profile instead of against a hardcoded tuple: an invented dimension
# still fails validation before it can reach the database.
Dimension = str
Metric = str

Intent = Literal[
    "aggregate",    # one number, optionally grouped
    "list",         # individual rows
    "compare",      # two periods side by side
    "reconcile",    # reconciliation-status questions
    "anomaly",      # outliers vs a vendor's own history
    "clarify",      # ambiguous - ask before computing
    "unsupported",  # not answerable from this schema
]

DateBasis = Literal["payment_date", "fiscal_year"]


# --------------------------------------------------------------------------
# QuerySpec
# --------------------------------------------------------------------------


class Period(BaseModel):
    """Half-open interval [start, end). Compiles to an indexed range predicate."""

    start: date
    end: date
    label: str = ""  # "August 2026" - used in narration, never in SQL

    @model_validator(mode="after")
    def _ordered(self) -> "Period":
        if self.end <= self.start:
            raise ValueError(f"period end {self.end} must be after start {self.start}")
        return self


class Filters(BaseModel):
    # These belong to no filter the active profile declares, so the extraction
    # schema never offers them and the compiler never reads them. They are kept
    # because the REGISTRY still populates the vocabularies behind them - the
    # bank profile maps payment_statuses to credit/debit - and because they are
    # where a future dataset plugs its own dimensions in. vendor_query and
    # vendor_ids were removed outright: nothing populated or read them once the
    # vendor resolver went.
    categories: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    funds: list[str] = Field(default_factory=list)
    programs: list[str] = Field(default_factory=list)
    payment_status: list[str] = Field(default_factory=list)
    reconciliation_status: list[str] = Field(default_factory=list)

    # --- bank-statement dataset ---
    # No counterparty table exists there, so a company name is a text search
    # against `description` rather than a foreign key.
    counterparty_like: str | None = None
    description_like: str | None = None
    transaction_type: list[str] = Field(default_factory=list)
    banks: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(default_factory=list)
    # The account NUMBER, which is not account_id: the id is an internal UUID,
    # the number is what a person reads off a statement and types into the box.
    # Filtering by one is not a disclosure - the value came FROM the asker - so
    # this is deliberately answerable while displaying a full number is not.
    account_numbers: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    reference_id: list[str] = Field(default_factory=list)

    min_amount: Decimal | None = None
    max_amount: Decimal | None = None

    # "over 50000" excludes a transaction of exactly 50000; "at least 50000"
    # includes it. Both phrasings are ordinary, and on this data the difference
    # was a whole row out of three.
    #
    # These are NOT in the extraction schema. The model emits only the number;
    # the comparator is derived from the question text in extract.py, because
    # the distinction is carried by two English words and a regex reads them
    # more reliably than a 7B model does.
    min_amount_exclusive: bool = False
    max_amount_exclusive: bool = False


class QuerySpec(BaseModel):
    intent: Intent
    metric: Metric = "amount_paid"
    group_by: list[Dimension] = Field(default_factory=list, max_length=3)
    filters: Filters = Field(default_factory=Filters)

    # ---- The $1.34B fork (AMBIGUITY.md §A) -------------------------------
    #
    # `date_basis` selects WHICH pair of fields below is authoritative:
    #
    #   "payment_date" -> period / compare_period       (model derives dates)
    #   "fiscal_year"  -> fiscal_year / compare_fiscal_year  (model emits "2026")
    #
    # Measured reason for the split: asked "How much did we pay McKesson in
    # FY2026?", qwen2.5:7b-instruct returned 2026-04-01 -> 2027-03-31 - the
    # Indian fiscal year, not SF's Jul-Jun. The model is unreliable at deriving
    # fiscal boundaries and never needs to: the compiler filters the
    # `fiscal_year` column directly. The validator below discards any dates
    # guessed under a fiscal basis, so that failure is unreachable rather than
    # merely discouraged.
    date_basis: DateBasis = "payment_date"

    period: Period | None = None
    compare_period: Period | None = None

    fiscal_year: str | None = None          # "2026" - never a date range
    compare_fiscal_year: str | None = None

    sort_desc: bool = True
    # How a grouped result is ordered when the axis is TIME. Trends read
    # chronologically whatever sort_desc says - that is deliberate, or "break
    # down by month" would come back newest-first. But "which month had the
    # highest spend?" is a ranking, and chronological order answered it with the
    # first month rather than the largest. Set only by the extraction
    # correctors, never offered to the model; None keeps the trend behaviour.
    order: Literal["chronological", "value"] | None = None
    limit: Annotated[int, Field(ge=1, le=200)] = 20

    # Populated by the extractor, surfaced in the provenance panel.
    reasoning: str = ""
    ambiguities: list[str] = Field(default_factory=list)

    # ---- invariants -------------------------------------------------------

    @model_validator(mode="after")
    def _known_vocabulary(self) -> "QuerySpec":
        """Guardrail layer 1, now profile-aware."""
        from api.profiles.base import get_profile

        prof = get_profile()
        if self.metric not in prof.metrics:
            raise ValueError(
                f"unknown metric {self.metric!r}; "
                f"this dataset has: {', '.join(prof.metric_names())}"
            )
        unknown = [d for d in self.group_by if d not in prof.dimensions]
        if unknown:
            raise ValueError(
                f"unknown dimension(s) {unknown}; "
                f"this dataset has: {', '.join(prof.dimension_names())}"
            )
        if self.intent in prof.disabled_intents:
            raise ValueError(
                f"intent {self.intent!r} is not supported by this dataset"
            )
        return self

    @model_validator(mode="after")
    def _reconcile_date_fields(self, info: ValidationInfo) -> "QuerySpec":
        """Keep exactly one temporal representation authoritative.

        A fiscal basis carrying a period is a genuine contradiction: the model
        has asserted two different time filters and we cannot know which it
        meant. Raising sends it back through the repair retry, which is the only
        place that can actually resolve the conflict.

        The earlier version discarded the period silently. That was right for
        the case it was written for (a hallucinated Apr-Mar window alongside a
        correct fiscal_year) but wrong in general: it also discarded *correct*
        dates whenever the basis was the mistaken half, turning a recoverable
        error into a silently wrong answer with no trace in `ambiguities`.

        Pass context {"lenient": True} to coerce instead of raising. That path
        exists only as extract.py's last resort, so a stubborn model degrades to
        the old behaviour rather than failing the question outright.
        """
        lenient = bool((info.context or {}).get("lenient"))

        if self.date_basis == "fiscal_year":
            if (self.period is not None or self.compare_period is not None) and not lenient:
                raise ValueError(
                    "date_basis='fiscal_year' cannot carry a period. Either set "
                    "date_basis='payment_date' and keep the period, or drop the "
                    "period and keep fiscal_year."
                )
            if self.period is not None or self.compare_period is not None:
                self.ambiguities = [
                    *self.ambiguities,
                    "Model gave both a fiscal year and a date range; the date "
                    "range was dropped.",
                ]
            self.period = None
            self.compare_period = None
            if self.intent not in ("clarify", "unsupported") and not self.fiscal_year:
                raise ValueError("date_basis='fiscal_year' requires fiscal_year")
        else:
            if (self.fiscal_year or self.compare_fiscal_year) and not lenient:
                raise ValueError(
                    "date_basis='payment_date' cannot carry a fiscal_year. Either "
                    "set date_basis='fiscal_year', or drop fiscal_year."
                )
            self.fiscal_year = None
            self.compare_fiscal_year = None
        return self

    @model_validator(mode="after")
    def _compare_needs_two(self) -> "QuerySpec":
        if self.intent == "compare":
            has_second = self.compare_period is not None or self.compare_fiscal_year is not None
            if not has_second:
                raise ValueError("intent='compare' requires compare_period or compare_fiscal_year")
        return self

    @model_validator(mode="after")
    def _amount_bounds(self) -> "QuerySpec":
        if (self.filters.min_amount is not None
                and self.filters.max_amount is not None
                and self.filters.max_amount < self.filters.min_amount):
            raise ValueError("max_amount must be >= min_amount")
        return self

    # ---- helpers ----------------------------------------------------------

    def is_temporally_bounded(self) -> bool:
        return self.period is not None or self.fiscal_year is not None

    def patched(self, **changes: Any) -> "QuerySpec":
        """Multi-turn follow-ups and ambiguity interpretations patch the previous
        spec rather than re-extracting.

        Validated leniently: a caller switching date_basis supplies the new
        temporal fields and expects the stale ones to be cleared, which is a
        deliberate coercion rather than a model contradiction.
        """
        return QuerySpec.model_validate(
            self.model_dump() | changes, context={"lenient": True}
        )


# --------------------------------------------------------------------------
# Extraction schema for Ollama's `format` parameter
#
# Built from the Literal vocabularies above via get_args(), so it cannot drift
# from QuerySpec. Deliberately FLAT - no $ref/$defs - because llama.cpp's
# JSON-Schema-to-GBNF conversion is happier without them, and a flat schema is
# what the measured 5/5-valid-JSON probe actually used.
# --------------------------------------------------------------------------


def _filter_schema(filt: "Filt") -> dict[str, Any]:
    """One filter's JSON Schema declaration, keyed off how it compiles."""
    if filt.kind == "text":
        return {"type": "string", "description": filt.hint}
    if filt.kind == "number":
        return {"type": "number", "description": filt.hint}
    out = {"type": "array", "items": {"type": "string"}, "description": filt.hint}
    if filt.max_items:
        # Declared to the decoder; enforced again in api/extract.py because
        # llama.cpp's grammar conversion does not reliably honour maxItems.
        out["maxItems"] = filt.max_items
    return out


def extraction_schema() -> dict[str, Any]:
    """JSON Schema constraining LLM call #1. Flat and self-contained.

    Enums are read from the active profile, so the model is offered only the
    dimensions and metrics that actually exist in the loaded dataset.
    """
    from api.profiles.base import get_profile

    prof = get_profile()
    period = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            "end": {"type": "string", "description": "YYYY-MM-DD, EXCLUSIVE"},
            "label": {"type": "string"},
        },
        "required": ["start", "end"],
    }
    strings = {"type": "array", "items": {"type": "string"}}

    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string",
                       "enum": [i for i in get_args(Intent)
                                if i not in prof.disabled_intents]},
            "metric": {"type": "string", "enum": prof.metric_names()},
            "group_by": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string", "enum": prof.dimension_names()},
            },
            "date_basis": {"type": "string", "enum": list(get_args(DateBasis))},
            "period": period,
            "compare_period": period,
            "fiscal_year": {
                "type": "string",
                "description": "Year only, e.g. '2026'. Never a date range.",
            },
            "compare_fiscal_year": {"type": "string"},
            "filters": {
                "type": "object",
                # Built from the ACTIVE PROFILE. Hardcoding this block meant the
                # model could not emit a field the dataset actually had:
                # counterparty_like was absent from the schema, so constrained
                # decoding blocked it and every "how much did we pay X" question
                # silently returned the unfiltered total.
                "properties": {
                    name: _filter_schema(filt) for name, filt in prof.filters.items()
                },
            },
            "sort_desc": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "reasoning": {"type": "string"},
            "ambiguities": strings,
        },
        "required": ["intent", "metric", "group_by", "date_basis"],
    }


# --------------------------------------------------------------------------
# SSE events
#
# The frontend reducer's action union IS this union. Adding a member here and
# regenerating types.ts turns a missed case into a TypeScript exhaustiveness
# error rather than a silent no-op.
# --------------------------------------------------------------------------

Stage = Literal["understanding", "checking", "querying", "explaining"]


class StageEvent(BaseModel):
    type: Literal["stage"] = "stage"
    stage: Stage


class ThreadEvent(BaseModel):
    """First frame of every response. A client that sent no thread_id gets the
    server-generated one here and should send it back on the next question -
    without it, settled ambiguity choices cannot be loaded."""

    type: Literal["thread"] = "thread"
    thread_id: str


class SpecEvent(BaseModel):
    type: Literal["spec"] = "spec"
    spec: QuerySpec


class ClarifyOption(BaseModel):
    key: str
    label: str
    detail: str
    preview: str  # "₹16,82,32,39,767.06 · 5,11,237 transactions"


class ClarifyEvent(BaseModel):
    type: Literal["clarify"] = "clarify"
    ambiguity_id: str
    kind: Literal["temporal", "metric", "entity", "scope", "anchor"]
    message: str
    options: list[ClarifyOption]
    default_key: str


class SqlEvent(BaseModel):
    type: Literal["sql"] = "sql"
    sql: str
    params: dict[str, Any] = Field(default_factory=dict)


class RowsEvent(BaseModel):
    type: Literal["rows"] = "rows"
    result_id: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: int
    truncated: bool = False


class TokenEvent(BaseModel):
    type: Literal["token"] = "token"
    text: str


class VerifiedEvent(BaseModel):
    """Numeric provenance check - PLAN.md §4 layer 4."""

    type: Literal["verified"] = "verified"
    ok: bool
    numbers_checked: int
    unverified: list[str] = Field(default_factory=list)


class NoteEvent(BaseModel):
    type: Literal["note"] = "note"
    kind: Literal["assumption", "coverage", "anomaly"]
    text: str


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    message_id: str
    confidence: Literal["high", "medium", "low"]
    # What the answer cost, in the pipeline's own measure. The breakdown table
    # already shows sql_ms, but that is the DATABASE step alone - single-digit
    # milliseconds - while the wait a person actually experiences is the two
    # model calls around it. Showing only the fast number invites the reader to
    # think the system is instant and something else is slow.
    total_ms: int = 0
    extract_ms: int = 0      # LLM call #1: question -> typed spec
    sql_ms: int = 0          # the database
    narrate_ms: int = 0      # LLM call #2, including verification and any retry


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    recoverable: bool = True


Event = Annotated[
    ThreadEvent | StageEvent | SpecEvent | ClarifyEvent | SqlEvent | RowsEvent
    | TokenEvent | VerifiedEvent | NoteEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = None
    # Which model answers THIS question. Absent means the configured default.
    # Validated against EVAL_MODELS server-side, so the field cannot be used to
    # make the daemon reach for an arbitrary name.
    model: str | None = None


class ClarifyRequest(BaseModel):
    thread_id: str
    ambiguity_id: str
    chosen_key: str
    # Carried so resolving an ambiguity continues on the model that raised it.
    model: str | None = None


class Coverage(BaseModel):
    earliest: date
    latest: date
    transaction_count: int
    total_paid: Decimal
    currency: str = "INR"
