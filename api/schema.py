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
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, Field, ValidationInfo, model_validator

# --------------------------------------------------------------------------
# Closed vocabularies
#
# Every one of these is a closed set on purpose. A hallucinated dimension or
# metric fails pydantic validation and never reaches the database. This is
# guardrail layer 1 (PLAN.md §4) and it costs nothing.
# --------------------------------------------------------------------------

Dimension = Literal[
    "vendor", "category", "account", "object", "department",
    "fund", "fund_type", "program", "month", "quarter",
    "fiscal_year", "payment_status", "reconciliation_status",
]

Metric = Literal[
    "amount_paid", "amount_total", "amount_pending", "amount_retainage",
    "txn_count", "voucher_count", "vendor_count", "avg_amount",
]

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
    # Raw user text, pre-resolution. The compiler never sees this - entity
    # resolution turns it into vendor_ids first (resolve/entities.py).
    vendor_query: str | None = None
    vendor_ids: list[str] = Field(default_factory=list)

    categories: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    funds: list[str] = Field(default_factory=list)
    programs: list[str] = Field(default_factory=list)
    payment_status: list[str] = Field(default_factory=list)
    reconciliation_status: list[str] = Field(default_factory=list)

    min_amount: Decimal | None = None
    max_amount: Decimal | None = None


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
    limit: Annotated[int, Field(ge=1, le=200)] = 20

    # Populated by the extractor, surfaced in the provenance panel.
    reasoning: str = ""
    ambiguities: list[str] = Field(default_factory=list)

    # ---- invariants -------------------------------------------------------

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


def extraction_schema() -> dict[str, Any]:
    """JSON Schema constraining LLM call #1. Flat and self-contained."""
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
            "intent": {"type": "string", "enum": list(get_args(Intent))},
            "metric": {"type": "string", "enum": list(get_args(Metric))},
            "group_by": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string", "enum": list(get_args(Dimension))},
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
                "properties": {
                    "vendor_query": {
                        "type": "string",
                        "description": (
                            "Company name ONLY. Leave absent for generic phrases "
                            "like 'vendor payouts', 'suppliers', 'top vendors'."
                        ),
                    },
                    "categories": strings,
                    "departments": strings,
                    "funds": strings,
                    "payment_status": strings,
                    "reconciliation_status": strings,
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


class SpecEvent(BaseModel):
    type: Literal["spec"] = "spec"
    spec: QuerySpec


class ClarifyOption(BaseModel):
    key: str
    label: str
    detail: str
    preview: str  # "$16,823,239,767.06 - 511,237 transactions"


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


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    recoverable: bool = True


Event = Annotated[
    StageEvent | SpecEvent | ClarifyEvent | SqlEvent | RowsEvent
    | TokenEvent | VerifiedEvent | NoteEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = None


class ClarifyRequest(BaseModel):
    thread_id: str
    ambiguity_id: str
    chosen_key: str


class Coverage(BaseModel):
    earliest: date
    latest: date
    transaction_count: int
    total_paid: Decimal
    currency: str = "USD"
