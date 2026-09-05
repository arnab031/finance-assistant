"""
Build (or rebuild) the DuckDB analytical replica from MySQL.

    PYTHONPATH=. ./.venv/bin/python scripts/11_duck_build.py
    PYTHONPATH=. ./.venv/bin/python scripts/11_duck_build.py --bench

The replica is disposable - it is a copy, never a source. Rebuild it whenever
the MySQL data changes; there is no incremental sync because a full rebuild is
cheaper than the machinery to avoid one.

--bench runs the same aggregates against MySQL and DuckDB and prints both, so a
claim about which is faster is always backed by a number from this machine
rather than from a benchmark someone else ran.
"""

from __future__ import annotations

import asyncio
import sys
import time

from api.db import db
from api.duck import duck

BENCH = {
    "group by transaction_type, no filter":
        "SELECT t.transaction_type, SUM(t.debit_amount) AS value, COUNT(*) AS n "
        "FROM v_txn t GROUP BY 1",
    "scalar, no filter":
        "SELECT SUM(t.debit_amount) AS value, COUNT(*) AS n FROM v_txn t",
    "group by bank, no filter":
        "SELECT t.bank_name, SUM(t.debit_amount) AS value, COUNT(*) AS n "
        "FROM v_txn t GROUP BY 1",
    "one month + group by type":
        "SELECT t.transaction_type, SUM(t.debit_amount) AS value, COUNT(*) AS n "
        "FROM v_txn t WHERE t.transaction_date >= '2026-08-01' "
        "AND t.transaction_date < '2026-09-01' GROUP BY 1",
}


async def main() -> int:
    await duck.connect()
    info = await duck.build()
    print(f"\nbuilt in {info['build_ms'] / 1000:.1f}s: "
          + ", ".join(f"{k}={v:,}" for k, v in info["counts"].items()))

    if "--bench" in sys.argv:
        await db.connect()
        print(f"\n{'query':<38} {'MySQL':>10} {'DuckDB':>10}  {'speedup':>8}")
        print("-" * 72)
        for name, sql in BENCH.items():
            t0 = time.perf_counter()
            await db.fetch_timed(sql)
            my = time.perf_counter() - t0

            await duck.fetch_timed(sql)                      # warm
            t0 = time.perf_counter()
            await duck.fetch_timed(sql)
            dk = time.perf_counter() - t0

            print(f"{name:<38} {my:>9.3f}s {dk:>9.3f}s  {my / dk:>7.0f}x")
        await db.close()

    await duck.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
