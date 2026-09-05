# Finsight

**Grounded answers from your bank ledger.**
Team Finsight · TBX / BVP Tech Catalyst — *Build a Finance Assistant That Actually Understands You*

Ask about spending, income, net movement and who was paid, in plain language. Every figure
is computed in SQL and checked against the source rows before it reaches the screen —
**no number the assistant states can originate from the model.**

```bash
./run.sh                     # chat  http://localhost:3000
                             # ops   http://localhost:3000/ops
./run.sh test                # 60 unit tests (compiler + narration + extraction)
./.venv/bin/python -m eval   # 46-question canary against SQL ground truth
```

---

## Submission index

Everything Section 6 of the problem statement asks for, and where it is:

| Requirement | Where |
|---|---|
| Working prototype (chat + backend) | [`./run.sh`](run.sh) → `localhost:3000`; backend in [api/](api/), frontend in [web/](web/) |
| Architecture diagram | **[architecture.html](architecture.html)** — open in a browser |
| README with setup instructions | [§1 Setup](#1-setup), below |
| Sample questions and the answers produced | [§3 Sample questions and answers](#3-sample-questions-and-answers) — captured from a live run, not written by hand |
| Model choice rationale + accuracy | [§4 Model choice](#4-model-choice) |
| Presentation deck | *separate deliverable* |

Latest measured run — `qwen2.5:7b-instruct`, 2026-09-05:

| | |
|---|---|
| Golden canary | **46 / 46 (100%)** in 202 s |
| Unit tests | **69 / 69** — 30 compiler, 18 narration, 12 extraction, 9 semantic |
| Latency, 1,315 logged requests | p50 **4.1 s**, p95 **14.2 s** |
| Verification pass rate | **97.4%** (threshold ≥ 95%) |
| High-confidence answers | **93.6%** (threshold ≥ 85%) |

---

## 1. Setup

### Prerequisites

| | | |
|---|---|---|
| Docker | any recent version | hosts MySQL 8.4 |
| Ollama | any recent version | serves the local model |
| Python | 3.12+ (built on 3.14) | backend |
| Node | 20+ (built on 24) | frontend |

### First run

```bash
git clone <this repo> && cd TBX

# 1. Python environment
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 2. Models (≈5 GB total, one-time)
ollama pull qwen2.5:7b-instruct
ollama pull nomic-embed-text

# 3. Database — MySQL 8.4 in Docker, data in a named volume
docker run -d --name tbx-mysql \
  -e MYSQL_ROOT_PASSWORD=tbxroot -e MYSQL_DATABASE=tbx_live \
  -e MYSQL_USER=tbx -e MYSQL_PASSWORD=tbx \
  -p 127.0.0.1:3306:3306 -v tbx-mysql-data:/var/lib/mysql mysql:8.4 \
  --character-set-server=utf8mb4 --collation-server=utf8mb4_0900_ai_ci

# 4. Configuration
cp .env.example .env
python3 -c "import secrets; print('SENSITIVE_KEY=' + secrets.token_hex(32))" >> .env

# 5. Frontend dependencies
(cd web && npm install)

# 6. Go
./run.sh
```

`run.sh` starts the container, Ollama, the API on `:8000` and the web app on `:3000`, then
tails what came up. It is idempotent — anything already listening is left alone.

There is **no data-loading step**. The API applies every migration in
[api/sql/mysql/](api/sql/mysql/) on boot, seeds the sample export, and backfills the derived
`counterparty` column. Starting the server on an empty volume is enough.

### Everyday commands

```bash
./run.sh            # start everything
./run.sh status     # what is up, plus a health check per subsystem
./run.sh stop       # stop the API and web dev server
./run.sh test       # compiler + narration + extraction suites
```

### Verifying the install

```bash
curl -s localhost:8000/api/health     # every subsystem, with a verdict each
curl -s localhost:8000/api/coverage   # what the loaded data actually spans
./.venv/bin/python -m eval            # the 46-question canary — expect 46/46
```

The canary is the only thing in the system that answers *"is the number right?"*. Ground
truth is a **query**, recomputed every run rather than stored as a literal, so the set stays
valid when the real export replaces the sample rows.

### Configuration

All keys map to [api/config.py](api/config.py); [.env.example](.env.example) documents each.
The ones that matter:

| Key | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `mysql://tbx:tbx@127.0.0.1:3306/tbx_live` | |
| `OLLAMA_MODEL` | `qwen2.5:7b-instruct` | the model that answers chat |
| `EVAL_MODELS` | `qwen2.5:7b-instruct,gemma3:4b-it-qat` | closed set the /ops bake-off may target |
| `SENSITIVE_KEY` | *(none — must be set)* | AES-SIV key for the account-number boundary |
| `LLM_PROVIDER` | `ollama` | `anthropic` swaps in `claude-haiku-4-5` behind the same interface |
| `REFERENCE_DATE` | *(wall clock)* | pin "today" to make a demo reproducible |

> **Rebuilding from scratch:** `docker rm -f tbx-mysql && docker volume rm tbx-mysql-data`,
> then repeat step 3. Do **not** apply migrations as `root` — root holds `SYSTEM_USER` in
> MySQL 8, and the `tbx` account then cannot replace the views it created. See
> [MYSQL_PORT.md](MYSQL_PORT.md).

---

## 2. Architecture

**[architecture.html](architecture.html)** is the diagram — pipeline, data model, and where
wrong answers get stopped. In one paragraph:

The obvious build is text-to-SQL. We deliberately did not, because the brief rules it out in
one sentence: *aggregate the data correctly **before** handing results to the language model,
so the model explains a computed result rather than calculating one itself.* So the model is
asked to do **extraction**, not generation — it fills a small typed JSON object (`QuerySpec`),
Python compiles that to parameterised SQL, MySQL does every arithmetic operation, and a second
model call may only narrate rows it was handed. A verifier then checks that every number in
the prose exists in those rows. **Exactly two model calls per question, and neither one can
put a number on screen.**

That reframe is also what makes a 7B model viable: filling a fixed schema is what small models
are good at.

---

## 3. Sample questions and answers

Captured from a live run against `tbx_live` on 2026-09-05, `qwen2.5:7b-instruct`. Every
answer below is what the assistant actually returned, including the refusals.

Data coverage is **2025-12-03 → 2026-06-24**, 10 transactions, currency **INR**.

### Aggregate

> **How much did we spend in total?**
> *We spent a total of ₹2,49,806.00.*

```sql
SELECT SUM(t.debit_amount) AS value, COUNT(*) AS txn_count FROM v_txn t
```
`value = 249806.00`, 10 transactions · verified 1/1 · **high** confidence · 2.8 s

Note the metric: `debit_amount`, not the gross sum. Amounts in this schema are **always
positive** and direction lives in `transaction_type`, so "spend" is debits only. Summing both
directions would report throughput and call it spend.

### A period, with partial coverage disclosed

> **How much did we spend in June 2026?**
> *We spent ₹1,69,299.00 in June 2026, based on the partial coverage data available, which
> spans approximately 80% of the month.*

```sql
SELECT SUM(t.debit_amount) AS value, COUNT(*) AS txn_count
FROM v_txn t WHERE t.transaction_date >= %(p_start)s AND t.transaction_date < %(p_end)s
```
`value = 169299.00`, 4 transactions · verified 3/3 · **high** · 4.3 s

The coverage note is generated by the pipeline, not the model: data ends 2026-06-24, so June
is 80% covered and the answer says so unprompted.

### Who was paid

> **Who did we pay the most?**
> *The counterparty we paid the most was SELECTION MOBILE, with a payment value of ₹146,474.00.*

| counterparty | value | txn_count |
|---|---:|---:|
| SELECTION MOBILE | 146,474.00 | 2 |
| NAVYUG SELECTION | 50,000.00 | 1 |
| RELIANCEDIGITAL RETAIL LTD SELECT CITY SAKET DELHI | 21,156.00 | 1 |
| SELECTION ELECTRONICS DAHISAR EAST | 14,866.00 | 1 |

verified 1/1 · **high** · 3.2 s

This schema has **no vendor table** — every payee name is buried in free-text narration. The
`counterparty` column is derived from `description` by [api/narration.py](api/narration.py) and
backfilled at boot, which is the only reason "who did we pay?" has an answer at all.

### Individual records

> **Show me the 3 largest transactions**
> *The three largest transactions are:*
> - *A credit of ₹2,60,000.00*
> - *A debit of ₹79,575.00*
> - *A debit of ₹66,899.00*

verified 3/3 · **high** · 4.9 s. The full row set — dates, descriptions, bank, reference —
renders in the breakdown table beside the prose, with **Export CSV**.

### Multi-turn

> **How much did we spend in June 2026?** → *₹1,69,299.00*
> **How does that compare to May?**
> *The value for the current period is ₹71,156.00, down from ₹169,299.00 in the previous.*

The follow-up carries intent without the user repeating context. See
[§11 Known limits](#11-known-limits) for what does **not** carry.

### Guardrail — a period outside the data

> **How much did we spend last month?**
> *No data for August 2026. Coverage runs 2025-12-03 to 2026-06-24.*

Not `₹0.00`. Zero and unknown are different answers, and conflating them is exactly the
failure the grounding criterion punishes.

### Guardrail — a domain this schema does not hold

> **Which transactions are still unreconciled?**
> *This data has no reconciliation status of any kind.*

One of the brief's own two example questions. This export has no reconciliation state, so the
honest answer is to say so. The decline names the *specific* missing domain rather than
reciting a generic list — there are eight such guards in
[api/profiles/bank_txn.py](api/profiles/bank_txn.py) (`ABSENT_CONCEPTS`).

### Guardrail — sensitive data

> **What is the full account number for the HDFC account?**
> *That isn't answerable from this data. […] If you have an account number, ask about it
> directly and that is answerable too; what cannot be done is listing or displaying account
> and UTR numbers.*

**Filtering is not displaying.** *"How much went through account 50200013729069"* is answered
normally — the number came from the user. Listing account numbers is refused.

---

## 4. Model choice

**`qwen2.5:7b-instruct`** (7.6B parameters, Q4_K_M, 4.7 GB) served locally by Ollama.
Embeddings: `nomic-embed-text` (137M).

**Why this one.** The constraint is *lowest possible model, highest possible accuracy*, and
the architecture is what makes a small model sufficient: the model never generates SQL and
never does arithmetic. It fills a typed JSON object, and later restates rows it was handed.
Both are extraction tasks. Three things carry the accuracy:

1. **Schema-constrained decoding** — the QuerySpec JSON Schema is passed to Ollama's `format`
   parameter, so invalid shapes are impossible rather than merely unlikely.
2. **Few-shot examples**, each targeting a failure observed in the zero-shot baseline probe —
   which scored ~2/5 semantically correct — not an imagined one.
3. **Pre-resolved date windows** ([api/dates.py](api/dates.py)), so the model *selects* a
   period instead of deriving one.

7.6B is comfortably inside the brief's 20B ceiling, and the whole thing runs on a laptop with
no API credits spent.

**Accuracy against the sample question set.** 46 golden questions in
[eval/questions.bank_txn.yaml](eval/questions.bank_txn.yaml) — 21 graded on the *number*
against ground-truth SQL, 14 on *behaviour* (did it refuse when it should have?), 11 on the
*spec* it produced:

```
46/46 passed (100%)  in 202s   model=qwen2.5:7b-instruct
```

Plus 69 unit tests across the deterministic layers: 30 compiler, 18 narration, 12 extraction,
9 semantic.

**Comparing models.** `/ops` runs the same canary against any model in `EVAL_MODELS` and keeps
a scorecard, so "is the smaller model good enough?" is a measured question rather than an
opinion. `gemma3:4b-it-qat` (4.3B) is pulled and configured for exactly that comparison.
Switching to `claude-haiku-4-5` is a two-line `.env` change behind an identical interface.

---

## 5. Requirements coverage

**Must have**

| Requirement | Where |
|---|---|
| Natural language query handling | [api/extract.py](api/extract.py) — schema-constrained QuerySpec, few-shot, resolved date windows |
| Grounded retrieval | [api/compile.py](api/compile.py) — Python writes every query; the model never emits SQL |
| Accurate computation | MySQL does all arithmetic in `DECIMAL(15,2)`; `max_rows_to_llm = 3` |
| Verifiable answers | Breakdown table + provenance panel beside every answer ([web/components/answer/](web/components/answer/)) |
| Hallucination guardrails | Five layers — [§7](#7-how-grounding-works) |
| Lightweight model | 7.6B local, ceiling is 20B — [§4](#4-model-choice) |
| Multi-turn conversation | Threads persisted in `chat_threads` / `chat_messages`; clarifications stick per thread |
| Explainability | Provenance panel shows the spec, the SQL, the rows, and every note the pipeline attached |

**Good to have**

| | |
|---|---|
| CSV export | **Export CSV** on any breakdown — [web/lib/toCsv.ts](web/lib/toCsv.ts) |

**Bonus**

| | |
|---|---|
| Confidence signalling | `high` / `medium` / `low` on every answer. Medium = a stated assumption or a template fallback; low = a figure could not be traced, and the narration is replaced by one built directly from the rows |
| Note on model choice | [§4](#4-model-choice) |
| Anomaly callouts | `intent: anomaly` compiles a windowed z-score against the account's own history — [api/compile.py](api/compile.py) `_compile_anomaly` |

---

## 6. Constraints

| Constraint | Status |
|---|---|
| **≤ 20B parameter LLM** | 7.6B (`qwen2.5:7b-instruct`). Embedding model 137M. |
| **≤ 20M records** | Schema is indexed for it: `idx_txn_date`, `idx_txn_account`, `idx_txn_counterparty`, FULLTEXT on `description` and `counterparty`. See [§11](#11-known-limits) for the two things to revisit at that scale. |
| **Answers grounded in the provided schema only** | Structural, not promised: the compiler only emits columns the profile declares, and the verifier rejects any figure absent from the result rows. |
| **Single company, single currency** | INR throughout — ₹ with Indian digit grouping (₹1,69,299.00, not ₹169,299.00), timestamps in IST. [api/money.py](api/money.py), [web/lib/format.ts](web/lib/format.ts) |
| **No fabricated figures** | The verifier is the enforcement point; the rate is a monitored signal on `/ops` (currently 97.4%, threshold ≥95%). |

---

## 7. How grounding works

Five layers, each cheap, each catching a different failure:

| Layer | Catches | Example on this data |
|---|---|---|
| **1 · Schema** (pydantic) | Invented dimensions, metrics, statuses; contradictory date fields | A metric outside the declared set is rejected and repaired on retry |
| **2 · Sanity pass** | Right shape, wrong question — and filters nobody asked for | "spend" routed to `gross_amount` is corrected to `debit_amount`; gross would add credits to debits and report roughly double |
| **3 · Coverage** | Periods outside the data | "last month" → *No data for August 2026. Coverage runs 2025-12-03 to 2026-06-24* — never `₹0.00` |
| **4 · Ambiguity** | Questions with two defensible answers | Each reading is run as a cheap scalar query and the decision comes from the **measured** gap: under 1% answer silently, under 10% answer and state the assumption, beyond that ask — showing the real number for each option. **Zero extra model calls.** A second trigger fires on absolute gap (0.5% of total), because 7% of a large enough number is still material. |
| **5 · Provenance** | Any figure in the prose absent from the rows | Verification failure downgrades confidence to `low` and replaces the narration with a template built from the rows |

Two more things sit underneath:

- **The empty-aggregate trap.** `SUM()` over nothing returns one row — `(NULL, 0)` — not zero
  rows, so every `if not rows` check is blind to it and the model reads that row as the number
  zero. Measured, before this was handled: *"The sum of all transactions regarding the account
  number 30123456789012 is Rs 0."* Verified, high confidence, and false. The pipeline now
  collapses that row and says the filters matched nothing, which is not the same as a total of
  zero.
- **Sensitive fields never reach the model.** `account_number` and `utr_number` are the two
  the brief marks sensitive. Views serve masked forms only; rows handed to the model carry
  AES-256-SIV ciphertext in place of the account number, and real values are substituted back
  only at the final render, *after* verification. Because SIV is deterministic the ciphertext
  doubles as a stable pseudonym, so the model can still say "this account appears in three
  rows" correctly without ever seeing the number. Asserted, not assumed — see
  [api/crypto.py](api/crypto.py).

---

## 8. The data

The organizers' schema in its native MySQL dialect — [api/sql/mysql/live/](api/sql/mysql/live/):

| Table | What it is |
|---|---|
| `bank` | Bank code → name |
| `account` | Account, entity, program, balance, bank. `account_number` is **sensitive** |
| `transaction` | The fact table: date, type (`credit`/`debit`), amount, description, reference, `utr_number` (**sensitive**), and the derived `counterparty` |

Three structural facts drive most of the design:

1. **Amounts are unsigned.** Direction lives in `transaction_type`. The `v_txn` view pre-splits
   this into `credit_amount` / `debit_amount` / `signed_amount` so the compiler keeps using
   plain `SUM()` rather than learning conditional aggregation.
2. **There is no counterparty table.** Payee names live inside free-text `description`;
   `counterparty` is extracted from it at boot.
3. **Two fields are sensitive.** Neither is exposed to the compiler at all — the views serve
   masked forms (`XXXXXX3445`, `UTR-1CRl`), so a raw value cannot reach an answer even if a
   later query forgets to mask.

Views: `v_txn` (the workhorse — denormalises bank and account onto every transaction),
`v_account`, `v_data_coverage`, plus `v_health` / `v_incidents` / `v_model_scorecard` for ops.

Currently loaded: the **10-row sample export**, 2025-12-03 → 2026-06-24. Nothing in the system
hardcodes that — coverage, vocabularies and the fiscal boundary are introspected into one
registry at boot ([api/registry.py](api/registry.py)), and detectors that find nothing to arm
themselves against switch off. Swapping in the full export is a load, not a code change.

---

## 9. Operations

`http://localhost:3000/ops` — because "it answered" and "it answered *correctly*" are
different questions, and only one of them can be measured from logs.

- **Canary runner** — the 46 golden questions against any configured model, with a scorecard
  comparing models over time.
- **Health signals** — 12 thresholded rates over the last 24 h (verification pass rate,
  template fallback, repair, coercion, sanity correction, clarify, empty result, confidence
  mix, error rate, p95). Each carries its own verdict; `/api/metrics` returns `ok: false` when
  any breaches.
- **Incidents** — recent requests that tripped a signal, with the question, the unverified
  figures, and a **replay** endpoint.

Every request is logged to `query_log` with its spec, SQL, row sample, timings, token counts
and confidence.

---

## 10. Repo map

```
api/            FastAPI backend
  extract.py      LLM call 1 — question → QuerySpec
  compile.py      QuerySpec → parameterised SQL   (no model involved)
  narrate.py      LLM call 2 + the number verifier
  crypto.py       AES-SIV boundary for sensitive fields
  registry.py     everything introspected from the live database at boot
  profiles/       per-schema maps: dimensions, metrics, filters, prompt rules
  routes/ask.py   the pipeline, streamed as SSE
  sql/mysql/      migrations, applied on every boot
web/            Next.js 15 chat + /ops, types generated from the backend schema
eval/           46-question canary + 69 unit tests
run.sh          start / status / stop / test
```

**Companion documents**

| | |
|---|---|
| [architecture.html](architecture.html) | The diagram — open in a browser |
| [PLAN.md](PLAN.md) | Strategy |
| [FLOW.md](FLOW.md) | Interfaces, event by event |
| [AMBIGUITY.md](AMBIGUITY.md) | Clarification design |
| [PRD.md](PRD.md) | Build spec |
| [MYSQL_PORT.md](MYSQL_PORT.md) | The PostgreSQL → MySQL port, and what it cost |

> **On the stand-in dataset.** Before the organizers' schema arrived, this repo carried a
> shape-compatible 1M-row stand-in built from SF Vendor Payments, on PostgreSQL. It was never
> ported to MySQL and its code has been removed — its profile, build scripts, question set and
> DDL are gone. [MYSQL_PORT.md](MYSQL_PORT.md) records what went and why; the files remain in
> git history. `data/DATA_DICTIONARY.md` and `data/SAMPLE_QUESTIONS.md` still describe *that*
> dataset, not this one.

---

## 11. Known limits

Stated plainly, because a judge will find them anyway:

- **Multi-turn anchors on today, not on the previous turn.** *"How much did we spend in June
  2026?"* → *"How does that compare to May?"* works. *"And the month before?"* resolves to the
  month before **today**, not the month before June, and lands outside coverage. Named periods
  in a follow-up are reliable; bare relative anchors are not.
- **Compare answers can mislabel which period is which.** The compiled SQL labels the two sides
  `'current'` / `'previous'` rather than emitting the real period names, so the narrator has to
  infer which month is which and sometimes gets it backwards — every number is real and
  traceable, so the verifier passes and confidence stays `high`. The canary does not catch it
  because it grades the numbers, not the labels.
- **Fuzzy counterparty matching degraded in the MySQL port.** `pg_trgm` gave real similarity
  scoring behind an index. MySQL has neither; FULLTEXT matches whole words, so it will not find
  `RELIANCE` inside `RELIANCEDIGITAL RETAIL LTD`. The profile ranks `LIKE` candidates by a
  positional score instead — correct, but it scans. First thing to revisit on a large export.
- **Vector search runs in Python.** MySQL 8.4 has no vector type, so `semantic_index.embedding`
  is a JSON array and cosine similarity runs over the loaded candidate set. Fine at this
  vocabulary size; `MAX_LABELS` refuses rather than quietly getting slow. Off by default
  (`ENABLE_SEMANTIC=false`).
- **`.env.example` still mentions the removed `vendor_payments` / `tbx_finance` stand-in.**
  Cosmetic; `DATASET` accepts only `bank_txn`.
