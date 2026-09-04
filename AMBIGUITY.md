# Ambiguity Resolution

Design note for the TBX finance assistant. Companion to [PLAN.md](PLAN.md) §4, Layer 3.

---

## The two failure modes

Every team building this hits one of two walls:

**Under-asking.** The assistant picks an interpretation silently and states a confident number. When the judge asks *"how much did we spend on FY2026?"* and gets `$16.8B` with no caveat, the other reading — `$18.2B` — is equally defensible. The answer isn't wrong so much as **unaccountable**, and that is precisely what the 30% grounding criterion punishes.

**Over-asking.** The assistant asks "did you mean X or Y?" on every question. Demos badly, feels broken, and buries the real ambiguities in noise.

Both come from the same mistake: **treating ambiguity as a property of the question**. It isn't. It's a property of *the question against this data*.

> "How much did we spend last month?" is ambiguous in principle — paid vs. committed. If pending and retainage happen to be $0 that month, it is not ambiguous **in fact**, and asking is pure friction.

---

## Core principle: measure the impact, then decide

**Don't ask the LLM whether something is ambiguous.** That judgment is inconsistent run-to-run, un-testable, and burns a model call.

Instead:

1. **Detect** candidate interpretations with deterministic rules
2. **Probe** — actually execute each interpretation as a cheap scalar aggregate
3. **Compare** the results
4. **Decide** by measured impact, not by guesswork

```
spread < 1%     →  answer silently. The distinction doesn't matter here.
1% – 10%        →  answer with the default, disclose the assumption inline.
spread > 10%    →  ask — and show the actual numbers for each option.
```

This is **impact-weighted clarification**, and it kills both failure modes at once. You never nag about distinctions that don't move the number, and you never silently absorb one that does.

### Two corrections found while implementing this

Both were errors in the policy as first written. Both are now in the code.

**1. Relative spread alone is the wrong test.** The FY2026 fork measures a 7.4%
spread — *below* the 10% ask threshold — but that 7.4% is **$1,337,116,299**. No
finance team wants a billion-dollar interpretation silently assumed, while 7.4% of
$1,000 is genuinely noise. So there are two independent triggers, and either one asks:

```python
if a.spread >= 0.10:                       # large relative difference
    ask
if a.is_money and a.absolute_gap >= material_amount:   # large absolute one
    ask
```

`material_amount` is 0.5% of total spend in the loaded dataset — $171,660,112 here —
so it scales with whatever data arrives tomorrow rather than being a hardcoded figure.

**2. Not every ambiguity may block.** Making "spend" a question was measurably wrong.
Retainage runs ~21% of paid in this data, so the paid-vs-committed detector fired on
essentially *every* spend question — the over-asking failure mode, burying the real
ambiguities in noise. But "spend" means cash out in ordinary finance usage; it has a
defensible default.

So each kind carries `can_ask`:

| Kind | May ask? | Why |
|---|---|---|
| temporal (FY vs window) | **yes** | No defensible default — both readings are legitimate |
| scope (unreconciled narrow vs broad) | **yes** | Both readings are legitimate |
| entity (which vendor) | **yes** | Picking one of two similarly-sized companies is a coin flip |
| metric (paid vs committed) | **no — disclose only** | "Spend" means cash out; the committed figure is a footnote |

The rule generalises: **ask when there is no defensible default; disclose when there
is one.**

The probes are cheap: one indexed aggregate each, ~20–200 ms, run concurrently. Two model calls per question stays two.

---

## Taxonomy — five classes, with measured impact

Every figure below is real, measured against the current 1,019,354-row build.

### A. Temporal basis — `fiscal_year` column vs. date window

Triggered by: `FY\d{4}`, "fiscal", "financial year", "last year", "this year".

| Reading | Transactions | Total paid |
|---|---|---|
| `WHERE fiscal_year = '2026'` | 511,237 | $16,823,239,767.06 |
| `transaction_date` in Jul 2025 – Jun 2026 | 519,233 | $18,160,356,066.39 |

**Spread: 7.4% — $1,337,116,299.** *Below* the 10% relative threshold, but far above the
absolute materiality floor, so it asks. This is the case that proved percentage alone is
insufficient. Caused by 17,747 back-dated rows: recent payments settling obligations booked
as far back as FY2018.

### B. Metric semantics — paid vs. committed

Triggered by: bare "spend", "spent", "cost", "how much".

| Reading | August 2026 |
|---|---|
| `SUM(amount_paid)` — money that left | $1,068,521,181.57 |
| `SUM(amount_total)` — incl. pending + retainage | $1,355,120,029.45 |

**Spread: 21.1%** (measured as gap/max; the same figures read as +26.8% against paid).
Data-dependent — in a month with no retainage it collapses to 0% and the resolver goes
silent. But this kind is **disclose-only**: see correction 2 above.

### C. Entity resolution — which vendor

Triggered by: any free-text vendor string.

```
"mckesson"  →  MCKESSON CORPORATION               $163,143,519.84   (110,910 txns)
               MCKESSON PLASMA AND BIOLOGICS LLC   $91,501,889.78   ( 11,795 txns)
               MCKESSON MEDICAL-SURGICAL INC        $1,168,918.75   (    571 txns)
```

Top match alone $163,143,519.84 vs all three combined $254,938,097.04 — **spread 36.0%**.

The nastier case:

```
"bank of new york"  →  THE BANK OF NEW YORK MELLON            $1,413,058,513.49   sim 0.607
                       THE BANK OF NEW YORK MELLON TRUST CO NA $1,427,918,562.87   sim 0.447
```

Two near-identical entities of near-identical size. Measured against the full candidate
set the resolver finds (6 vendors matching that string): **spread 49.9%, gap $1,424,753,870.** Note the trap: the *higher* similarity score belongs to the *smaller* vendor. Ranking by `similarity()` alone silently picks a coin-flip on $1.4B. This case alone justifies the whole mechanism.

### D. Scope — how wide is a status word

Triggered by: "unreconciled", "open", "outstanding", "pending", "unmatched".

| Reading | Transactions | Exposure |
|---|---|---|
| Strict — `status = 'Unreconciled'` | 35,395 | $3,305,352,559.84 |
| Broad — `status <> 'Reconciled'` | 56,969 | $4,623,134,976.38 |

**Spread: 28.5%** — $1,317,782,417. "Still unreconciled" is one of the two reference questions in the problem statement, so this one *will* be tested.

### E. Relative-date anchor

Triggered by: "last month", "this quarter", "YTD", "recently".

Not a clarify — a **disclosure**. The anchor is `min(today, max(transaction_date))`, never wall-clock alone. If the resolved window is only partly covered by the data, say so:

> *"August 2026 — note the data ends 2026-08-29, so this covers 29 of 31 days."*

Anchoring naively to wall-clock is how you end up confidently reporting a near-empty current month.

---

## Architecture

```
QuerySpec + raw question + session
        │
        ▼
   ┌─────────────────────────────────────────┐
   │  Detectors (deterministic, no LLM)      │
   │  A temporal · B metric · C entity       │
   │  D scope    · E anchor                  │
   └──────────────┬──────────────────────────┘
                  │  list[Ambiguity], each with 2+ Interpretations
                  ▼
   ┌─────────────────────────────────────────┐
   │  Probe — execute each interpretation     │
   │  as a scalar aggregate, concurrently     │
   └──────────────┬──────────────────────────┘
                  │  each Interpretation now has a real value
                  ▼
   ┌─────────────────────────────────────────┐
   │  Policy — spread vs. thresholds          │
   └──────────────┬──────────────────────────┘
                  │
     ┌────────────┼────────────────┐
     ▼            ▼                ▼
  Proceed    Proceed+disclose   Clarify (with numbers)
```

### Types

```python
@dataclass
class Interpretation:
    key: str                      # "fiscal_year"
    label: str                    # "The FY2026 budget year"
    detail: str                   # one line the user can act on
    spec: QuerySpec               # a full, executable variant
    value: Decimal | None = None  # filled by probe
    rows: int | None = None


@dataclass
class Ambiguity:
    kind: Literal["temporal", "metric", "entity", "scope", "anchor"]
    trigger: str                          # the span that fired the detector
    interpretations: list[Interpretation]
    default_key: str

    @property
    def spread(self) -> float:
        """Relative gap between the widest interpretations. 0.0 = identical."""
        vals = [abs(i.value) for i in self.interpretations if i.value is not None]
        if len(vals) < 2:
            return 0.0
        hi = max(vals)
        return 0.0 if hi == 0 else float((hi - min(vals)) / hi)
```

### The resolver

```python
class AmbiguityResolver:
    SILENT   = 0.01   # below this, the distinction is noise
    DISCLOSE = 0.10   # below this, answer but state the assumption

    def __init__(self, registry: SemanticRegistry, db: Database):
        self.detectors = [TemporalBasis(registry), MetricSemantics(registry),
                          EntityResolution(registry), ScopeWidth(registry),
                          RelativeAnchor(registry)]
        self.db = db

    async def resolve(self, spec, question, session) -> Decision:
        found = [a for d in self.detectors
                 if (a := d.detect(spec, question))
                 and not session.already_settled(a.kind, a.trigger)]

        if not found:
            return Proceed(spec)

        # Probe every interpretation concurrently — cheap indexed aggregates
        await asyncio.gather(*(self._probe(i)
                               for a in found for i in a.interpretations))

        worst = max(found, key=lambda a: a.spread)
        if worst.spread >= self.DISCLOSE:
            return Clarify(worst, others=[a for a in found if a is not worst])

        notes = [a.disclosure() for a in found if a.spread >= self.SILENT]
        return Proceed(spec, notes=notes)

    async def _probe(self, interp: Interpretation) -> None:
        sql, params = compile_scalar(interp.spec)   # strips GROUP BY / ORDER / LIMIT
        row = await self.db.fetchone(sql, params)
        interp.value, interp.rows = row["value"], row["n"]
```

`compile_scalar` reuses the main compiler with grouping stripped — one code path, so a probe can never disagree with the answer it is predicting.

---

## What the user actually sees

The clarify payload carries **the numbers**, not just labels. This is the whole UX trick — the user recognises their intent instantly instead of parsing jargon.

```json
{
  "type": "clarify",
  "message": "\"FY2026\" has two readings here, and they differ by $1.34 billion.",
  "options": [
    {
      "key": "fiscal_year",
      "label": "The FY2026 budget year",
      "detail": "Payments charged to FY2026 appropriations — including some settled in later years",
      "preview": "$16,823,239,767.06 · 511,237 transactions"
    },
    {
      "key": "payment_date",
      "label": "Payments made Jul 2025 – Jun 2026",
      "detail": "Money that actually left in that window, whatever budget year it was charged to",
      "preview": "$18,160,356,066.39 · 519,233 transactions"
    }
  ],
  "default": "payment_date"
}
```

Render as two buttons. One click patches the spec and re-runs — no second extraction call.

For the disclose tier, no buttons — just a line under the answer:

> *Counted as money paid ($1.07B). Including $286.6M pending and retainage would give $1.36B.*

---

## Sticky resolution

Once the user picks, **remember it for the session** — `session.settle(kind, trigger, key)`. Asking the same question twice in one demo is worse than not asking at all.

Scope it per `(kind, trigger)`, not globally: settling "mckesson → MCKESSON CORPORATION" must not also settle "bank of new york". Temporal and metric choices are safe to settle session-wide, since they're stable user preferences rather than per-entity facts.

---

## Surviving tomorrow's dataset swap

This is the part that matters for building today. **No detector may hardcode a literal from our SF data.** Everything comes from a `SemanticRegistry` built at startup:

```python
class SemanticRegistry:
    """Everything the detectors need, derived from the live DB at boot."""

    @classmethod
    async def build(cls, db) -> "SemanticRegistry":
        return cls(
            coverage        = await db.fetchone("SELECT * FROM v_data_coverage"),
            recon_statuses  = await db.column("SELECT DISTINCT reconciliation_status FROM reconciliation"),
            payment_statuses= await db.column("SELECT DISTINCT payment_status FROM transactions"),
            categories      = await db.column("SELECT DISTINCT category_name FROM chart_of_accounts"),
            departments     = await db.column("SELECT DISTINCT department_name FROM departments"),
            money_columns   = await db.money_columns("transactions"),
            has_fiscal_year = await db.has_column("transactions", "fiscal_year"),
        )
```

Then each detector degrades gracefully:

- **Temporal basis** — only arms if `has_fiscal_year` is true *and* a probe shows the two readings actually diverge on this data. If tomorrow's dataset has no fiscal-year column, the detector silently disables itself.
- **Scope width** — "unreconciled" maps to whichever of `recon_statuses` are not the reconciled one. If tomorrow's data has only two statuses, strict and broad coincide, spread is 0%, and the resolver stays quiet without any code change.
- **Metric semantics** — reads `money_columns`; the paid/committed split exists only if more than one money column is present.
- **Entity resolution** — pure `pg_trgm` against whatever vendor table exists.

Trigger phrases ("unreconciled", "outstanding", "spend") live in **`config/triggers.yaml`**, not in Python. Tomorrow that file is the only thing you may need to touch, and it takes minutes.

**Build today, in this order:**

1. `SemanticRegistry.build()` against our Postgres — proves the introspection works
2. The five detectors, each against the registry only
3. `compile_scalar` sharing the main compiler
4. The policy + thresholds
5. Test fixtures pinning all four measured spreads above

Tomorrow: point at the new DB, run the fixtures, adjust `triggers.yaml`. The mechanism is dataset-independent by construction.

---

## Testing

Each ambiguity class gets three fixtures:

| Fixture | Asserts |
|---|---|
| **Fires** | The detector arms and spread exceeds the ask threshold — e.g. "FY2026" → clarify |
| **Doesn't fire** | Same phrasing, data where readings coincide → silent. E.g. a month with zero pending → "spend" is unambiguous |
| **Disclosed** | Spread between 1% and 10% → answers, and the note names the assumption |

The middle row is the one teams skip, and it's the one that proves you aren't just always asking. Include it in the demo: run the same question against two different months, show it asking on one and answering directly on the other. That single contrast makes the whole mechanism legible to a judge in about ten seconds.

Add the ambiguity cases to the 50-question eval set graded on **behaviour, not numbers** — did it ask, disclose, or proceed correctly? PLAN.md §7 already reserves 5 slots for these; make them the four measured cases above plus one that must *not* fire.

---

## Cost

| | Per ambiguous question |
|---|---|
| Extra model calls | **0** — detection and policy are pure Python |
| Extra SQL | 2–6 scalar aggregates, run concurrently |
| Added latency | ~50–200 ms typical |

Ambiguity handling costs no tokens. Worth stating plainly in the deck — it's the cheapest 30%-criterion insurance in the build.
