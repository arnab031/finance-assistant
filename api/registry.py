"""
SemanticRegistry - everything the system knows about the data, learned by
introspecting the live database at startup.

This module is the reason tomorrow's dataset swap is a 2-hour job rather than a
rewrite. NOTHING anywhere else may hardcode a category name, a status value, a
date range, or a column list. If a component needs to know what's in the data,
it asks the registry.

Built once in the FastAPI lifespan and treated as immutable thereafter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from api.db import Database

log = logging.getLogger("tbx.registry")

# Columns we will not offer as filterable vocabularies even if they are text.
_SKIP_VOCAB = {"transaction_id", "voucher_id", "vendor_id", "contract_title",
               "purchase_order", "contract_number"}


@dataclass(frozen=True)
class SemanticRegistry:
    # ---- coverage ----
    earliest: date
    latest: date
    transaction_count: int
    total_paid: Decimal

    # ---- vocabularies (closed sets the extractor may choose from) ----
    categories: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)
    funds: list[str] = field(default_factory=list)
    fund_types: list[str] = field(default_factory=list)
    programs: list[str] = field(default_factory=list)
    payment_statuses: list[str] = field(default_factory=list)
    recon_statuses: list[str] = field(default_factory=list)
    fiscal_years: list[str] = field(default_factory=list)

    # ---- capability flags: which detectors and dimensions may arm ----
    has_fiscal_year: bool = False
    has_reconciliation: bool = False
    has_payouts: bool = False
    money_columns: list[str] = field(default_factory=list)

    # ---- derived semantics ----
    reconciled_label: str | None = None      # which status means "matched"
    vendor_count: int = 0
    # Month a fiscal year starts (1-12), inferred from the data - never assumed.
    # SF is 7 (July). A dataset on an Apr-Mar or Jan-Dec year yields 4 or 1 and
    # the temporal detector adapts with no code change.
    fiscal_year_start_month: int | None = None

    def fiscal_year_window(self, fy: str) -> tuple[date, date] | None:
        """The [start, end) calendar window a fiscal-year label corresponds to.

        This is the *other* reading in the temporal ambiguity: FY2026 as a date
        range rather than as the budget-year column.
        """
        if not self.fiscal_year_start_month:
            return None
        try:
            year = int(fy)
        except (TypeError, ValueError):
            return None
        m = self.fiscal_year_start_month
        if m == 1:                       # calendar-year fiscal year
            return date(year, 1, 1), date(year + 1, 1, 1)
        # FY2026 starting in July runs Jul 2025 -> Jul 2026
        return date(year - 1, m, 1), date(year, m, 1)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    async def build(cls, db: Database) -> "SemanticRegistry":
        cov = await db.fetchone(
            """SELECT MIN(transaction_date) AS earliest,
                      MAX(transaction_date) AS latest,
                      COUNT(*)              AS n,
                      COALESCE(SUM(amount_paid), 0) AS paid
               FROM transactions"""
        )
        if not cov or cov["n"] == 0:
            raise RuntimeError("transactions table is empty - load data first")

        has_fy = await db.has_column("transactions", "fiscal_year")
        has_recon = await db.has_table("reconciliation")
        has_payouts = await db.has_table("vendor_payouts")

        money = [c for c in await db.numeric_columns("transactions")
                 if c.startswith("amount_")]

        recon_statuses: list[str] = []
        if has_recon:
            recon_statuses = await db.column(
                "SELECT DISTINCT reconciliation_status FROM reconciliation "
                "WHERE reconciliation_status IS NOT NULL ORDER BY 1"
            )

        reg = cls(
            earliest=cov["earliest"],
            latest=cov["latest"],
            transaction_count=cov["n"],
            total_paid=cov["paid"],
            categories=await db.column(
                "SELECT DISTINCT category_name FROM transactions "
                "WHERE category_name IS NOT NULL ORDER BY 1"),
            departments=await db.column(
                "SELECT DISTINCT department_name FROM transactions "
                "WHERE department_name IS NOT NULL ORDER BY 1"),
            funds=await db.column(
                "SELECT DISTINCT fund_name FROM transactions "
                "WHERE fund_name IS NOT NULL ORDER BY 1"),
            fund_types=await db.column(
                "SELECT DISTINCT fund_type_name FROM funds "
                "WHERE fund_type_name IS NOT NULL ORDER BY 1"),
            programs=await db.column(
                "SELECT DISTINCT program_name FROM transactions "
                "WHERE program_name IS NOT NULL ORDER BY 1"),
            payment_statuses=await db.column(
                "SELECT DISTINCT payment_status FROM transactions "
                "WHERE payment_status IS NOT NULL ORDER BY 1"),
            recon_statuses=recon_statuses,
            fiscal_years=(await db.column(
                "SELECT DISTINCT fiscal_year FROM transactions "
                "WHERE fiscal_year IS NOT NULL ORDER BY 1") if has_fy else []),
            has_fiscal_year=has_fy,
            has_reconciliation=has_recon,
            has_payouts=has_payouts,
            money_columns=money,
            reconciled_label=_detect_reconciled(recon_statuses),
            vendor_count=await db.scalar("SELECT COUNT(*) FROM vendors") or 0,
            fiscal_year_start_month=(await _detect_fy_start(db)) if has_fy else None,
        )
        log.info(
            "registry: %s..%s  %s txns  %s vendors  %s categories  %s recon statuses "
            "(matched=%r)  fiscal_year=%s",
            reg.earliest, reg.latest, f"{reg.transaction_count:,}",
            f"{reg.vendor_count:,}", len(reg.categories), len(reg.recon_statuses),
            reg.reconciled_label, reg.has_fiscal_year,
        )
        return reg

    # ------------------------------------------------------------------
    # Queries the rest of the system asks
    # ------------------------------------------------------------------

    def covers(self, start: date, end: date) -> bool:
        """True if any part of [start, end) falls inside the data window."""
        return not (end <= self.earliest or start > self.latest)

    def coverage_fraction(self, start: date, end: date) -> float:
        """How much of the requested window the data actually spans (0.0-1.0)."""
        lo, hi = max(start, self.earliest), min(end, self.latest)
        span = (end - start).days
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, ((hi - lo).days + 1) / span))

    def open_recon_statuses(self) -> list[str]:
        """Every status that is not the 'matched' one - the broad reading of
        'unreconciled'. Empty when the dataset has no reconciliation layer."""
        if not self.reconciled_label:
            return list(self.recon_statuses)
        return [s for s in self.recon_statuses if s != self.reconciled_label]

    def scope_is_ambiguous(self) -> bool:
        """'Unreconciled' has a strict and a broad reading only if there is more
        than one open status. With 2 statuses total they coincide, and the
        scope detector must stay silent - no code change needed."""
        return len(self.open_recon_statuses()) > 1

    def money_metric_split(self) -> bool:
        """paid vs committed is only a real distinction if both exist."""
        return len({"amount_paid", "amount_total"} & set(self.money_columns)) == 2

    def vocabulary(self, kind: str) -> list[str]:
        return {
            "category": self.categories,
            "department": self.departments,
            "fund": self.funds,
            "fund_type": self.fund_types,
            "program": self.programs,
            "payment_status": self.payment_statuses,
            "reconciliation_status": self.recon_statuses,
            "fiscal_year": self.fiscal_years,
        }.get(kind, [])

    def prompt_context(self) -> str:
        """The data facts injected into LLM call #1. Kept short - every token
        here is paid on every question."""
        def cap(xs: list[str], n: int = 40) -> str:
            return ", ".join(xs[:n]) + (f", ... (+{len(xs) - n} more)" if len(xs) > n else "")

        lines = [
            f"Data coverage: {self.earliest} to {self.latest} "
            f"({self.transaction_count:,} transactions).",
            f"Categories: {cap(self.categories)}",
            f"Payment statuses: {cap(self.payment_statuses)}",
        ]
        if self.has_reconciliation:
            lines.append(f"Reconciliation statuses: {cap(self.recon_statuses)}")
        if self.has_fiscal_year and self.fiscal_years:
            lines.append(
                f"Fiscal years present: {self.fiscal_years[0]}-{self.fiscal_years[-1]} "
                f"(a fiscal year is the budget year charged, not a date range)"
            )
        lines.append(f"Departments ({len(self.departments)}): {cap(self.departments, 25)}")
        return "\n".join(lines)


async def _detect_fy_start(db: Database) -> int | None:
    """Infer which calendar month a fiscal year begins in.

    Method: for each calendar month, take the fiscal year that dominates it,
    then find the months where that dominant value increments. Those are the
    fiscal-year boundaries; their modal month is the answer.

    The obvious approach - earliest month seen per fiscal year - does not work.
    A data extract rarely starts on a fiscal boundary, so the first month
    present reflects the extract window, not the fiscal calendar. (Measured: it
    returned February for SF data that is unambiguously July.) Back-dated rows
    make it worse. Looking at transitions is immune to both.
    """
    rows = await db.fetch(
        """
        WITH per_month AS (
            SELECT date_trunc('month', transaction_date) AS mon_start,
                   fiscal_year, COUNT(*) AS n
            FROM transactions
            WHERE fiscal_year IS NOT NULL
            GROUP BY 1, 2
        ),
        modal AS (                       -- the fiscal year owning each month
            SELECT DISTINCT ON (mon_start) mon_start, fiscal_year
            FROM per_month ORDER BY mon_start, n DESC
        ),
        transitions AS (
            SELECT mon_start, fiscal_year,
                   LAG(fiscal_year) OVER (ORDER BY mon_start) AS prev_fy
            FROM modal
        )
        SELECT EXTRACT(MONTH FROM mon_start)::int AS mon, COUNT(*) AS hits
        FROM transitions
        WHERE prev_fy IS NOT NULL AND fiscal_year <> prev_fy
        GROUP BY 1 ORDER BY hits DESC, mon LIMIT 1
        """
    )
    return rows[0]["mon"] if rows else None


def _detect_reconciled(statuses: list[str]) -> str | None:
    """Find the status meaning 'matched to the bank', without hardcoding it.

    Deliberately careful about the Unreconciled/Reconciled pair: a naive
    substring test matches both.
    """
    if not statuses:
        return None
    lowered = {s.lower(): s for s in statuses}
    for exact in ("reconciled", "matched", "cleared", "settled"):
        if exact in lowered:
            return lowered[exact]
    for low, original in lowered.items():
        if "reconcil" in low and not low.startswith(("un", "not", "partial")):
            return original
    return None
