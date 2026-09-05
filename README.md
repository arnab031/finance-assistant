# Finsight

**Grounded answers from your ledger.**
Team Finsight · TBX / BVP Tech Catalyst — *Build a Finance Assistant That Actually Understands You*

Ask about vendor spend, payouts and reconciliation in plain language. Every figure is
computed in SQL and verified against the source rows before it is shown — **no number the
assistant states can originate from the model.**

```bash
./run.sh          # start everything
                  # chat  http://localhost:3000
                  # ops   http://localhost:3000/ops
./run.sh test     # 49 regression tests
python -m eval    # 50-question canary against SQL ground truth
```

| | |
|---|---|
| **Architecture** | [architecture.html](architecture.html) — open in a browser |
| **Strategy** | [PLAN.md](PLAN.md) |
| **Interfaces** | [FLOW.md](FLOW.md) |
| **Clarification design** | [AMBIGUITY.md](AMBIGUITY.md) |
| **Build spec** | [PRD.md](PRD.md) |
| **Schema** | [data/DATA_DICTIONARY.md](data/DATA_DICTIONARY.md) |
| **MySQL port** | [MYSQL_PORT.md](MYSQL_PORT.md) |

Stack: Next.js 15 · FastAPI · MySQL 8.4 (Docker) · qwen2.5:7b-instruct via Ollama.

Current canary: **40/40 (100%)** on the organizers' schema, plus 69 unit tests
(30 compiler, 18 narration, 12 extraction, 9 semantic).

> **On the PostgreSQL and SQLite sections below.** They describe building the
> *stand-in* dataset, which predates the organizers' schema. It was PostgreSQL-only,
> was never ported, and its code has been removed — see
> [MYSQL_PORT.md](MYSQL_PORT.md). Those sections are kept as a record of how the
> 1M-row stand-in was built and validated; they no longer describe anything you
> can run.

---

# The dataset

The organizers will ship their own starter dataset before the hackathon. This is a
shape-compatible stand-in, so the retrieval, aggregation and grounding layers are already
built and the real data drops in through one loader.

---

## What you get

Everything Section 4 of the problem statement promises:

| Table | Rows | What it is |
|---|---|---|
| `transactions` | 1,019,354 | Voucher-level payments — the fact table |
| `reconciliation` | 1,019,354 | Bank-matching state, 1:1 with transactions |
| `vendor_payouts` | 194,248 | Payment runs, vendor × date |
| `vendors` | 7,905 | Vendor master |
| `chart_of_accounts` | 616 | Three-level COA |
| `departments` | 58 | Department + org group |
| `funds` | 338 | Fund, type, category |

Plus [DATA_DICTIONARY.md](data/DATA_DICTIONARY.md) and
[SAMPLE_QUESTIONS.md](data/SAMPLE_QUESTIONS.md) — a golden question → SQL → answer set.

**Window:** 2024-09-01 → 2026-08-29, 24 complete months. Real daily dates, so
"last month", "vs. the month before", and quarter-over-quarter all work out of the box.

**Distributions** (why there's something to actually query):

| `payment_status` | | | `reconciliation_status` | |
|---|---|---|---|---|
| Paid | 96.93% | | Reconciled | 94.41% |
| Retainage Held | 1.40% | | Unreconciled | 3.47% |
| Reversed | 0.86% | | Partially Matched | 1.45% |
| Pending | 0.81% | | Disputed | 0.67% |

That leaves **56,969 non-reconciled items** — enough that *"which transactions are still
unreconciled?"* has a real, non-trivial answer with genuine variation by age, size, and
department.

---

## Source

[SF Vendor Payments (Vouchers)](https://data.sf.gov/d/n9pm-xkyq) — City & County of San
Francisco, public domain.

Why this one, out of everything public:

- **Voucher-level, not aggregated.** One row per payment line, with a real date.
- **A real chart of accounts.** SF publishes a genuine three-level hierarchy
  (category → object → sub-object) plus fund and department dimensions. You don't have
  to invent a COA, which is the part most synthetic datasets get unconvincingly wrong.
- **Real vendor names.** 7,905 of them, as filed. 48 of those collapse into duplicate
  entities under different surface forms (`NEW FLYER OF AMERICA INC` / `New Flyer of America
  Inc`, `D & D UPHOLSTERY INC` / `D&D Upholstery Inc`, `COMPUTERLAND SILICON VALLEY` /
  `ComputerLand of Silicon Valley`) — a modest 1.2%, but enough to make entity resolution a
  real test rather than a string-equality check.
- **Negative amounts, retainage, pending vouchers.** The awkward cases that break naive
  `SUM()` logic are already in the data.
- **Public domain.** No licence problem in a submission.

### What's synthetic

The SF source publishes paid/pending/retainage amounts but **not** bank-level reconciliation
state — no public dataset does, it's internal back-office data. So `reconciliation.csv`,
`vendors.payment_terms`, and `vendor_payouts.payment_method` are generated.

Generation is **deterministic** — seeded off a BLAKE2b hash of the transaction id, no
`random` module, no timestamps. Re-running produces byte-identical output, so your eval set
stays stable. Details and the probability model are in
[DATA_DICTIONARY.md](data/DATA_DICTIONARY.md#provenance--what-is-real-and-what-is-not).

Everything else is real and unmodified.

---

## Setup

No dependencies — stdlib Python 3.9+ only.

```bash
python3 scripts/01_download.py    # 31 MB gzipped, ~15 min (46 API calls)
python3 scripts/02_normalize.py   # builds data/processed/*.csv + data/finance.db, ~2 min
python3 scripts/03_validate.py    # 14 integrity checks + regenerates SAMPLE_QUESTIONS.md
python3 scripts/04_load_postgres.py --emit-sql   # optional: PostgreSQL build
```

`01_download.py` is resumable — it skips months already on disk, so just re-run it if the
connection drops.

`02_normalize.py` writes plain `.csv`; the three large ones ship gzipped here. Either
`gunzip data/processed/*.gz` or read them directly (`pandas.read_csv` handles `.gz`
transparently).

Outputs:

```
data/
  raw/            sf_vendor_payments_YYYY-MM.csv.gz    24 files,  31 MB
  processed/      transactions.csv.gz                            30 MB
                  reconciliation.csv.gz                          22 MB
                  vendor_payouts.csv.gz                          2.9 MB
                  vendors.csv / chart_of_accounts.csv
                  departments.csv / funds.csv                    865 KB
  finance.db      SQLite — 7 tables, 13 indexes, 2 views        621 MB
  postgres/       schema.sql, constraints_indexes_views.sql,
                  load.sh                                        24 KB
  DATA_DICTIONARY.md
  SAMPLE_QUESTIONS.md
```

### On the 621 MB database

That's 1M rows against a deliberately denormalized fact table — `transactions` carries
`vendor_name`, `department_name`, `account_name` and friends inline rather than behind joins.
That costs ~73 MB, and it's a deliberate trade: **fewer joins means a small model writes
correct SQL far more often**, which is the 20% model-efficiency criterion. Indexes are another
~200 MB.

It's gitignored and regenerates in ~2 minutes from the 31 MB of raw files, so it never needs
to be committed or synced. If iCloud syncing it is a nuisance, iCloud skips any path
containing `.nosync`:

```bash
mkdir -p data/db.nosync && mv data/finance.db data/db.nosync/
```

---

## PostgreSQL

The SQLite build is the zero-setup default. **Postgres is the better target if you have it** —
and this project has a specific reason to prefer it:

| | SQLite | PostgreSQL |
|---|---|---|
| Money | `REAL` (binary float) | `NUMERIC(18,2)` — **exact decimal** |
| Dates | `TEXT` | `DATE` — `date_trunc`, intervals, `BETWEEN` |
| Foreign keys | declared, not enforced | **enforced** |
| Fuzzy vendor match | `LIKE` → full scan | `pg_trgm` GIN index → `ILIKE` stays fast |

The money type is the one that matters, though the honest version is narrower than the usual
scare story. Measured on this data, `SUM(amount_total)` over 1,019,354 rows:

```
SQLite REAL   35858536332.6699981689453125
exact         35858536332.67
drift             -0.0000018310546875     # sub-cent, rounds away at 2dp
```

So SQLite is *fine today* — the drift is millionths of a cent and invisible once you round.
`NUMERIC` buys you a **guarantee** rather than a bugfix: drift compounds with repeated
arithmetic (variance calculations, running balances, percentage-of-total, currency
conversion), and this is a domain where you'd rather not have to reason about when it stops
being invisible. The other three rows in that table — real `DATE` types, enforced foreign
keys, and indexed fuzzy matching — are the bigger day-to-day wins.

### Setup

```bash
brew install postgresql@18 && brew services start postgresql@18
export PATH="/opt/homebrew/opt/postgresql@18/bin:$PATH"
createdb tbx_finance

python3 scripts/04_load_postgres.py --emit-sql
./data/postgres/load.sh "postgresql://$USER@localhost:5432/tbx_finance"
```

`load.sh` finds `psql` from `$PSQL`, then `$PATH`, then the copy bundled inside pgAdmin —
so it works even with no server tooling on your `PATH`.

Prefer a direct Python load? `pip install "psycopg[binary]"`, then:

```bash
python3 scripts/04_load_postgres.py --dsn "postgresql://$USER@localhost:5432/tbx_finance"
```

**Connecting pgAdmin:** Add New Server → Host `localhost`, Port `5432`, Database
`tbx_finance`, Username your macOS username, no password (Homebrew initdb sets local
connections to `trust`).

> **One behavioural difference.** The Postgres load maps empty CSV fields to `NULL`
> (`COPY ... NULL ''`), which is idiomatic. So `WHERE purchase_order <> ''` in SQLite becomes
> `WHERE purchase_order IS NOT NULL` in Postgres. `bank_amount` and `variance` are NULL in
> both.

### Verified parity

The Postgres build was cross-checked against the SQLite golden answers — identical row counts
across all 7 tables, and question 1 matches to the cent:

```
                       rows      vouchers   vendors    total_paid
SQLite (golden)        44,807    43,218     2,757      1,068,521,181.57
PostgreSQL             44,807    43,218     2,757      1,068,521,181.57
```

All 6 foreign keys were created without a single violation, which enforces at the engine level
what `03_validate.py` could only assert by query — Postgres would have rejected the load
otherwise. `EXPLAIN ANALYZE` confirms both critical indexes engage: the date range uses
`idx_txn_date` (18.6 ms), and `vendor_name ILIKE '%mckesson%'` uses the `pg_trgm` GIN index
via Bitmap Index Scan (163 ms over 123k matching rows) instead of a sequential scan.

Load time is ~20 seconds for all 1M rows. Database size 655 MB.

---

## Querying it

```python
import sqlite3
con = sqlite3.connect("data/finance.db")

con.execute("""
    SELECT vendor_name, ROUND(SUM(amount_paid), 2) AS spend
    FROM transactions
    WHERE substr(transaction_date, 1, 7) = '2026-08'
    GROUP BY vendor_name ORDER BY spend DESC LIMIT 10
""").fetchall()
```

Two views ship ready for the two reference questions in the problem statement:

- `v_unreconciled` — every non-reconciled item with amount, age, and exception reason
- `v_monthly_spend` — pre-grouped month × vendor × category × department

---

## Notes for building the assistant

The evaluation weights **accuracy & grounding at 30%**, the single largest slice. These are
the traps in this data that will cost you those points:

1. **`amount_paid` vs `amount_total`.** "Spent" means paid. `amount_total` includes pending
   and retainage — money not yet out the door.
2. **`voucher_id` is not unique.** One voucher spans multiple line items. Count vouchers with
   `COUNT(DISTINCT voucher_id)`.
3. **Negative amounts are real.** Reversals and credit memos. `SUM()` is correct;
   `SUM(ABS())` is not. Don't filter them out silently.
4. **`fiscal_year` is the budget year charged, not a date range.** 98.3% of rows align with
   the Jul–Jun window of their `transaction_date`, but 1.7% are back-dated — recent payments
   settling obligations booked as far back as FY2018. The two readings of "spend in FY2026"
   differ by **$1.34B** ($16.82B by `fiscal_year` column vs $18.16B by date window). Neither
   is wrong; they answer different questions. "Last year" is ambiguous three ways here, and a
   good assistant says which basis it used instead of picking silently.
5. **Data ends 2026-08-31.** Questions about later periods must return *"no data for that
   period"*, not `$0.00`. Zero and unknown are different answers, and conflating them is
   exactly the failure the grounding criterion punishes. Question 12 in
   [SAMPLE_QUESTIONS.md](data/SAMPLE_QUESTIONS.md) is a guardrail test for this.
6. **NULL ≠ 0.** `bank_amount` and `variance` are NULL for unmatched items. `AVG(variance)`
   silently skips them — be explicit about the denominator.
7. **Filter dates with ranges, never `substr()`.** `substr(transaction_date,1,7) = '2026-08'`
   is not sargable — SQLite scans all 1M rows (**512 ms**). The equivalent
   `transaction_date >= '2026-08-01' AND transaction_date < '2026-09-01'` hits `idx_txn_date`
   and returns in **16 ms**, 31× faster, same answer. Since the assistant generates SQL,
   put this rule in the prompt or a query template — "answer instantly" is in the spec, and
   month filtering is the single most common thing it will do. (`substr()` in `SELECT` or
   `GROUP BY` is fine; it's only `WHERE` that hurts.)

Architecturally, the requirement that the model "explains a computed result rather than
calculating one itself" means: **compute in SQL, pass the result rows to the model**. Never
hand raw transactions to the LLM and ask it to add them up. That's also what makes the
lightweight-model constraint (20% of the score) achievable — a small model narrating a
correct table beats a large one doing mental arithmetic.

---

## Swapping in the organizers' dataset

Keep every query behind the seven-table schema in
[DATA_DICTIONARY.md](data/DATA_DICTIONARY.md). When the real data lands, write a new
normalizer that emits the same seven CSVs and re-run `02_normalize.py`'s SQLite step. Nothing
downstream changes.
