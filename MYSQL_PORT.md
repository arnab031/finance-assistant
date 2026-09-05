# MySQL port — complete

The team moved off PostgreSQL. The database, the driver and the SQL the
application generates are all MySQL now. `DATABASE_URL` points at it and every
test runs against it.

```
container  tbx-mysql          image mysql:8.4 (8.4.11)     volume tbx-mysql-data
endpoint   mysql://tbx:tbx@127.0.0.1:3306/tbx_live         root: root / tbxroot
```

```bash
./run.sh                # starts the container, ollama, api, web
docker exec -it tbx-mysql mysql -utbx -ptbx tbx_live
```

## Measured after the port

| | |
|---|---|
| Canary (40 golden questions) | **40/40** |
| `eval.test_compiler` | **30/30** |
| `eval.test_narrate` | **18/18** |
| `eval.test_extract` | **12/12** |
| `eval.test_semantic` | **9/9** |

Values verified identical to the PostgreSQL database: 10 transactions, debits
249,806.00, credits 296,810.00, max 260,000.00, coverage 2025-12-03 → 2026-06-24,
counterparty extraction 10/10.

## What changed, and why it mattered

Schema in `api/sql/mysql/` (app) and `api/sql/mysql/live/` (domain), applied on
every boot by `Database.migrate()`. One file per concern rather than the
Postgres set's six: those grew a column at a time against a live database, and
MySQL has no `ADD COLUMN IF NOT EXISTS` to make repeated `ALTER`s idempotent.
Everything is `CREATE ... IF NOT EXISTS` / `INSERT IGNORE`, because a migration
that runs on every start must never drop anything.

| Postgres | MySQL | Note |
|---|---|---|
| psycopg3 + `AsyncConnectionPool` | aiomysql | pyformat `%(name)s` is shared, so no call site changed |
| `= ANY(%(x)s)` | `IN (%(x_0)s, …)` | `_in_list()` in `api/compile.py`. Empty list compiles to `1 = 0` |
| `date_trunc('month', x)` | `DATE(x) - INTERVAL (DAYOFMONTH(x)-1) DAY` | see *the % trap* |
| `ILIKE` | `LIKE` | `utf8mb4_0900_ai_ci` keeps it case-insensitive |
| `x::date` / `::text` / `::int` | `CAST(x AS …)` | |
| `ORDER BY … NULLS LAST` | `ORDER BY x IS NULL, x …` | MySQL's default flips with direction |
| `ON CONFLICT … DO UPDATE` | `ON DUPLICATE KEY UPDATE` | |
| `RETURNING id` | `cursor.lastrowid` | `Database.insert_returning_id()` |
| `PERCENTILE_DISC(p) WITHIN GROUP` | `MIN(x)` over `CUME_DIST() >= p` | *is* the definition, so values are identical |
| `COUNT(*) FILTER (WHERE p)` | `SUM(CASE WHEN p THEN 1 ELSE 0 END)` | |
| `DISTINCT ON (k)` | `ROW_NUMBER() OVER (PARTITION BY k …)` | |
| `cardinality(text[])` | `JSON_LENGTH(json)` | `TEXT[]` columns became `JSON` |
| `to_regclass`, `table_schema='public'` | `information_schema` + `TABLE_SCHEMA = DATABASE()` | |
| `pg_trgm`, `pgvector` | FULLTEXT / JSON | genuinely degraded, see below |

### The % trap

PyMySQL interpolates the query string whenever parameters are bound, so a
literal `%` in SQL is read as a placeholder and raises. `_args()` in
`api/db.py` handles the parameterless case, but the real fix is upstream: **no
generated SQL contains a literal `%`.** That is why month and quarter truncation
use `INTERVAL` arithmetic instead of the obvious `DATE_FORMAT(x, '%Y-%m-01')`,
and why the counterparty prefix test is `LEFT(c, CHAR_LENGTH(q)) = q` rather
than `LIKE CONCAT(q, '%')`.

### What genuinely degraded

**Fuzzy counterparty matching.** `pg_trgm` gave real similarity scoring and a GIN
index behind `ILIKE '%…%'`. MySQL has neither. FULLTEXT indexes exist on
`description` and `counterparty`, but `MATCH … AGAINST` matches whole words, so
it will **not** find `RELIANCE` inside `RELIANCEDIGITAL RETAIL LTD` — exactly the
sample data. `Profile.entity_sql` therefore ranks LIKE candidates by a
positional score (exact > prefix > anywhere, shortest label wins ties). Correct,
but it scans; on a large export this is the first thing to revisit.

**Vector search.** MySQL 8.4 has no vector type (9.0 adds one, still without an
ANN index), so `semantic_index.embedding` is a JSON array with nothing in SQL to
search it. Cosine runs in Python over the loaded candidate set — see
`api/semantic.py`. Fine at this vocabulary size, not fine at a million labels;
`MAX_LABELS` refuses rather than quietly getting slow.

### Two bugs the port exposed

Neither was caused by the port; both were found while re-testing.

1. **Every decline described the wrong database.** `api/routes/ask.py` hardcoded
   the vendor_payments wording, so on bank data it told users the schema holds
   "vendor payments, chart of accounts, departments and funds". Both profiles
   already carried an `unsupported_note` that nothing read. The canary never
   caught it because it grades the intent, not the text.
2. **`counterparty` had no way to be populated on a fresh database.** It is
   derived by `api/narration.py`, so no migration can fill it. `ensure_counterparty()`
   now runs at startup, incrementally and only on `NULL` rows.

## Removed

The stand-in dataset (`vendor_payments` / `tbx_finance`) was PostgreSQL-only and
was never ported, so everything that existed only to serve it is gone: its
profile, its build scripts (`01_download` … `04_load_postgres`), its 50-question
set, the PostgreSQL DDL under `api/sql/` and `api/sql/live/`, the pg_trgm/pgvector
index builder, and the vendor entity resolver with its `detect_entity` ambiguity
detector and `vendor_query`/`vendor_ids` spec fields. The superseded data
scripts went too: `08_tokenize` / `08_encrypt` / `09_decrypt_at_rest` (the design
became plaintext at rest, encrypted at the LLM boundary in `api/crypto.py`) and
`10_counterparty` (now `ensure_counterparty()` at boot).

`DATASET` is a one-value `Literal` now. The registry vocabularies and the
temporal / metric / scope detectors stayed: they arm themselves from
`capability_sql`, so on this schema they disarm on their own. That is the profile
abstraction working, not dead code.

## Rebuilding from scratch

```bash
docker rm -f tbx-mysql && docker volume rm tbx-mysql-data
docker run -d --name tbx-mysql -e MYSQL_ROOT_PASSWORD=tbxroot \
  -e MYSQL_DATABASE=tbx_live -e MYSQL_USER=tbx -e MYSQL_PASSWORD=tbx \
  -p 3306:3306 -v tbx-mysql-data:/var/lib/mysql mysql:8.4 \
  --character-set-server=utf8mb4 --collation-server=utf8mb4_0900_ai_ci
```

Then start the API: it applies every migration, seeds, and backfills
`counterparty` on boot. Do **not** apply the migrations as `root` — root holds
`SYSTEM_USER` in MySQL 8, and the `tbx` account then cannot replace the views it
created.
