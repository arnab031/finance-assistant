# Finsight — submission deck

Full content of **[Finsight_Deck.pptx](Finsight_Deck.pptx)**, slide by slide: every line of on-slide
copy, the layout, and what to say over it. Sourced from [README.md](README.md),
[architecture.html](architecture.html), and the vLLM serving benchmark.

15 slides · 16:9 · ~8–10 minutes at a normal pace.

**Running order at a glance**

| # | Slide | The one thing it proves |
|---|---|---|
| 1 | Finsight | — |
| 2 | Ask in plain language. Get a figure you can trace. | It works, end to end |
| 3 | We deliberately did not build text-to-SQL | The architecture is a decision, not a default |
| 4 | The request pipeline | Two model calls, neither can put a number on screen |
| 5 | Five places a wrong answer gets stopped | Grounding is layered and cheap |
| 6 | Ambiguity is measured, not guessed | We price both readings before asking |
| 7 | A confident, verified, false answer | We hunted silent wrongness, not crashes |
| 8 | Three tables, two foreign keys, one view | We read the real schema properly |
| 9 | A 7.6 B model — because the architecture lets it be | Small model is a consequence, not a compromise |
| 10 | Same model, same accuracy, 2.7× lower latency | The serving layer was engineered too |
| 11 | What it actually answered | Including the refusals |
| 12 | What we measured | 46/46, and how we know |
| 13 | Every constraint, and how it is met | Section-by-section compliance |
| 14 | Known limits | Honesty, with named next steps |
| 15 | In one line | The thesis |

---

## 1 · Title

**Eyebrow** — TBX / BVP TECH CATALYST · BUILD A FINANCE ASSISTANT THAT ACTUALLY UNDERSTANDS YOU

# Finsight
### Grounded answers from your bank ledger.

Ask about spending, income, net movement and who was paid, in plain language. **Every figure is
computed in SQL and checked against the source rows before it reaches the screen — no number the
assistant states can originate from the model.**

| | | | |
|---|---|---|---|
| **46/46** golden canary, 100% | **7.6 B** local model · 20 B ceiling | **2** model calls per question | **0** numbers authored by the model |

*Team Finsight · Section 6 deliverable: presentation deck*

> **Say:** One sentence of positioning, then move. The four numbers are the whole deck in miniature —
> point at the last one and say we will spend the next eight minutes on how "zero" is enforced rather
> than promised.

---

## 2 · What we built

**Eyebrow** — WHAT WE BUILT

# Ask in plain language. Get a figure you can trace.

A chat assistant over the organizers' bank-transaction schema — MySQL 8.4, a 7.6 B local model, and a
pipeline where the model never touches a number.

- **Aggregates.** Spend, income, net movement, counts — over any period the data covers.
- **Who was paid.** This schema has no vendor table; payee names live inside free-text descriptions. A
  derived `counterparty` column is what makes the question answerable at all.
- **Records.** Largest transactions, anything matching a filter — with the full row set and Export CSV.
- **Follow-ups.** Threads persist; a clarification sticks for the rest of the conversation.
- **Refusals.** When the schema cannot answer, it says which domain is missing — rather than inventing
  one, or returning `₹0.00`.
- **Anomalies.** A windowed z-score against the account's own history.

**Live answer, shown as a card on the slide:**

> **How much did we spend in June 2026?**
> We spent **₹1,69,299.00** in June 2026, based on the partial coverage data available, which spans
> approximately 80% of the month.
>
> ```sql
> SELECT SUM(t.debit_amount) AS value, COUNT(*)
> FROM v_txn t WHERE t.transaction_date >= %(p_start)s
> ```
> `verified 3/3` · `high confidence` · `4 rows`
>
> Coverage note written by the pipeline, not the model: data ends 2026-06-24.

**Banner** — *The thesis.* The model decides what to compute. MySQL computes it. The model's words are
then checked against the result before anyone sees them.

> **Say:** Read the answer aloud and land on the coverage clause — the model did not decide to disclose
> partial coverage, the pipeline did. That distinction is the product.

---

## 3 · The decision everything follows from

**Eyebrow** — 01 · THE DECISION EVERYTHING FOLLOWS FROM

# We deliberately did not build text-to-SQL

The brief rules it out in one sentence: *"aggregate the data correctly before handing results to the
language model, so the model explains a computed result rather than calculating one itself."*

| | Text-to-SQL | Extract → compile (ours) |
|---|---|---|
| Model produces | Free-form SQL | **A small typed JSON object** |
| Can emit invalid SQL? | Yes | **No** — Python writes every query |
| Can invent a number? | Yes | **No** — arithmetic happens only in MySQL |
| Small-model reliability | Poor — generation is hard | **Good** — extraction is easy |
| Guardrails attach | After generation, awkwardly | **Before compute**, on a typed object |

**That reframe is also what makes a 7.6 B model viable.** We ask the model to do extraction, not
generation — and filling a fixed schema is exactly what small models are good at. The model-efficiency
criterion turns from a constraint into an advantage.

> **Say:** The obvious build was available and we passed on it. Everything downstream — the guardrails,
> the small model, the verifier — is a consequence of this one row: guardrails attach *before* compute.

---

## 4 · The request pipeline

**Eyebrow** — 02 · ARCHITECTURE

# The request pipeline

**User question** — *"How much did we spend in June 2026?"*

| | Stage | What it does |
|---|---|---|
| ⌁ | **LLM call 1 · extract** | question → typed QuerySpec |
| | **Validate** | closed vocabularies; contradictions rejected |
| | **Resolve** | counterparty · coverage · ambiguity probes |
| | **Compile → SQL** | pure Python, bound parameters only |
| ★ | **MySQL 8.4** | every SUM and GROUP BY, in DECIMAL(15,2) |
| ⌁ | **LLM call 2 · narrate** | sees only the returned rows; forbidden to compute |
| | **Verify** | every figure must exist in those rows |

**Answer** — prose + breakdown table + provenance panel

Branches drawn on the slide: *material gap → ask the user, both readings priced first* (from Resolve) ·
*fails → retry, then a template built from the rows* (from Verify).

Legend: dashed accent = model, never sees a total it did not receive · solid = code, deterministic,
testable, no model.

**Banner** — **Exactly two model calls per question — and neither one can put a number on screen.** Rows
stream to the browser at the MySQL step, before narration begins, so real figures appear while the prose
is still being written.

> **Say:** Trace the box order with a finger. The two dashed boxes are the only places a model runs.
> Everything between them is Python you can unit-test — and there are 69 such tests.

---

## 5 · Where wrong answers get stopped

**Eyebrow** — 03 · GROUNDING

# Five places a wrong answer gets stopped

Accuracy and grounding is the largest single criterion. Each layer is cheap, each catches a different
failure, and every example below is from this database.

| Layer | Catches | Example on this data |
|---|---|---|
| **1 · Schema** | Invented dimensions, metrics or statuses; contradictory date fields | A metric outside the declared set is rejected and repaired on retry |
| **2 · Sanity pass** | Right shape, wrong question — and filters nobody asked for | "spend" routed to `gross_amount`: **₹5,46,616 instead of ₹2,49,806**, because gross adds credits to debits |
| **3 · Coverage** | Periods outside the data | "last month" → *No data for August 2026. Coverage runs 2025-12-03 to 2026-06-24* — never `₹0.00` |
| **4 · Ambiguity** | Questions with two defensible answers | "the total for this account" — net movement or throughput? Both readings are **priced** before either is shown |
| **5 · Provenance** | Any figure in the prose absent from the rows | Verification failure drops confidence to **low** and replaces the narration with a template built from the rows |

**Two footers on the slide**

- *Sensitive fields never reach the model.* Views serve masked forms only; rows carry AES-256-SIV
  ciphertext, substituted back after verification.
- *Structural, not promised.* The compiler emits only columns the profile declares; the verifier rejects
  any figure absent from the rows.

> **Say:** Layer 2 is the one people underestimate — the query was valid, the shape was right, and the
> answer was double. Nothing except a semantic sanity pass catches that.

---

## 6 · Layer four, slowly

**Eyebrow** — 04 · LAYER FOUR, SLOWLY

# Ambiguity is measured, not guessed

Ambiguity is not a property of the question — it is a property of the question **against this data**.
"How much did we spend last month" is ambiguous in principle; if the competing readings land on the same
number it is not ambiguous in fact, and asking is friction.

**So we never ask the model whether something is ambiguous.** Rules detect the candidate readings, each
one is run as a cheap scalar query, and the decision comes from the measured gap between them.

When we do ask, we show the real number for each option — so the user recognises their own intent
instead of parsing jargon.

`Zero extra model calls`

| Measured gap | Action | Why |
|---|---|---|
| **under 1%** | Answer silently. | The fork does not change the answer. |
| **1 – 10%** | Answer, and state the assumption. | The user sees which reading was taken. |
| **over 10%** | Ask — with both numbers shown. | Two defensible answers, materially apart. |

**Callout** — *Percentage alone was the wrong test.* A fork can sit below the ask threshold in percentage
terms and still be worth crores in absolute terms. A second trigger now fires on absolute gap — 0.5% of
total spend, so it scales with whatever dataset is loaded.

> **Say:** The cheap version of this feature asks the LLM "is this ambiguous?" and burns a call to get an
> opinion. We spend two scalar queries instead and get a fact.

---

## 7 · The bug worth showing

**Eyebrow** — 05 · THE BUG WORTH SHOWING

# A confident, verified, false answer

`SUM()` over nothing returns **one row** — `(NULL, 0)` — not zero rows.

So every `if not rows` check is blind to it, and the model reads that row as the number zero.

Zero means "these netted to nothing." The truth was "no such transactions." The pipeline now collapses
that row and reports that the filters matched nothing — which is not the same as a total of zero.

> *"The sum of all transactions regarding the account number 30123456789012 is Rs 0."*
> `verified` · `high confidence` · **and false**
> Measured on this data, before the case was handled.

**Bugs of this shape do not throw — they render.** Nothing in the stack was failing: the query ran, a row
came back, the verifier found the figure in that row, and the badge went green. The only thing that
catches it is asking "is the number right?" against independently computed ground truth — which is what
the 46-question canary exists to do, and why its expected values are recomputed queries rather than
stored literals.

> **Say:** This is the slide that separates a demo from a system. Every guardrail we had passed, and the
> answer was still wrong. Show the quote, pause on "green badge", then explain the fix.

---

## 8 · Data model

**Eyebrow** — 06 · DATA MODEL

# Three tables, two foreign keys, one view doing the work

| `bank` | `account` | `transaction` |
|---|---|---|
| bank_code · bank_name | account_id · entity_id<br>program_id · balance<br>**account_number** *(sensitive)* | date · type · amount<br>description · reference<br>**utr_number** *(sensitive)*<br>**counterparty** *(derived)* |
| 10 rows | 10 rows | 10 rows · 9 counterparties |

FKs: `account → bank` on `bank_code` · `transaction → account` on `account_id`. All three feed:

**`v_txn` — the only surface the SQL compiler may touch**
joins all three · denormalises bank and account onto every row · splits the unsigned amount into
`credit_amount` / `debit_amount` / `signed_amount`
serves masked forms only — `XXXXXX3445`, `UTR-1CRl` — so a raw value cannot reach an answer even if a
later query forgets to mask
companions: `v_account` · `v_data_coverage` · `v_health` · `v_incidents` · `v_model_scorecard`

- **Amounts are unsigned.** Direction lives in `transaction_type`, so the view pre-splits it and the
  compiler keeps using plain `SUM()` rather than learning conditional aggregation. "Spend" is debits only.
- **There is no counterparty table.** Payee names are buried in free-text `description`; `counterparty`
  is extracted at boot and backfilled across the whole table — the only reason "who did we pay?" has an
  answer.
- **Nothing hardcodes the sample.** Coverage, vocabularies and the money columns are introspected into
  one registry at boot, so swapping in the full export is a load, not a code change.

> **Say:** Three structural facts about the organizers' schema drove most of the design. The unsigned
> amount is the one that would silently double every "spend" answer if you missed it.

---

## 9 · Model choice

**Eyebrow** — 07 · MODEL CHOICE

# A 7.6 B model — because the architecture lets it be

The constraint is the lowest possible model at the highest possible accuracy. The model never generates
SQL and never does arithmetic; both of its jobs are extraction.

1. **Schema-constrained decoding** — the QuerySpec JSON Schema is passed to the serving layer's
   structured-output constraint, so an invalid shape is impossible rather than merely unlikely.
2. **Few-shot examples from observed failures** — each example targets a failure seen in the zero-shot
   baseline probe, which scored about 2/5 semantically correct, not an imagined one.
3. **Pre-resolved date windows** — `api/dates.py` hands the model a closed set of periods, so it selects
   a window instead of deriving one.

**Serving stack**

| | |
|---|---|
| `qwen2.5-7b-instruct` | 7.6 B parameters · 4-bit · 4.7 GB · local. Ceiling in the brief is 20 B; this runs on a laptop with no API credits spent. |
| `nomic-embed-text` | 137 M — semantic entity resolution, off by default |
| `claude-haiku-4-5` | fallback — same interface, a two-line `.env` change |

**Banner** — *Comparing models is a measurement, not an opinion.* The `/ops` page runs the same
46-question canary against any configured model and keeps a scorecard over time, so "is the smaller model
good enough?" has an answer with a date on it.

> **Say:** We did not pick a small model and hope. We built a pipeline whose two model jobs are both
> extraction, and then the small model was sufficient by construction.

---

## 10 · Serving the model

**Eyebrow** — 08 · SERVING THE MODEL

# Same model, same accuracy, 2.7× lower latency

Qwen2.5-7B-Instruct-4bit served locally through vLLM on Apple Silicon (MLX), behind an OpenAI-compatible
`/v1/chat/completions` API with structured-output constraints.

| Metric | vLLM — Qwen2.5-7B-4bit | Ollama — Qwen2.5-7B-4bit |
|---|---|---|
| Accuracy | **38 / 40 (95%)** | 38 / 40 (95%) |
| Median query latency (p50) | **2.26 s** | 6.07 s |
| Total wall time, 40 queries | **113.92 s** | 262.2 s |
| Numeric questions | **19 / 19** | 19 / 19 |
| Behaviour questions | **11 / 11** | 11 / 11 |

Identical 4-bit weights and identical prompts on both sides — the win is in the serving layer, not the
model.

**Callout** — *Read the two latency figures separately.* 2.26 s is the median of this 40-query serving
benchmark. The 4.1 s p50 on the results slide is end-to-end across 1,315 logged production requests on
the Ollama path — a different measurement, not a contradiction.

- **Structured output.** Uses `guidance` as the structured-decoding backend, with unrestricted whitespace
  generation disabled — which makes JSON extraction reliable.
- **Why not Gemma.** A Gemma-based model degraded on the long production prompt — whitespace and
  repetition loops. Qwen2.5 produced valid, semantically correct output.

> **Say:** Accuracy is identical on both rows, which is the point — this was a serving-layer engineering
> win, not a model swap dressed up as one. Then flag the two-latency caveat before a judge asks.

---

## 11 · Sample questions

**Eyebrow** — 09 · SAMPLE QUESTIONS

# What it actually answered

Captured from a live run against `tbx_live` on 2026-09-05. Coverage is 2025-12-03 → 2026-06-24, 10
transactions, INR — including the refusals.

**How much did we spend in total?**
> We spent a total of **₹2,49,806.00**.
`verified 1/1` · `high` · `2.8 s` — the metric is `debit_amount`, not the gross sum; summing both
directions would report throughput.

**Who did we pay the most?**
> The counterparty we paid the most was **SELECTION MOBILE**, with a payment value of **₹146,474.00**.
`verified 1/1` · `high` · `3.2 s` — answerable only because `counterparty` is derived from free text at
boot; this schema has no vendor table.

**How much did we spend last month?**
> **No data for August 2026. Coverage runs 2025-12-03 to 2026-06-24.**
`guardrail · coverage` — not ₹0.00. Zero and unknown are different answers, and conflating them is what
the grounding criterion punishes.

**Which transactions are still unreconciled?**
> **This data has no reconciliation status of any kind.**
`guardrail · absent concept` — one of the brief's own example questions. The refusal names the specific
missing domain, not a generic list.

**Callout** — *Filtering is not displaying.* "How much went through account 50200013729069" is answered
normally — the number came from the user. Listing or displaying account and UTR numbers is refused.

> **Say:** Two answers and two refusals, deliberately. The refusals are the harder engineering and the
> thing most demos skip.

---

## 12 · Results

**Eyebrow** — 10 · RESULTS

# What we measured

Latest run — `qwen2.5:7b-instruct`, 2026-09-05.

| | | | |
|---|---|---|---|
| **46 / 46** golden canary, 100% in 202 s — 21 graded on the number, 14 on behaviour, 11 on the spec | **69 / 69** unit tests — 30 compiler, 18 narration, 12 extraction, 9 semantic | **97.4%** verification pass rate, against a ≥ 95% threshold | **93.6%** answers at high confidence, against a ≥ 85% threshold |

**Latency** — p50 **4.1 s**, p95 **14.2 s**, over 1,315 logged requests. The same model medians **2.26 s**
on the vLLM serving path.

**Ground truth is a query, not a literal.** Every expected value in the canary is recomputed on each run,
so the 46-question set stays valid the moment the organizers' full export replaces the sample rows — no
answer key to maintain.

**`/ops` — "it answered" and "it answered correctly" are different questions**

- **Canary runner** — the 46 golden questions against any configured model, with a scorecard comparing
  models over time.
- **Health signals** — 12 thresholded rates over 24 h: verification, template fallback, repair, clarify,
  empty result, p95. `/api/metrics` returns `ok: false` when any breaches.
- **Incidents + replay** — every request logged with its spec, SQL, row sample, timings and confidence;
  tripped requests are replayable.

> **Say:** The number to dwell on is not 46/46 — it is that the expected values are recomputed queries.
> It means the score survives the dataset we have not seen yet.

---

## 13 · Constraints

**Eyebrow** — 11 · CONSTRAINTS

# Every constraint in the brief, and how it is met

| Constraint | How it is met |
|---|---|
| **≤ 20 B parameter LLM** | `qwen2.5-7b-instruct` — **7.6 B**, 4-bit, 4.7 GB. Runs on a laptop, no API credits spent. Embedding model 137 M. |
| **≤ 20 M records** | Indexed for it: `idx_txn_date`, `idx_txn_account`, `idx_txn_counterparty`, FULLTEXT on description and counterparty. The two things to revisit at that scale are named on the next slide. |
| **Grounded in the provided schema only** | Structural, not promised — the compiler emits only columns the profile declares, and the verifier rejects any figure absent from the result rows. |
| **Single company, single currency** | INR throughout: ₹ with Indian digit grouping (₹1,69,299.00, not ₹169,299.00), timestamps stored UTC and displayed IST. |
| **No fabricated figures** | The verifier enforces it per answer; the rate is a monitored signal on `/ops` — currently **97.4%** against a ≥ 95% threshold. |
| **Sensitive fields**<br>`account_number, utr_number` | Views serve masked forms only. Rows handed to the model carry AES-256-SIV ciphertext in place of the account number, substituted back at final render, **after** verification — and because SIV is deterministic, the ciphertext doubles as a stable pseudonym. |

**Banner** — *Also delivered.* CSV export on any breakdown · confidence signalling (high / medium / low)
on every answer · anomaly callouts via a windowed z-score against the account's own history · a
provenance panel showing the spec, the SQL, the rows and every note the pipeline attached.

> **Say:** Read the left column only, and let the right column be there for the judge who wants it.
> The row worth stopping on is the last one — the model can count how often an account appears without
> ever seeing the account number.

---

## 14 · Known limits

**Eyebrow** — 12 · KNOWN LIMITS

# Stated plainly, because a judge will find them anyway

- **Multi-turn anchors on today, not on the previous turn.** "How much did we spend in June 2026?" →
  "How does that compare to May?" works. "And the month before?" resolves to the month before today, not
  before June, and lands outside coverage. Named periods in a follow-up are reliable; bare relative
  anchors are not.
- **Compare answers can mislabel which period is which.** The SQL labels the two sides `'current'` /
  `'previous'` rather than the real period names, so the narrator infers which month is which and
  sometimes gets it backwards. Every number is real, so the verifier passes — and the canary grades
  numbers, not labels.
- **Fuzzy counterparty matching degraded in the MySQL port.** `pg_trgm` gave indexed similarity scoring;
  MySQL has neither, and FULLTEXT matches whole words, so it will not find `RELIANCE` inside
  `RELIANCEDIGITAL RETAIL LTD`. The profile ranks `LIKE` candidates by a positional score instead —
  correct, but it scans.
- **Vector search runs in Python.** MySQL 8.4 has no vector type, so the semantic index is a JSON array
  and cosine similarity runs over the loaded candidate set. Fine at this vocabulary size; `MAX_LABELS`
  refuses rather than quietly getting slow. Off by default.

**None of these is a correctness hole in the numbers** — each is a named boundary with a known next step.

> **Say:** Volunteer these. A judge who finds a limit you did not mention discounts everything else you
> claimed; a judge who hears you name it first trusts the rest.

---

## 15 · In one line

**Eyebrow** — IN ONE LINE

# The model decides **what** to compute. MySQL computes it. The model's words are then checked against the result before anyone sees them.

No number the assistant states can originate from the model — and that is enforced structurally, not
promised.

| | | | |
|---|---|---|---|
| `./run.sh` chat on :3000, ops on :3000/ops | `architecture.html` the diagram, in a browser | `python -m eval` the 46-question canary — expect 46/46 | `README.md` setup, samples, model choice |

*Team Finsight · TBX / BVP Tech Catalyst*

> **Say:** Repeat the thesis verbatim from slide 2. It is the only sentence you want them holding when
> they score you.

---

## Regenerating the deck

The `.pptx` is generated, not hand-built — edit the copy here or in the generator and re-run:

```bash
pip install python-pptx
python scripts/deck/build_deck.py   # writes Finsight_Deck.pptx at the repo root
python scripts/deck/audit.py        # checks every shape for out-of-bounds and text overflow
```

| | |
|---|---|
| [scripts/deck/deck_lib.py](scripts/deck/deck_lib.py) | design system — palette, type scale, grid / card / callout / arrow primitives |
| [scripts/deck/build_deck.py](scripts/deck/build_deck.py) | the 15 slides |
| [scripts/deck/audit.py](scripts/deck/audit.py) | geometry check — no shape off-slide, no text overflowing its box |

Palette and type follow [architecture.html](architecture.html) so the deck, the diagram and the app read
as one system. Fonts are Helvetica Neue and Menlo; if you present on Windows, substitute a system sans
before exporting.
