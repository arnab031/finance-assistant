"""
Dataset profiles.

The pipeline - extract, validate, resolve, compile, execute, narrate, verify -
is domain-independent. What is NOT domain-independent is the vocabulary: which
dimensions exist, which metrics mean what, how a filter becomes a WHERE clause,
where the counterparty name lives.

A Profile holds exactly that, and nothing else. Switching datasets is then a
config value rather than a fork, which matters because the organizers' schema
turned out to be a different domain (bank statements) rather than a differently
shaped version of the same one (vendor payments).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Dim:
    sql: str                 # expression, e.g. "t.vendor_name"
    alias: str               # output column name
    join: str | None = None  # join key required to use it


@dataclass(frozen=True)
class Filt:
    sql: str
    join: str | None = None
    # The kind decides BOTH how the field is compiled and how it is declared in
    # the extraction schema, and the two must agree:
    #   "list"   -> IN (...), declared as an array of strings
    #   "text"   -> LIKE,     declared as a single string
    #   "number" -> a bound on the amount column, declared as a number. These
    #               are compiled by _where's dedicated amount-bounds block, not
    #               by its per-filter loop.
    # When the schema and the compiler disagree the failure is silent: the model
    # simply cannot emit the field, so the filter never applies and the answer
    # comes back confidently unfiltered. That is how counterparty_like went
    # unpopulated on every question, and how "debits over 50000" returned every
    # debit - min_amount was compilable but undeclared.
    kind: str = "list"
    hint: str = ""


@dataclass(frozen=True)
class SemanticSource:
    """One vocabulary worth embedding.

    `sql` must yield exactly two columns, entity_key and label. entity_key is
    what the SQL compiler will filter on, so a resolved match can become a WHERE
    clause; label is the text that gets embedded. They are usually the same
    string, and are separate for the case where they are not.
    """
    entity_type: str
    sql: str
    note: str = ""


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    database: str

    # ---- shape ----
    fact: str                              # FROM clause, e.g. "transactions t"
    alias: str                             # "t"
    date_column: str                       # fully qualified
    dimensions: dict[str, Dim]
    metrics: dict[str, str]
    money_metrics: frozenset[str]
    filters: dict[str, Filt]
    joins: dict[str, str]
    list_columns: list[str]                # columns for intent="list"
    time_dimensions: frozenset[str] = frozenset({"month", "quarter"})

    # ---- registry: how to learn what is in the data ----
    coverage_sql: str = ""
    vocab_sql: dict[str, str] = field(default_factory=dict)
    capability_sql: dict[str, str] = field(default_factory=dict)
    entity_count_sql: str = ""      # "how many vendors / accounts"
    money_columns_table: str = ""   # table to introspect for money columns

    # ---- entity resolution ----
    banks: list[str] = field(default_factory=list)
    entity_kind: str = "vendor"            # what a free-text name refers to
    entity_sql: str = ""                   # candidate lookup

    # ---- prompting ----
    # Starter chips and the composer placeholder. Hardcoding these in the UI
    # meant the chat advertised vendors, categories and FY2026 on a dataset with
    # none of them - an invitation to ask questions that cannot be answered.
    suggestions: list[str] = field(default_factory=list)
    placeholder: str = "Ask about your financial data…"

    prompt_rules: str = ""
    fewshot: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    unsupported_note: str = ""

    # ---- optional intents this dataset cannot support ----
    disabled_intents: frozenset[str] = frozenset()

    # (pattern, reason) pairs naming domains the schema does not contain. A
    # question matching one is forced to intent="unsupported" deterministically,
    # because a 7B model treats "we have no vendors" as a suggestion: asked
    # about "vendor payouts" it put "vendor" in counterparty_like and answered
    # with every debit in the month. Declining is the only honest answer, so it
    # cannot be left to the model's discretion.
    #
    # This lives on the PROFILE, not in shared code: "vendor" is an absent
    # concept on bank statements and the central one on vendor payments.
    absent_concepts: tuple[tuple[str, str], ...] = ()

    # Vocabularies the semantic index is built from, in api/semantic.py. Empty
    # means this dataset has nothing worth embedding, which is a real answer
    # rather than a gap - most columns here are enums or ids, and embedding
    # those adds cost without adding recall.
    semantic_sources: tuple[SemanticSource, ...] = ()

    def dimension_names(self) -> list[str]:
        return list(self.dimensions)

    def metric_names(self) -> list[str]:
        return list(self.metrics)


_ACTIVE: Profile | None = None


def get_profile() -> Profile:
    global _ACTIVE
    if _ACTIVE is None:
        from api.profiles.bank_txn import PROFILE

        _ACTIVE = PROFILE
    return _ACTIVE


def set_profile(p: Profile) -> None:
    """For tests and for switching datasets without a restart."""
    global _ACTIVE
    _ACTIVE = p
