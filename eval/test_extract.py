"""
Extraction regression - bank_txn on MySQL.

Deliberately NOT a copy of the canary. The 40 golden questions grade a spec or a
number; these assert on the things a grade cannot see:

  * that a refusal runs NO QUERY at all, rather than running one and reporting
    zero - the difference between "there is no data" and a confident $0.00
  * the note text the user actually reads
  * that a strong default is DISCLOSED rather than turned into a question,
    which is the over-asking failure AMBIGUITY.md is about

Every case here is a bug that existed in this codebase, not a hypothetical.

    ./.venv/bin/python -m eval.test_extract     (API must be running)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

API = "http://localhost:8000"


async def ask(question: str) -> dict[str, Any]:
    """Collect an SSE stream into the pieces we assert on."""
    out: dict[str, Any] = {"notes": [], "stages": []}
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream("POST", f"{API}/api/ask",
                                 json={"question": question}) as r:
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                e = json.loads(line[6:])
                t = e["type"]
                if t == "spec":
                    out["spec"] = e["spec"]
                elif t == "rows":
                    out["rows"] = e["rows"]
                    out["columns"] = e["columns"]
                elif t == "note":
                    out["notes"].append(e["text"])
                elif t == "clarify":
                    out["clarify"] = e
                elif t == "error":
                    out["error"] = e["message"]
                elif t == "stage":
                    out["stages"].append(e["stage"])
    return out


def value_of(res: dict) -> str | None:
    if "rows" not in res or not res["rows"]:
        return None
    return res["rows"][0][res["columns"].index("value")]


CHECKS = []


def check(name: str):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


# ---- absent concepts: the schema knows what it does not hold --------------

@check("absent concept declines WITHOUT running a query")
async def _():
    r = await ask("How much did we spend on vendor payouts last month?")
    assert r["spec"]["intent"] == "unsupported", r["spec"]["intent"]
    # The important half. The model previously agreed there were no vendors,
    # then put "vendor" in counterparty_like and answered with every debit in
    # the month - a confident number for an unanswerable question.
    assert "rows" not in r, "unsupported must not execute a query"
    assert r["notes"], "a decline must say why"
    return f"intent=unsupported, note={r['notes'][0][:66]}..."


@check("absent concept: reconciliation")
async def _():
    r = await ask("Which transactions are still unreconciled?")
    assert r["spec"]["intent"] == "unsupported", r["spec"]["intent"]
    assert "rows" not in r
    return r["notes"][0][:70] + "..."


@check("absent concept: headcount")
async def _():
    r = await ask("What is our headcount?")
    assert r["spec"]["intent"] == "unsupported", r["spec"]["intent"]
    assert "rows" not in r
    return f"intent=unsupported, note={r['notes'][0][:60]}..."


# ---- coverage guardrails --------------------------------------------------

@check("GUARDRAIL: out-of-range period says no data, never 0.00")
async def _():
    r = await ask("How much did we spend in December 2026?")
    assert "rows" not in r, "must not execute a query outside coverage"
    assert r["notes"], "expected a coverage note"
    assert "no data" in r["notes"][0].lower(), r["notes"][0]
    return r["notes"][0][:70] + "..."


@check("GUARDRAIL: partial coverage is disclosed, not silently truncated")
async def _():
    r = await ask("How much did we spend between May 2026 and August 2026?")
    assert any("partial coverage" in n.lower() for n in r["notes"]), r["notes"]
    assert r.get("rows"), "partial coverage still answers, it just says so"
    return [n for n in r["notes"] if "artial" in n][0][:76]


# ---- counterparty: the only name mechanism this schema has ----------------

@check("a company name goes to counterparty_like, and resolves")
async def _():
    r = await ask("How much did we pay Reliance Digital?")
    cp = r["spec"]["filters"].get("counterparty_like")
    assert cp, f"counterparty_like should be set, got {cp!r}"
    assert value_of(r) == "21156.00", value_of(r)
    return f"counterparty_like={cp!r}, value={value_of(r)}"


@check("a generic phrase is NOT a counterparty name")
async def _():
    r = await ask("Who did we pay the most?")
    s = r["spec"]
    cp = s["filters"].get("counterparty_like")
    assert not cp, f"counterparty_like should be empty, got {cp!r}"
    assert s["group_by"] == ["counterparty"], s["group_by"]
    return f"counterparty_like={cp!r}, group_by={s['group_by']}"


# ---- metric and filter nuances that returned wrong money ------------------

@check("'largest transaction' is MAX, not a SUM")
async def _():
    r = await ask("What was our single largest transaction?")
    assert r["spec"]["metric"] == "max_amount", r["spec"]["metric"]
    assert value_of(r) == "260000.00", value_of(r)
    return f"metric=max_amount, value={value_of(r)}"


@check("'over 50000' is exclusive on the boundary row")
async def _():
    r = await ask("List the debits over 50000")
    f = r["spec"]["filters"]
    assert f.get("min_amount") is not None, "min_amount must be populated"
    # One debit sits at exactly 50,000. An inclusive bound returns three rows
    # where two are correct.
    assert len(r["rows"]) == 2, f"expected 2 rows, got {len(r['rows'])}"
    return f"min_amount={f['min_amount']} exclusive={f.get('min_amount_exclusive')}, 2 rows"


@check("'split between credits and debits' actually groups by direction")
async def _():
    r = await ask("Show the split between credits and debits")
    assert r["spec"]["group_by"] == ["transaction_type"], r["spec"]["group_by"]
    assert len(r["rows"]) == 2, f"expected 2 rows, got {len(r['rows'])}"
    return f"group_by=transaction_type, {len(r['rows'])} rows"


@check("no over-asking: a strong default answers instead of blocking")
async def _():
    r = await ask("How much did we spend in June 2026?")
    assert "clarify" not in r, "spend has a strong default; must not block"
    assert value_of(r) == "169299.00", value_of(r)
    return f"answered directly, value={value_of(r)}"


@check("month-over-month comparison")
async def _():
    r = await ask("How does June 2026 compare to the month before?")
    s = r["spec"]
    assert s["intent"] == "compare", s["intent"]
    assert s["compare_period"] is not None
    return f"{s['period']['label']} vs {s['compare_period']['label']}"


async def main() -> int:
    passed = failed = 0
    print("=" * 78)
    print("EXTRACTION REGRESSION - bank_txn / MySQL  (qwen2.5:7b-instruct)")
    print("=" * 78)
    for name, fn in CHECKS:
        try:
            detail = await fn()
            passed += 1
            print(f"  [ok  ] {name}")
            print(f"         {detail}")
        except AssertionError as exc:
            failed += 1
            print(f"  [FAIL] {name}")
            print(f"         {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [ERR ] {name}: {type(exc).__name__}: {exc}")
    print()
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
