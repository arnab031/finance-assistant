#!/usr/bin/env python3
"""
Load data/processed/*.csv into PostgreSQL.

Two modes:

  1. Direct load (needs `pip install "psycopg[binary]"`):
         python3 scripts/04_load_postgres.py --dsn postgresql://user:pw@localhost:5432/tbx_finance
     Connection also read from standard PG* env vars if --dsn is omitted.

  2. No driver installed - emit SQL + a psql loader script instead:
         python3 scripts/04_load_postgres.py --emit-sql
     Writes data/postgres/schema.sql and data/postgres/load.sh, which use the
     psql bundled with pgAdmin. No pip install required.

Why Postgres beats the SQLite build for this project:
  * money is NUMERIC(18,2), not REAL - exact decimal arithmetic, no float drift
  * dates are real DATE columns - date_trunc, intervals, BETWEEN all work
  * primary and foreign keys are actually enforced
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROC = ROOT / "data" / "processed"
PGDIR = ROOT / "data" / "postgres"

PGADMIN_PSQL = "/Applications/pgAdmin 4.app/Contents/SharedSupport/psql"

# ---------------------------------------------------------------- schema

SCHEMA_SQL = """
-- TBX hackathon finance dataset - PostgreSQL schema
-- Money is NUMERIC(18,2): exact decimal, no floating-point drift.
-- Dates are DATE: date_trunc / intervals / BETWEEN work natively.

DROP TABLE IF EXISTS reconciliation  CASCADE;
DROP TABLE IF EXISTS transactions    CASCADE;
DROP TABLE IF EXISTS vendor_payouts  CASCADE;
DROP TABLE IF EXISTS vendors         CASCADE;
DROP TABLE IF EXISTS chart_of_accounts CASCADE;
DROP TABLE IF EXISTS departments     CASCADE;
DROP TABLE IF EXISTS funds           CASCADE;

CREATE TABLE vendors (
    vendor_id           TEXT PRIMARY KEY,
    vendor_name         TEXT NOT NULL,
    is_nonprofit        CHAR(1),
    primary_category    TEXT,
    payment_terms       TEXT,
    first_payment_date  DATE,
    last_payment_date   DATE,
    transaction_count   INTEGER,
    total_amount        NUMERIC(18,2)
);

CREATE TABLE chart_of_accounts (
    account_code   TEXT PRIMARY KEY,
    account_name   TEXT,
    object_code    TEXT,
    object_name    TEXT,
    category_code  TEXT,
    category_name  TEXT,
    account_type   TEXT
);

CREATE TABLE departments (
    department_code TEXT PRIMARY KEY,
    department_name TEXT,
    org_group_code  TEXT,
    org_group_name  TEXT
);

CREATE TABLE funds (
    fund_code           TEXT PRIMARY KEY,
    fund_name           TEXT,
    fund_type_code      TEXT,
    fund_type_name      TEXT,
    fund_category_code  TEXT,
    fund_category_name  TEXT
);

CREATE TABLE transactions (
    transaction_id    TEXT PRIMARY KEY,
    voucher_id        TEXT,
    transaction_date  DATE NOT NULL,
    fiscal_year       TEXT,
    vendor_id         TEXT NOT NULL,
    vendor_name       TEXT NOT NULL,
    account_code      TEXT,
    account_name      TEXT,
    category_name     TEXT,
    department_code   TEXT,
    department_name   TEXT,
    fund_code         TEXT,
    fund_name         TEXT,
    program_code      TEXT,
    program_name      TEXT,
    purchase_order    TEXT,
    contract_number   TEXT,
    contract_title    TEXT,
    amount_paid       NUMERIC(18,2) NOT NULL,
    amount_pending    NUMERIC(18,2) NOT NULL,
    amount_retainage  NUMERIC(18,2) NOT NULL,
    amount_total      NUMERIC(18,2) NOT NULL,
    payment_status    TEXT NOT NULL
);

CREATE TABLE reconciliation (
    reconciliation_id     TEXT PRIMARY KEY,
    transaction_id        TEXT NOT NULL,
    voucher_id            TEXT,
    vendor_id             TEXT,
    transaction_date      DATE,
    ledger_amount         NUMERIC(18,2),
    bank_amount           NUMERIC(18,2),
    variance              NUMERIC(18,2),
    reconciliation_status TEXT NOT NULL,
    bank_statement_ref    TEXT,
    matched_date          DATE,
    days_outstanding      INTEGER,
    exception_reason      TEXT
);

CREATE TABLE vendor_payouts (
    payout_id         TEXT PRIMARY KEY,
    vendor_id         TEXT NOT NULL,
    vendor_name       TEXT,
    payout_date       DATE NOT NULL,
    fiscal_year       TEXT,
    transaction_count INTEGER,
    department_count  INTEGER,
    gross_amount      NUMERIC(18,2),
    paid_amount       NUMERIC(18,2),
    pending_amount    NUMERIC(18,2),
    retainage_amount  NUMERIC(18,2),
    payment_method    TEXT,
    payout_status     TEXT
);
"""

CONSTRAINTS_SQL = """
-- Foreign keys added after load (much faster than validating per row)
ALTER TABLE transactions
    ADD CONSTRAINT fk_txn_vendor FOREIGN KEY (vendor_id) REFERENCES vendors(vendor_id),
    ADD CONSTRAINT fk_txn_account FOREIGN KEY (account_code)
        REFERENCES chart_of_accounts(account_code),
    ADD CONSTRAINT fk_txn_dept FOREIGN KEY (department_code)
        REFERENCES departments(department_code),
    ADD CONSTRAINT fk_txn_fund FOREIGN KEY (fund_code) REFERENCES funds(fund_code);

ALTER TABLE reconciliation
    ADD CONSTRAINT fk_rec_txn FOREIGN KEY (transaction_id)
        REFERENCES transactions(transaction_id),
    ADD CONSTRAINT uq_rec_txn UNIQUE (transaction_id);

ALTER TABLE vendor_payouts
    ADD CONSTRAINT fk_pay_vendor FOREIGN KEY (vendor_id) REFERENCES vendors(vendor_id);
"""

INDEXES_SQL = """
CREATE INDEX idx_txn_date       ON transactions (transaction_date);
CREATE INDEX idx_txn_vendor     ON transactions (vendor_id);
CREATE INDEX idx_txn_dept       ON transactions (department_code);
CREATE INDEX idx_txn_account    ON transactions (account_code);
CREATE INDEX idx_txn_status     ON transactions (payment_status);
CREATE INDEX idx_txn_cat        ON transactions (category_name);
CREATE INDEX idx_txn_vendor_date ON transactions (vendor_id, transaction_date);

-- Case-insensitive and fuzzy vendor lookup. pg_trgm makes
-- "WHERE vendor_name ILIKE '%mckesson%'" index-backed instead of a seq scan,
-- which matters because the assistant resolves vendor names from free text.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_txn_vendorname_trgm ON transactions USING gin (vendor_name gin_trgm_ops);
CREATE INDEX idx_ven_name_trgm       ON vendors      USING gin (vendor_name gin_trgm_ops);

CREATE INDEX idx_rec_status ON reconciliation (reconciliation_status);
CREATE INDEX idx_rec_date   ON reconciliation (transaction_date);
CREATE INDEX idx_rec_open   ON reconciliation (reconciliation_status)
    WHERE reconciliation_status <> 'Reconciled';

CREATE INDEX idx_pay_vendor ON vendor_payouts (vendor_id);
CREATE INDEX idx_pay_date   ON vendor_payouts (payout_date);
"""

VIEWS_SQL = """
CREATE OR REPLACE VIEW v_unreconciled AS
SELECT t.transaction_id, t.voucher_id, t.transaction_date, t.vendor_name,
       t.department_name, t.account_name, t.amount_total,
       r.reconciliation_status, r.days_outstanding, r.exception_reason, r.variance
FROM transactions t
JOIN reconciliation r USING (transaction_id)
WHERE r.reconciliation_status <> 'Reconciled';

CREATE OR REPLACE VIEW v_monthly_spend AS
SELECT date_trunc('month', transaction_date)::date AS month,
       vendor_id, vendor_name, category_name, department_name,
       COUNT(*)               AS txn_count,
       SUM(amount_paid)       AS total_paid,
       SUM(amount_total)      AS total_amount
FROM transactions
GROUP BY 1,2,3,4,5;

-- Convenience: the data window, so the assistant can check a question is answerable
CREATE OR REPLACE VIEW v_data_coverage AS
SELECT MIN(transaction_date) AS earliest,
       MAX(transaction_date) AS latest,
       COUNT(*)              AS transaction_count,
       SUM(amount_paid)      AS total_paid
FROM transactions;
"""

# table -> (csv filename, column list in CSV order)
TABLES: list[tuple[str, str]] = [
    ("vendors", "vendors.csv"),
    ("chart_of_accounts", "chart_of_accounts.csv"),
    ("departments", "departments.csv"),
    ("funds", "funds.csv"),
    ("transactions", "transactions.csv"),
    ("reconciliation", "reconciliation.csv"),
    ("vendor_payouts", "vendor_payouts.csv"),
]


def resolve(fname: str) -> Path:
    """Accept either plain .csv or .csv.gz."""
    p = PROC / fname
    if p.exists():
        return p
    gz = PROC / (fname + ".gz")
    if gz.exists():
        return gz
    sys.exit(f"missing {p} (and {gz}). Run scripts/02_normalize.py first.")


def open_csv(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, encoding="utf-8", newline="")


# ---------------------------------------------------------------- direct load


def direct_load(dsn: str | None) -> int:
    try:
        import psycopg
    except ImportError:
        sys.exit(
            'psycopg not installed.\n'
            '  pip install "psycopg[binary]"\n'
            "or run with --emit-sql to use the psql bundled with pgAdmin instead."
        )

    conn = psycopg.connect(dsn) if dsn else psycopg.connect()
    conn.autocommit = False
    print(f"connected: {conn.info.host}:{conn.info.port}/{conn.info.dbname}", flush=True)

    with conn.cursor() as cur:
        print("creating schema ...", flush=True)
        cur.execute(SCHEMA_SQL)

        for table, fname in TABLES:
            path = resolve(fname)
            with open_csv(path) as fh:
                header = fh.readline().rstrip("\r\n")
                cols = header.split(",")
                sql = (f'COPY {table} ({",".join(cols)}) '
                       f"FROM STDIN WITH (FORMAT csv, NULL '')")
                with cur.copy(sql) as cp:
                    while chunk := fh.read(1 << 20):
                        cp.write(chunk)
            n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<20} {n:>10,} rows   <- {path.name}", flush=True)

        print("adding constraints ...", flush=True)
        cur.execute(CONSTRAINTS_SQL)
        print("creating indexes (pg_trgm may need superuser) ...", flush=True)
        cur.execute(INDEXES_SQL)
        print("creating views ...", flush=True)
        cur.execute(VIEWS_SQL)

    conn.commit()
    with conn.cursor() as cur:
        cur.execute("ANALYZE")
    conn.commit()

    with conn.cursor() as cur:
        row = cur.execute("SELECT * FROM v_data_coverage").fetchone()
        print(f"\ncoverage: {row[0]} .. {row[1]}   {row[2]:,} txns   ${row[3]:,.2f} paid")
        size = cur.execute(
            "SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
        print(f"database size: {size}")
    conn.close()
    print("\nDone.")
    return 0


# ---------------------------------------------------------------- emit sql


def emit_sql() -> int:
    PGDIR.mkdir(parents=True, exist_ok=True)

    (PGDIR / "schema.sql").write_text(
        SCHEMA_SQL.lstrip() + "\n", encoding="utf-8")
    (PGDIR / "constraints_indexes_views.sql").write_text(
        CONSTRAINTS_SQL.lstrip() + "\n" + INDEXES_SQL + "\n" + VIEWS_SQL.lstrip() + "\n"
        + "\nANALYZE;\n",
        encoding="utf-8",
    )

    lines = [
        "#!/usr/bin/env bash",
        "# Load the TBX finance dataset into PostgreSQL using psql.",
        "#",
        "#   ./data/postgres/load.sh 'postgresql://postgres@localhost:5432/tbx_finance'",
        "#",
        "# psql is taken from PSQL env var, then PATH, then the copy bundled with pgAdmin.",
        "set -euo pipefail",
        "",
        'DSN="${1:-postgresql://postgres@localhost:5432/tbx_finance}"',
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'PROC="$HERE/../processed"',
        "",
        'if [[ -n "${PSQL:-}" ]]; then :',
        'elif command -v psql >/dev/null 2>&1; then PSQL="psql"',
        f'elif [[ -x "{PGADMIN_PSQL}" ]]; then PSQL="{PGADMIN_PSQL}"',
        'else echo "psql not found - set PSQL=/path/to/psql" >&2; exit 1; fi',
        "",
        'echo "using: $PSQL"',
        '"$PSQL" "$DSN" -v ON_ERROR_STOP=1 -f "$HERE/schema.sql"',
        "",
    ]
    for table, fname in TABLES:
        path = resolve(fname)
        rel = f'$PROC/{path.name}'
        copy = (f"\\\\copy {table} FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')")
        if path.suffix == ".gz":
            lines.append(f'echo "  {table} ..."')
            lines.append(f'gunzip -c "{rel}" | "$PSQL" "$DSN" -v ON_ERROR_STOP=1 '
                         f'-c "{copy}"')
        else:
            lines.append(f'echo "  {table} ..."')
            lines.append(f'"$PSQL" "$DSN" -v ON_ERROR_STOP=1 -c "{copy}" < "{rel}"')
    lines += [
        "",
        '"$PSQL" "$DSN" -v ON_ERROR_STOP=1 -f "$HERE/constraints_indexes_views.sql"',
        '"$PSQL" "$DSN" -c "SELECT * FROM v_data_coverage;"',
        'echo "Done."',
        "",
    ]
    sh = PGDIR / "load.sh"
    sh.write_text("\n".join(lines), encoding="utf-8")
    sh.chmod(0o755)

    print(f"wrote {PGDIR.relative_to(ROOT)}/schema.sql")
    print(f"wrote {PGDIR.relative_to(ROOT)}/constraints_indexes_views.sql")
    print(f"wrote {PGDIR.relative_to(ROOT)}/load.sh")
    print("\nNext:")
    print("  createdb tbx_finance   # or create the DB in pgAdmin")
    print("  ./data/postgres/load.sh 'postgresql://postgres@localhost:5432/tbx_finance'")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", help="postgresql://user:pw@host:port/dbname")
    ap.add_argument("--emit-sql", action="store_true",
                    help="write schema.sql + load.sh instead of connecting")
    args = ap.parse_args()

    if args.emit_sql:
        return emit_sql()
    dsn = args.dsn or os.environ.get("DATABASE_URL")
    return direct_load(dsn)


if __name__ == "__main__":
    sys.exit(main())
