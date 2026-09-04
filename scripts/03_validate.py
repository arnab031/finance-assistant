#!/usr/bin/env python3
"""
Sanity-check data/finance.db and emit a golden question -> SQL -> answer set.

Two jobs:
  1. Integrity checks (referential integrity, null/range checks, row counts).
  2. Run the reference questions from the problem statement and write
     data/SAMPLE_QUESTIONS.md - a grounded answer key you can score the
     assistant against.

Stdlib only - no pip install required.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "finance.db"
OUT_MD = ROOT / "data" / "SAMPLE_QUESTIONS.md"

# ---------------------------------------------------------------- integrity

CHECKS: list[tuple[str, str, str]] = [
    ("transactions with no matching vendor",
     "SELECT COUNT(*) FROM transactions t LEFT JOIN vendors v USING(vendor_id) "
     "WHERE v.vendor_id IS NULL", "== 0"),
    ("transactions with no matching account",
     "SELECT COUNT(*) FROM transactions t LEFT JOIN chart_of_accounts c "
     "ON t.account_code=c.account_code WHERE t.account_code<>'' AND c.account_code IS NULL", "== 0"),
    ("transactions with no matching department",
     "SELECT COUNT(*) FROM transactions t LEFT JOIN departments d "
     "ON t.department_code=d.department_code WHERE t.department_code<>'' "
     "AND d.department_code IS NULL", "== 0"),
    ("transactions with no matching fund",
     "SELECT COUNT(*) FROM transactions t LEFT JOIN funds f "
     "ON t.fund_code=f.fund_code WHERE t.fund_code<>'' AND f.fund_code IS NULL", "== 0"),
    ("reconciliation rows without a transaction",
     "SELECT COUNT(*) FROM reconciliation r LEFT JOIN transactions t USING(transaction_id) "
     "WHERE t.transaction_id IS NULL", "== 0"),
    ("transactions without a reconciliation row",
     "SELECT COUNT(*) FROM transactions t LEFT JOIN reconciliation r USING(transaction_id) "
     "WHERE r.transaction_id IS NULL", "== 0"),
    ("payouts without a vendor",
     "SELECT COUNT(*) FROM vendor_payouts p LEFT JOIN vendors v USING(vendor_id) "
     "WHERE v.vendor_id IS NULL", "== 0"),
    ("amount_total != paid+pending+retainage (>1c drift)",
     "SELECT COUNT(*) FROM transactions WHERE "
     "ABS(amount_total-(amount_paid+amount_pending+amount_retainage)) > 0.011", "== 0"),
    ("reconciled rows with non-zero variance",
     "SELECT COUNT(*) FROM reconciliation WHERE reconciliation_status='Reconciled' "
     "AND variance <> 0", "== 0"),
    ("reconciled rows missing a matched_date",
     "SELECT COUNT(*) FROM reconciliation WHERE reconciliation_status='Reconciled' "
     "AND (matched_date IS NULL OR matched_date='')", "== 0"),
    ("unmatched rows carrying a bank ref they should not",
     "SELECT COUNT(*) FROM reconciliation WHERE reconciliation_status IN "
     "('Unreconciled','Disputed') AND bank_statement_ref <> ''", "== 0"),
    ("blank transaction_date", "SELECT COUNT(*) FROM transactions WHERE transaction_date=''", "== 0"),
    ("blank vendor_name", "SELECT COUNT(*) FROM transactions WHERE vendor_name=''", "== 0"),
    ("payout gross != sum of its transactions",
     "SELECT COUNT(*) FROM (SELECT p.payout_id, p.gross_amount, "
     "ROUND(SUM(t.amount_total),2) AS s FROM vendor_payouts p JOIN transactions t "
     "ON t.vendor_id=p.vendor_id AND t.transaction_date=p.payout_date "
     "GROUP BY p.payout_id) WHERE ABS(gross_amount-s) > 0.011", "== 0"),
]


def run_checks(con: sqlite3.Connection) -> bool:
    print("=" * 74)
    print("INTEGRITY CHECKS")
    print("=" * 74)
    ok = True
    for label, sql, expect in CHECKS:
        got = con.execute(sql).fetchone()[0]
        passed = (got == 0) if expect == "== 0" else True
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}]  {label:<52} {got:>8,}")
    return ok


def profile(con: sqlite3.Connection) -> None:
    print("\n" + "=" * 74)
    print("PROFILE")
    print("=" * 74)
    for t in ("transactions", "reconciliation", "vendor_payouts", "vendors",
              "chart_of_accounts", "departments", "funds"):
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<20} {n:>10,} rows")

    lo, hi = con.execute(
        "SELECT MIN(transaction_date), MAX(transaction_date) FROM transactions").fetchone()
    print(f"\n  date range           {lo} .. {hi}")

    paid, total = con.execute(
        "SELECT ROUND(SUM(amount_paid),2), ROUND(SUM(amount_total),2) FROM transactions"
    ).fetchone()
    print(f"  total paid           ${paid:,.2f}")
    print(f"  total (incl pending) ${total:,.2f}")

    neg = con.execute("SELECT COUNT(*) FROM transactions WHERE amount_paid < 0").fetchone()[0]
    print(f"  negative (reversals) {neg:,}")

    print("\n  payment_status:")
    for s, n, amt in con.execute(
        "SELECT payment_status, COUNT(*), ROUND(SUM(amount_total),2) FROM transactions "
        "GROUP BY 1 ORDER BY 2 DESC"
    ):
        print(f"    {s:<16} {n:>9,}   ${amt:>18,.2f}")

    print("\n  reconciliation_status:")
    for s, n, amt in con.execute(
        "SELECT reconciliation_status, COUNT(*), ROUND(SUM(ledger_amount),2) "
        "FROM reconciliation GROUP BY 1 ORDER BY 2 DESC"
    ):
        print(f"    {s:<20} {n:>9,}   ${amt:>18,.2f}")


# ---------------------------------------------------------------- golden set

GOLDEN: list[tuple[str, str]] = [
    ("How much did we spend on vendor payouts last month?",
     """-- range predicate, not substr(), so idx_txn_date is used: 16ms vs 512ms
SELECT '2026-08'                  AS month,
       COUNT(*)                   AS line_items,
       COUNT(DISTINCT voucher_id) AS vouchers,
       COUNT(DISTINCT vendor_id)  AS vendors,
       ROUND(SUM(amount_paid),2)  AS total_paid
FROM transactions
WHERE transaction_date >= '2026-08-01' AND transaction_date < '2026-09-01'"""),

    ("Which transactions are still unreconciled?",
     """SELECT reconciliation_status,
       COUNT(*)                     AS txns,
       ROUND(SUM(ledger_amount),2)  AS exposure,
       ROUND(AVG(days_outstanding),1) AS avg_days_open
FROM reconciliation
WHERE reconciliation_status <> 'Reconciled'
GROUP BY 1 ORDER BY exposure DESC"""),

    ("Show me the 10 oldest unreconciled items with the amount at risk.",
     """SELECT transaction_id, transaction_date, vendor_name, department_name,
       ROUND(amount_total,2) AS amount, reconciliation_status,
       days_outstanding, exception_reason
FROM v_unreconciled
ORDER BY days_outstanding DESC, amount DESC
LIMIT 10"""),

    ("Who were our top 10 vendors by spend in the last 12 months?",
     """SELECT vendor_name,
       COUNT(*) AS txns,
       ROUND(SUM(amount_paid),2) AS total_paid
FROM transactions
WHERE transaction_date >= '2025-09-01'
GROUP BY vendor_name
ORDER BY total_paid DESC
LIMIT 10"""),

    ("How much did we pay McKesson last quarter, and how does that compare to the quarter before?",
     """SELECT CASE WHEN transaction_date >= '2026-06-01' THEN '2026-Q3 (Jun-Aug)'
            ELSE '2026-Q2 (Mar-May)' END AS period,
       COUNT(*) AS txns,
       ROUND(SUM(amount_paid),2) AS total_paid
FROM transactions
WHERE vendor_name LIKE '%MCKESSON%'
  AND transaction_date >= '2026-03-01' AND transaction_date < '2026-09-01'
GROUP BY 1 ORDER BY 1"""),

    ("What did we spend by expense category last month?",
     """SELECT category_name,
       COUNT(*) AS txns,
       ROUND(SUM(amount_paid),2) AS total_paid
FROM transactions
WHERE transaction_date >= '2026-08-01' AND transaction_date < '2026-09-01'
GROUP BY 1 ORDER BY total_paid DESC"""),

    ("Which departments have the largest unreconciled exposure?",
     """SELECT department_name,
       COUNT(*) AS open_items,
       ROUND(SUM(amount_total),2) AS exposure
FROM v_unreconciled
GROUP BY 1 ORDER BY exposure DESC LIMIT 10"""),

    ("Show payouts to any vendor that were unusually large versus their own history.",
     """WITH stats AS (
  SELECT vendor_id, AVG(gross_amount) AS mu, COUNT(*) AS n
  FROM vendor_payouts GROUP BY vendor_id HAVING n >= 12
)
SELECT p.payout_date, p.vendor_name, ROUND(p.gross_amount,2) AS payout,
       ROUND(s.mu,2) AS vendor_avg,
       ROUND(p.gross_amount / s.mu, 1) AS times_avg
FROM vendor_payouts p JOIN stats s USING(vendor_id)
WHERE p.gross_amount > s.mu * 10 AND p.gross_amount > 1000000
ORDER BY times_avg DESC LIMIT 10"""),

    ("What is our month-over-month spend trend for the last 6 months?",
     """SELECT substr(transaction_date,1,7) AS month,
       COUNT(*) AS txns,
       ROUND(SUM(amount_paid),2) AS total_paid
FROM transactions
WHERE transaction_date >= '2026-03-01'
GROUP BY 1 ORDER BY 1"""),

    ("How much is still pending or held as retainage?",
     """SELECT payment_status, COUNT(*) AS txns,
       ROUND(SUM(amount_pending),2)  AS pending,
       ROUND(SUM(amount_retainage),2) AS retainage
FROM transactions
WHERE payment_status IN ('Pending','Retainage Held')
GROUP BY 1"""),

    ("Which payments had a variance against the bank statement?",
     """SELECT r.transaction_id, r.transaction_date, t.vendor_name,
       ROUND(r.ledger_amount,2) AS ledger, ROUND(r.bank_amount,2) AS bank,
       ROUND(r.variance,2) AS variance, r.exception_reason
FROM reconciliation r JOIN transactions t USING(transaction_id)
WHERE r.reconciliation_status = 'Partially Matched'
ORDER BY ABS(r.variance) DESC LIMIT 10"""),

    ("[GUARDRAIL] How much did we spend in December 2026?",
     """SELECT COUNT(*) AS txns, ROUND(SUM(amount_paid),2) AS total_paid
FROM transactions
WHERE transaction_date >= '2026-12-01' AND transaction_date < '2027-01-01'"""),
]


def fmt_table(cols: list[str], rows: list[tuple]) -> str:
    if not rows:
        return "_(no rows)_"
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:,.2f}"
        if isinstance(v, int):
            return f"{v:,}"
        return str(v)
    body = [[cell(v) for v in r] for r in rows]
    widths = [max(len(c), *(len(b[i]) for b in body)) for i, c in enumerate(cols)]
    out = ["| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols)) + " |",
           "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    for b in body:
        out.append("| " + " | ".join(b[i].ljust(widths[i]) for i in range(len(cols))) + " |")
    return "\n".join(out)


def write_golden(con: sqlite3.Connection) -> None:
    lo, hi = con.execute(
        "SELECT MIN(transaction_date), MAX(transaction_date) FROM transactions").fetchone()
    parts = [
        "# Sample Questions & Grounded Answers",
        "",
        "Generated by [03_validate.py](../scripts/03_validate.py) directly against "
        "`data/finance.db`.",
        "",
        f"Data window **{lo} .. {hi}**. \"Last month\" = **2026-08**.",
        "",
        "Use this as the answer key: the assistant's numbers should match these exactly. "
        "Every figure below came from SQL, so any deviation is a grounding failure, not a "
        "matter of phrasing.",
        "",
        "---",
        "",
    ]
    for i, (q, sql) in enumerate(GOLDEN, start=1):
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        parts += [f"## {i}. {q}", "", "```sql", sql.strip(), "```", "",
                  fmt_table(cols, rows), ""]
        if q.startswith("[GUARDRAIL]"):
            parts += [
                "> **Expected behaviour:** the data ends 2026-08-31, so there are no December "
                "2026 records. The assistant must say *no data exists for that period* — "
                "returning `$0.00` would be wrong, and inventing a figure worse.",
                "",
            ]
        parts.append("---")
        parts.append("")

    OUT_MD.write_text("\n".join(parts), encoding="utf-8")
    print(f"\nwrote {OUT_MD.relative_to(ROOT)}  ({len(GOLDEN)} questions)")


def main() -> int:
    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} not found. Run scripts/02_normalize.py first.")
    con = sqlite3.connect(DB_PATH)
    ok = run_checks(con)
    profile(con)
    write_golden(con)
    con.close()
    print("\n" + ("ALL CHECKS PASSED" if ok else "*** SOME CHECKS FAILED ***"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
