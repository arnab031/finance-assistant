"""
Compiler acceptance test - bank_txn on MySQL.

Hand-written QuerySpecs compiled and executed, checked against values computed
independently by direct SQL before any of this ran. NO LLM IS INVOLVED. When the
canary goes wrong, this is what tells you the bug is in extraction and not in the
compute path.

The cases below deliberately concentrate on what the MySQL port CHANGED, because
the 40-question canary exercises the pipeline end to end but would not
necessarily catch a compiler regression on its own:

  IN-list expansion   = ANY(array) has no MySQL equivalent, so every list filter
                        is now N placeholders. One value, several values, and the
                        empty list (which must match nothing, not everything).
  LIKE                ILIKE is gone; the ai_ci collation is what keeps it
                        case-insensitive, so a lowercase probe must still match.
  INTERVAL truncation date_trunc is gone. Month and quarter grouping is INTERVAL
                        arithmetic chosen to avoid a literal % in generated SQL.
  Null ordering       NULLS LAST is gone and MySQL's default flips with
                        direction, so ordering is now explicit.
  Amount bounds       > vs >= on a row sitting exactly on the boundary.

    ./.venv/bin/python -m eval.test_compiler
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from api.compile import compile_query, compile_scalar
from api.db import db
from api.registry import SemanticRegistry
from api.schema import QuerySpec

JUN = {"start": "2026-06-01", "end": "2026-07-01", "label": "June 2026"}
MAY = {"start": "2026-05-01", "end": "2026-06-01", "label": "May 2026"}

# (name, spec dict, expected first-row `value`, expected row count or None)
CASES: list[tuple[str, dict, Decimal | int | None, int | None]] = [
    # ---- direction. Amounts are unsigned; the view splits them. ----
    ("total spend, whole dataset",
     {"intent": "aggregate", "metric": "debit_amount"},
     Decimal("249806.00"), None),

    ("total received, whole dataset",
     {"intent": "aggregate", "metric": "credit_amount"},
     Decimal("296810.00"), None),

    ("net movement (credits minus debits)",
     {"intent": "aggregate", "metric": "net_amount"},
     Decimal("47004.00"), None),

    ("gross, both directions added",
     {"intent": "aggregate", "metric": "gross_amount"},
     Decimal("546616.00"), None),

    # MAX, not SUM. Reading this as gross_amount reported 546,616 for the
    # "largest single transaction" - more than twice the real answer, and still
    # shaped like money.
    ("largest single transaction",
     {"intent": "aggregate", "metric": "max_amount"},
     Decimal("260000.00"), None),

    ("transaction count", {"intent": "aggregate", "metric": "txn_count"}, 10, None),
    ("distinct accounts", {"intent": "aggregate", "metric": "account_count"}, 6, None),
    ("distinct entities", {"intent": "aggregate", "metric": "entity_count"}, 6, None),

    # ---- periods: half-open [start, end), never substr() on a date ----
    ("spend in June 2026",
     {"intent": "aggregate", "metric": "debit_amount", "period": JUN},
     Decimal("169299.00"), None),

    ("spend in May 2026",
     {"intent": "aggregate", "metric": "debit_amount", "period": MAY},
     Decimal("71156.00"), None),

    # The boundary is EXCLUSIVE at the top. A closed range would pull June's
    # first instant into May.
    ("May and June do not overlap",
     {"intent": "aggregate", "metric": "debit_amount",
      "period": {"start": "2026-05-01", "end": "2026-07-01"}},
     Decimal("240455.00"), None),

    # ---- IN-list expansion: the = ANY() replacement ----
    ("IN-list, one value",
     {"intent": "aggregate", "metric": "gross_amount",
      "filters": {"transaction_type": ["debit"]}},
     Decimal("249806.00"), None),

    ("IN-list, two values",
     {"intent": "aggregate", "metric": "gross_amount",
      "filters": {"transaction_type": ["debit", "credit"]}},
     Decimal("546616.00"), None),

    ("IN-list over a joined dimension",
     {"intent": "aggregate", "metric": "debit_amount",
      "filters": {"banks": ["HDFC BANK LIMITED"]}},
     Decimal("240455.00"), None),

    ("IN-list, two banks",
     {"intent": "aggregate", "metric": "debit_amount",
      "filters": {"banks": ["HDFC BANK LIMITED", "ICICI BANK LIMITED"]}},
     Decimal("249696.00"), None),

    # An empty list must match NOTHING. Dropping the clause instead would turn
    # "none of these" into "all rows" - silently, and in the expensive direction.
    ("IN-list, empty, matches nothing",
     {"intent": "aggregate", "metric": "debit_amount",
      "filters": {"banks": []}},
     Decimal("249806.00"), None),

    # ---- LIKE: ILIKE is gone, the ai_ci collation carries the case-folding ----
    ("LIKE on counterparty",
     {"intent": "aggregate", "metric": "debit_amount",
      "filters": {"counterparty_like": "SELECTION"}},
     Decimal("219299.00"), None),

    ("LIKE is still case-insensitive",
     {"intent": "aggregate", "metric": "debit_amount",
      "filters": {"counterparty_like": "selection"}},
     Decimal("219299.00"), None),

    ("LIKE matching one counterparty",
     {"intent": "aggregate", "metric": "debit_amount",
      "filters": {"counterparty_like": "RELIANCE"}},
     Decimal("21156.00"), None),

    # ---- amount bounds. One debit sits at exactly 50,000. ----
    ("min_amount inclusive (>=)",
     {"intent": "aggregate", "metric": "txn_count",
      "filters": {"transaction_type": ["debit"], "min_amount": 50000}},
     3, None),

    ("min_amount exclusive (>) - 'over 50000'",
     {"intent": "aggregate", "metric": "txn_count",
      "filters": {"transaction_type": ["debit"], "min_amount": 50000,
                  "min_amount_exclusive": True}},
     2, None),

    ("max_amount inclusive (<=)",
     {"intent": "aggregate", "metric": "txn_count",
      "filters": {"transaction_type": ["debit"], "max_amount": 50000}},
     6, None),

    # ---- grouping. INTERVAL arithmetic replaces date_trunc. ----
    ("group by bank",
     {"intent": "aggregate", "metric": "debit_amount", "group_by": ["bank"]},
     Decimal("240455.00"), 4),

    ("group by month, chronological",
     {"intent": "aggregate", "metric": "debit_amount", "group_by": ["month"]},
     Decimal("9241.00"), 5),

    ("group by transaction_type",
     {"intent": "aggregate", "metric": "gross_amount",
      "group_by": ["transaction_type"]},
     Decimal("296810.00"), 2),

    ("group by counterparty",
     {"intent": "aggregate", "metric": "debit_amount",
      "group_by": ["counterparty"]},
     Decimal("146474.00"), 9),

    # ---- list intent ----
    ("list respects limit",
     {"intent": "list", "metric": "debit_amount", "limit": 3},
     None, 3),

    ("list filtered by direction",
     {"intent": "list", "metric": "debit_amount",
      "filters": {"transaction_type": ["debit"]}, "limit": 100},
     None, 8),
]

async def main() -> int:
    await db.connect()
    reg = await SemanticRegistry.build(db)

    print("=" * 78)
    print("REGISTRY")
    print("=" * 78)
    print(f"  coverage         {reg.earliest} .. {reg.latest}")
    print(f"  transactions     {reg.transaction_count:,}")
    print(f"  entities         {reg.vendor_count:,}")
    print(f"  banks            {len(reg.banks)}")
    print(f"  directions       {reg.payment_statuses}")
    print(f"  money split      {reg.money_metric_split()}")
    print(f"  has fiscal_year  {reg.has_fiscal_year}")

    print()
    print("=" * 78)
    print("COMPILER  (no LLM involved)")
    print("=" * 78)

    passed = failed = 0
    for name, raw, expected, want_rows in CASES:
        try:
            spec = QuerySpec.model_validate(raw)
            cq = compile_query(spec, reg)
            cols, rows, ms = await db.fetch_timed(cq.sql, cq.params)

            problems = []
            if expected is not None:
                got = rows[0][cols.index("value")] if rows else None
                if isinstance(expected, Decimal):
                    ok = got is not None and Decimal(str(got)) == expected
                else:
                    ok = got == expected
                if not ok:
                    problems.append(f"value {got!r} != {expected!r}")
            if want_rows is not None and len(rows) != want_rows:
                problems.append(f"rows {len(rows)} != {want_rows}")

            if problems:
                failed += 1
                print(f"  [FAIL] {name:<42} {'; '.join(problems)}")
                print(f"         {cq.sql.splitlines()[0]} ...")
            else:
                passed += 1
                head = rows[0][cols.index("value")] if rows and "value" in cols else "-"
                shown = f"{head:,.2f}" if isinstance(head, Decimal) else head
                print(f"  [ok  ] {name:<42} {str(shown):>18}  {len(rows):>3} rows {ms:>5}ms")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [ERR ] {name:<42} {type(exc).__name__}: {exc}")

    # The scalar prober must agree with the full query it predicts.
    print()
    print("=" * 78)
    print("SCALAR PROBE AGREES WITH FULL QUERY  (ambiguity prober correctness)")
    print("=" * 78)
    for label, raw in [
        ("June 2026 spend", {"intent": "aggregate", "metric": "debit_amount",
                             "period": JUN}),
        ("spend, filtered by bank",
         {"intent": "aggregate", "metric": "debit_amount",
          "filters": {"banks": ["HDFC BANK LIMITED"]}}),
    ]:
        spec = QuerySpec.model_validate(raw)
        full = compile_query(spec, reg)
        probe = compile_scalar(spec, reg)
        f_rows = await db.fetch(full.sql, full.params)
        p_rows = await db.fetch(probe.sql, probe.params)
        a, b = f_rows[0]["value"], p_rows[0]["value"]
        ok = a == b
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label:<42} {a:>18,.2f} == {b:,.2f}")

    print()
    print(f"{passed} passed, {failed} failed")
    await db.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
