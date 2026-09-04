"""
Ambiguity resolution - detect, probe, decide.

The design rule, restated from AMBIGUITY.md: ambiguity is not a property of the
question, it is a property of the question *against this data*. "How much did we
spend last month" is ambiguous in principle (paid vs committed) but not in fact
if pending happens to be zero that month.

So we never ask the model whether something is ambiguous. We:

  1. detect candidate interpretations with deterministic rules
  2. probe - actually execute each one as a cheap scalar aggregate
  3. decide from the measured spread

    spread < 1%   -> answer silently
    1% - 10%      -> answer, disclose the assumption
    spread > 10%  -> ask, showing the real numbers for each option

Zero extra model calls. Probes are indexed aggregates run concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from api.compile import CompileError, compile_scalar
from api.config import settings
from api.db import Database
from api.registry import SemanticRegistry
from api.resolve.entities import VendorMatch, is_meaningful, resolve_vendor
from api.schema import ClarifyEvent, ClarifyOption, NoteEvent, QuerySpec

log = logging.getLogger("tbx.ambiguity")

Kind = Literal["temporal", "metric", "entity", "scope", "anchor"]


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------


@dataclass
class Interpretation:
    key: str
    label: str
    detail: str
    spec: QuerySpec
    value: Decimal | None = None
    rows: int | None = None

    def preview(self) -> str:
        if self.value is None:
            return "no result"
        n = f" · {self.rows:,} transactions" if self.rows is not None else ""
        return f"${self.value:,.2f}{n}"


@dataclass
class Ambiguity:
    kind: Kind
    trigger: str
    message: str
    interpretations: list[Interpretation]
    default_key: str
    is_money: bool = True
    # Whether this kind may BLOCK on the user, or may only disclose.
    #
    # Ask when there is no defensible default: "FY2026" genuinely has two
    # readings, "unreconciled" genuinely has two scopes, "bank of new york"
    # genuinely matches two companies of similar size.
    #
    # Disclose when a strong default exists. "Spend" means cash out in ordinary
    # finance usage; the committed figure is a useful footnote, not a question.
    # Making it a question was measurably wrong - retainage runs ~21% of paid in
    # this data, so it fired on essentially every spend question and buried the
    # real ambiguities in noise.
    can_ask: bool = True
    ambiguity_id: str = field(default_factory=lambda: f"amb_{uuid.uuid4().hex[:8]}")

    def _values(self) -> list[Decimal]:
        return [abs(i.value) for i in self.interpretations if i.value is not None]

    @property
    def spread(self) -> float:
        """Relative gap between the widest interpretations. 0.0 = identical."""
        vals = self._values()
        if len(vals) < 2:
            return 0.0
        hi = max(vals)
        return 0.0 if hi == 0 else float((hi - min(vals)) / hi)

    @property
    def absolute_gap(self) -> Decimal | None:
        """The gap in currency. Relative spread alone is not enough to decide:
        7.4% of $18B is $1.34 billion, which no finance team would want silently
        assumed, while 7.4% of $1,000 is noise. Both tests must be available."""
        vals = self._values()
        return (max(vals) - min(vals)) if len(vals) >= 2 else None

    def by_key(self, key: str) -> Interpretation | None:
        return next((i for i in self.interpretations if i.key == key), None)

    def to_event(self) -> ClarifyEvent:
        return ClarifyEvent(
            ambiguity_id=self.ambiguity_id,
            kind=self.kind,
            message=self.message.format(gap=_money_gap(self)),
            options=[
                ClarifyOption(key=i.key, label=i.label, detail=i.detail, preview=i.preview())
                for i in self.interpretations
            ],
            default_key=self.default_key,
        )

    def disclosure(self) -> NoteEvent:
        chosen = self.by_key(self.default_key)
        others = [i for i in self.interpretations if i.key != self.default_key]
        alt = others[0] if others else None
        text = f"Read as {chosen.label.lower()} ({chosen.preview()})."
        if alt is not None and alt.value is not None:
            text += f" {alt.label} would give {alt.preview()}."
        return NoteEvent(kind="assumption", text=text)


def _money_gap(a: Ambiguity) -> str:
    vals = [abs(i.value) for i in a.interpretations if i.value is not None]
    if len(vals) < 2:
        return "different amounts"
    return f"${max(vals) - min(vals):,.0f}"


@dataclass
class Decision:
    spec: QuerySpec
    clarify: Ambiguity | None = None
    notes: list[NoteEvent] = field(default_factory=list)
    detected: list[Ambiguity] = field(default_factory=list)

    @property
    def needs_user(self) -> bool:
        return self.clarify is not None


# --------------------------------------------------------------------------
# Detectors. Each returns None when it does not apply to this question/data.
# --------------------------------------------------------------------------

_FISCAL_RE = re.compile(r"\b(fy\s*\d{2,4}|fiscal(?:\s+year)?|financial\s+year)\b", re.I)
_SPEND_RE = re.compile(r"\b(spend|spent|spending|cost|costs|how much|paid out)\b", re.I)
_COMMITTED_RE = re.compile(r"\b(committed|commitment|owe[ds]?|outstanding|pending|"
                           r"retainage|accrued|liabilit)\w*\b", re.I)
_OPEN_RE = re.compile(r"\b(unreconciled|un-?matched|not reconciled|open items?|"
                      r"outstanding items?|still open)\b", re.I)


def detect_temporal(spec: QuerySpec, q: str, reg: SemanticRegistry) -> Ambiguity | None:
    """FY2026-the-budget-year vs FY2026-the-date-window. The $1.34B fork."""
    if not reg.has_fiscal_year or not reg.fiscal_year_start_month:
        return None
    if not _FISCAL_RE.search(q):
        return None

    fy = spec.fiscal_year
    if fy is None and spec.period is not None:
        m = re.search(r"\b(?:fy\s*)?((?:19|20)\d{2})\b", q, re.I)
        fy = m.group(1) if m else None
    if not fy:
        return None

    window = reg.fiscal_year_window(fy)
    if window is None:
        return None
    start, end = window

    by_column = spec.patched(date_basis="fiscal_year", fiscal_year=fy,
                            period=None, compare_period=None)
    by_window = spec.patched(
        date_basis="payment_date", fiscal_year=None, compare_fiscal_year=None,
        period={"start": start, "end": end, "label": f"{start:%b %Y} - {end:%b %Y}"},
    )

    return Ambiguity(
        kind="temporal",
        trigger=f"FY{fy}",
        message=f'"FY{fy}" has two readings here, and they differ by {{gap}}.',
        interpretations=[
            Interpretation(
                key="fiscal_year",
                label=f"The FY{fy} budget year",
                detail=f"Payments charged to FY{fy} appropriations, including any "
                       f"settled in later years",
                spec=by_column,
            ),
            Interpretation(
                key="payment_date",
                label=f"Payments made {start:%b %Y} - {end - _ONE_DAY:%b %Y}",
                detail="Money that actually left in that window, whatever budget "
                       "year it was charged to",
                spec=by_window,
            ),
        ],
        default_key="fiscal_year" if spec.date_basis == "fiscal_year" else "payment_date",
        is_money=spec.metric in _MONEY,
    )


def detect_metric(spec: QuerySpec, q: str, reg: SemanticRegistry) -> Ambiguity | None:
    """"Spend" = money that left, or money committed?"""
    if not reg.money_metric_split():
        return None
    if spec.metric not in ("amount_paid", "amount_total"):
        return None
    if not _SPEND_RE.search(q) or _COMMITTED_RE.search(q):
        return None  # explicit wording resolves it

    return Ambiguity(
        kind="metric",
        trigger="spend",
        message='"Spend" could mean money paid or money committed - a {gap} difference here.',
        interpretations=[
            Interpretation(
                key="amount_paid", label="Money paid out",
                detail="Cash that actually left, excluding pending and retainage",
                spec=spec.patched(metric="amount_paid"),
            ),
            Interpretation(
                key="amount_total", label="Total committed",
                detail="Paid plus pending plus retainage held",
                spec=spec.patched(metric="amount_total"),
            ),
        ],
        default_key="amount_paid",
        is_money=True,
        can_ask=False,   # strong default: "spend" means cash out
    )


def detect_scope(spec: QuerySpec, q: str, reg: SemanticRegistry) -> Ambiguity | None:
    """"Unreconciled" strictly, or every status that is not reconciled?"""
    if not reg.scope_is_ambiguous() or not _OPEN_RE.search(q):
        return None

    open_all = reg.open_recon_statuses()
    strict = [s for s in open_all if s.lower().startswith(("unrecon", "un-recon", "not recon"))]
    if not strict or set(strict) == set(open_all):
        return None

    return Ambiguity(
        kind="scope",
        trigger="unreconciled",
        message='"Unreconciled" can be read narrowly or broadly - a {gap} difference.',
        interpretations=[
            Interpretation(
                key="strict", label=f"Only {', '.join(strict)}",
                detail="Items with no bank record found at all",
                spec=spec.patched(filters=spec.filters.model_copy(
                    update={"reconciliation_status": strict}).model_dump()),
            ),
            Interpretation(
                key="broad", label="Everything not reconciled",
                detail=f"Includes {', '.join(s for s in open_all if s not in strict)}",
                spec=spec.patched(filters=spec.filters.model_copy(
                    update={"reconciliation_status": open_all}).model_dump()),
            ),
        ],
        default_key="strict",
        is_money=spec.metric in _MONEY,
    )


def detect_entity(spec: QuerySpec, match: VendorMatch | None) -> Ambiguity | None:
    """Which vendor did they mean? Fires when no single entity dominates."""
    if match is None or len(match.candidates) < 2:
        return None
    if match.dominance() >= 0.85:
        return None  # one vendor holds nearly all the spend; picking it is safe

    top = match.candidates[0]
    everyone = match.candidates
    return Ambiguity(
        kind="entity",
        trigger=match.query,
        message=f'"{match.query}" matches {len(everyone)} vendors, '
                f"differing by {{gap}}.",
        interpretations=[
            Interpretation(
                key="top", label=top.vendor_name,
                detail=f"{top.txn_count:,} transactions on record",
                spec=spec.patched(filters=spec.filters.model_copy(
                    update={"vendor_ids": [top.vendor_id], "vendor_query": None}).model_dump()),
            ),
            Interpretation(
                key="all", label=f"All {len(everyone)} matching vendors",
                detail=", ".join(c.vendor_name for c in everyone[:4]),
                spec=spec.patched(filters=spec.filters.model_copy(
                    update={"vendor_ids": [c.vendor_id for c in everyone],
                            "vendor_query": None}).model_dump()),
            ),
        ],
        default_key="top",
        is_money=spec.metric in _MONEY,
    )


_ONE_DAY = __import__("datetime").timedelta(days=1)
_MONEY = {"amount_paid", "amount_total", "amount_pending", "amount_retainage", "avg_amount"}


# --------------------------------------------------------------------------
# Resolver
# --------------------------------------------------------------------------


class AmbiguityResolver:
    def __init__(self, db: Database, reg: SemanticRegistry) -> None:
        self.db = db
        self.reg = reg
        # Materiality floor, scaled to the dataset rather than hardcoded, so a
        # smaller ledger tomorrow gets a proportionally smaller threshold.
        self.material_amount = (
            abs(reg.total_paid) * Decimal(str(settings.ambiguity_material_fraction))
        )

    def _is_material(self, a: Ambiguity) -> bool:
        """Ask when EITHER test trips: a large relative difference, or a large
        absolute one. Percentage alone misses the $1.34B-at-7.4% case."""
        if not a.can_ask:
            return False
        if a.spread >= settings.ambiguity_disclose:
            return True
        if a.is_money and a.absolute_gap is not None:
            return a.absolute_gap >= self.material_amount
        return False

    async def resolve(
        self, spec: QuerySpec, question: str, settled: dict[str, str] | None = None
    ) -> Decision:
        settled = settled or {}

        # Entity resolution happens first: it rewrites the spec (vendor_query ->
        # vendor_ids) and its candidate list feeds the entity detector.
        spec, match = await self._resolve_entities(spec)

        # Apply previously-settled choices BEFORE detecting the rest. Detectors
        # build their interpretations from the spec they are given, so running
        # them against the pre-settlement spec would offer the user options
        # computed on a basis they already rejected - e.g. after choosing the
        # payment-date reading of FY2026, the paid-vs-committed options would
        # still have been priced on the fiscal-year column.
        spec = self._apply_settled(spec, question, match, settled)

        found = [a for a in self._detect_all(spec, question, match)
                 if not self._settled_choice(a, settled)]

        if not found:
            return Decision(spec=spec)

        await self._probe_all(found)

        material = [a for a in found if self._is_material(a)]
        if material:
            worst = max(material, key=lambda a: (a.spread, a.absolute_gap or 0))
            log.info("clarify %s (%s) spread=%.1f%% gap=%s", worst.kind, worst.trigger,
                     worst.spread * 100, worst.absolute_gap)
            return Decision(spec=spec, clarify=worst, detected=found)

        notes = [a.disclosure() for a in found if a.spread >= settings.ambiguity_silent]
        for a in found:
            chosen = a.by_key(a.default_key)
            if chosen is not None:
                spec = chosen.spec
        log.info("proceeding with %d disclosure(s)", len(notes))
        return Decision(spec=spec, notes=notes, detected=found)

    # ---- internals ----

    def _detect_all(
        self, spec: QuerySpec, question: str, match: VendorMatch | None
    ) -> list[Ambiguity]:
        return [a for a in (
            detect_temporal(spec, question, self.reg),
            detect_metric(spec, question, self.reg),
            detect_scope(spec, question, self.reg),
            detect_entity(spec, match),
        ) if a is not None]

    @staticmethod
    def _settled_choice(a: Ambiguity, settled: dict[str, str]) -> str | None:
        """Scoped to (kind, trigger) first: settling one vendor must not settle
        a different one. A bare kind is the session-wide fallback, which is
        appropriate for temporal and metric preferences."""
        return settled.get(f"{a.kind}:{a.trigger}") or settled.get(a.kind)

    def _apply_settled(
        self, spec: QuerySpec, question: str, match: VendorMatch | None,
        settled: dict[str, str],
    ) -> QuerySpec:
        """Fold every remembered choice into the spec.

        Exactly one choice is applied per pass, then detection re-runs. Applying
        several in one pass is wrong: each Interpretation.spec is derived from
        the spec the detector was handed, so a second application would be built
        from the pre-settlement spec and would silently clobber the first. That
        showed up as the temporal choice being undone by the metric choice.
        """
        for _ in range(len(settled) + 2):
            applied = False
            for a in self._detect_all(spec, question, match):
                key = self._settled_choice(a, settled)
                if not key:
                    continue
                chosen = a.by_key(key)
                if chosen is not None and chosen.spec != spec:
                    spec = chosen.spec
                    applied = True
                    break            # re-detect against the updated spec
            if not applied:
                break
        return spec

    async def _resolve_entities(self, spec: QuerySpec) -> tuple[QuerySpec, VendorMatch | None]:
        vq = spec.filters.vendor_query
        if not is_meaningful(vq):
            if vq:
                log.info("dropping generic vendor_query %r", vq)
                spec = spec.patched(filters=spec.filters.model_copy(
                    update={"vendor_query": None}).model_dump())
            return spec, None

        match = await resolve_vendor(vq, self.db)
        if not match.candidates:
            return spec, match

        spec = spec.patched(filters=spec.filters.model_copy(
            update={"vendor_ids": match.ids(all_candidates=False),
                    "vendor_query": vq}).model_dump())
        return spec, match

    async def _probe_all(self, ambiguities: list[Ambiguity]) -> None:
        """Execute every interpretation concurrently. Cheap: indexed aggregates."""
        tasks = [self._probe(i) for a in ambiguities for i in a.interpretations]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe(self, interp: Interpretation) -> None:
        try:
            cq = compile_scalar(interp.spec, self.reg)
            row = await self.db.fetchone(cq.sql, cq.params)
            if row:
                interp.value = row.get("value")
                interp.rows = row.get("n")
        except (CompileError, Exception) as exc:  # noqa: BLE001
            log.warning("probe failed for %s: %s", interp.key, exc)
