"""
Phase 3 acceptance test.

Hand-written QuerySpecs compiled and executed against Postgres, checked against
values verified independently (by direct psql, before any of this code existed).
NO LLM IS INVOLVED. When Phase 4 goes wrong, this test is what tells you the
bug is in extraction and not in the compute path.

    ./.venv/bin/python -m eval.test_compiler
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from api.compile import compile_query, compile_scalar
from api.db import db
from api.registry import SemanticRegistry
from api.schema import QuerySpec

AUG = {"start": "2026-08-01", "end": "2026-09-01", "label": "August 2026"}
JUL = {"start": "2026-07-01", "end": "2026-08-01", "label": "July 2026"}

# (name, spec dict, expected first-row `value`, expected row count or None)
CASES: list[tuple[str, dict, Decimal | int | None, int | None]] = [
    ("total paid, whole dataset",
     {"intent": "aggregate", "metric": "amount_paid"},
     Decimal("34332022429.25"), None),

    ("paid in August 2026",
     {"intent": "aggregate", "metric": "amount_paid", "period": AUG},
     Decimal("1068521181.57"), None),

    ("transaction count, August 2026",
     {"intent": "aggregate", "metric": "txn_count", "period": AUG},
     44807, None),

    ("distinct vouchers, August 2026",
     {"intent": "aggregate", "metric": "voucher_count", "period": AUG},
     43218, None),

    ("distinct vendors, August 2026",
     {"intent": "aggregate", "metric": "vendor_count", "period": AUG},
     2757, None),

    # The $1.34B fork - fiscal basis must NOT equal the date-window reading.
    ("FY2026 by fiscal_year column",
     {"intent": "aggregate", "metric": "amount_paid",
      "date_basis": "fiscal_year", "fiscal_year": "2026"},
     Decimal("16823239767.06"), None),

    ("FY2026 by date window Jul25-Jun26",
     {"intent": "aggregate", "metric": "amount_paid",
      "period": {"start": "2025-07-01", "end": "2026-07-01"}},
     Decimal("18160356066.39"), None),

    # Scope ambiguity, both readings.
    ("unreconciled - strict",
     {"intent": "reconcile", "metric": "amount_total", "group_by": [],
      "filters": {"reconciliation_status": ["Unreconciled"]}},
     Decimal("3305352559.84"), None),

    ("unreconciled - broad (3 open statuses)",
     {"intent": "reconcile", "metric": "amount_total", "group_by": [],
      "filters": {"reconciliation_status":
                  ["Unreconciled", "Partially Matched", "Disputed"]}},
     Decimal("4623134976.38"), None),

    # Metric ambiguity: paid vs committed, same window.
    ("August 2026 committed (amount_total)",
     {"intent": "aggregate", "metric": "amount_total", "period": AUG},
     Decimal("1355120029.45"), None),

    # Grouping + ordering.
    ("top vendor in August 2026",
     {"intent": "aggregate", "metric": "amount_paid", "group_by": ["vendor"],
      "period": AUG, "limit": 5},
     Decimal("161034955.43"), 5),

    ("spend by category, August 2026",
     {"intent": "aggregate", "metric": "amount_paid", "group_by": ["category"],
      "period": AUG, "limit": 50},
     None, None),

    ("monthly trend is chronological",
     {"intent": "aggregate", "metric": "amount_paid", "group_by": ["month"],
      "period": {"start": "2026-03-01", "end": "2026-09-01"}, "limit": 12},
     None, 6),

    ("compare August vs July 2026",
     {"intent": "compare", "metric": "amount_paid", "period": AUG, "compare_period": JUL},
     None, 2),

    ("list largest payments in August 2026",
     {"intent": "list", "metric": "amount_paid", "period": AUG, "limit": 10},
     None, 10),

    ("reconciliation breakdown",
     {"intent": "reconcile", "metric": "amount_total"},
     None, 4),

    ("anomaly - payouts vs vendor history",
     {"intent": "anomaly", "metric": "amount_paid", "limit": 10},
     None, 10),

    # Requires the chart_of_accounts join.
    ("group by object (join)",
     {"intent": "aggregate", "metric": "amount_paid", "group_by": ["object"],
      "period": AUG, "limit": 5},
     None, 5),

    # Requires the funds join.
    ("group by fund_type (join)",
     {"intent": "aggregate", "metric": "amount_paid", "group_by": ["fund_type"],
      "period": AUG, "limit": 10},
     None, None),
]


async def main() -> int:
    await db.connect()
    reg = await SemanticRegistry.build(db)

    print("=" * 78)
    print("REGISTRY")
    print("=" * 78)
    print(f"  coverage         {reg.earliest} .. {reg.latest}")
    print(f"  transactions     {reg.transaction_count:,}")
    print(f"  vendors          {reg.vendor_count:,}")
    print(f"  categories       {len(reg.categories)}")
    print(f"  departments      {len(reg.departments)}")
    print(f"  funds            {len(reg.funds)}")
    print(f"  payment statuses {reg.payment_statuses}")
    print(f"  recon statuses   {reg.recon_statuses}")
    print(f"  reconciled label {reg.reconciled_label!r}")
    print(f"  open statuses    {reg.open_recon_statuses()}")
    print(f"  scope ambiguous  {reg.scope_is_ambiguous()}")
    print(f"  money split      {reg.money_metric_split()}")
    print(f"  has fiscal_year  {reg.has_fiscal_year}  ({len(reg.fiscal_years)} years)")

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
        ("August 2026 paid", {"intent": "aggregate", "metric": "amount_paid", "period": AUG}),
        ("FY2026 fiscal", {"intent": "aggregate", "metric": "amount_paid",
                           "date_basis": "fiscal_year", "fiscal_year": "2026"}),
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
