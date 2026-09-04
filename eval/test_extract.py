"""
Phase 4 regression: the four failures measured in the zero-shot baseline probe,
plus guardrail behaviour.

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


# ---- the four measured baseline failures ---------------------------------

@check("BASELINE FIX: 'vendor payouts' is not a vendor name")
async def _():
    r = await ask("How much did we spend on vendor payouts last month?")
    vq = r["spec"]["filters"]["vendor_query"]
    assert vq is None, f"vendor_query should be null, got {vq!r}"
    assert value_of(r) == "1068521181.57", value_of(r)
    return f"vendor_query=None, value={value_of(r)}"


@check("BASELINE FIX: 'Top 5 vendors' is not a vendor name")
async def _():
    r = await ask("Top 5 vendors by spend in the last 12 months")
    s = r["spec"]
    vq = s["filters"]["vendor_query"]
    assert vq is None, f"vendor_query should be null, got {vq!r}"
    assert s["group_by"] == ["vendor"], s["group_by"]
    assert len(r["rows"]) == 5, f"expected 5 rows, got {len(r['rows'])}"
    return f"group_by=vendor, limit honoured, top={r['rows'][0][0]}"


@check("BASELINE FIX: FY2026 emits a year, never Apr-Mar dates")
async def _():
    r = await ask("How much did we pay McKesson in FY2026?")
    s = r["spec"]
    assert s["date_basis"] == "fiscal_year", s["date_basis"]
    assert s["fiscal_year"] == "2026", s["fiscal_year"]
    assert s["period"] is None, f"period must be None, got {s['period']}"
    assert (s["filters"]["vendor_query"] or "").lower().startswith("mckesson")
    return f"fiscal_year={s['fiscal_year']}, period=None, vendor={s['filters']['vendor_query']!r}"


@check("BASELINE FIX: headcount is unsupported, not an aggregate")
async def _():
    r = await ask("What is our headcount?")
    assert r["spec"]["intent"] == "unsupported", r["spec"]["intent"]
    assert "rows" not in r, "unsupported must not run a query"
    return f"intent=unsupported, note={r['notes'][0][:60]}..."


# ---- guardrails ----------------------------------------------------------

@check("GUARDRAIL: out-of-range period says no data, never $0.00")
async def _():
    r = await ask("How much did we spend in December 2026?")
    assert "rows" not in r, "must not execute a query outside coverage"
    assert r["notes"], "expected a coverage note"
    assert "no data" in r["notes"][0].lower(), r["notes"][0]
    return r["notes"][0][:70] + "..."


@check("GUARDRAIL: partial coverage is disclosed")
async def _():
    r = await ask("How much did we spend on vendor payouts last month?")
    assert any("partial coverage" in n.lower() for n in r["notes"]), r["notes"]
    return [n for n in r["notes"] if "artial" in n][0][:70]


# ---- core capability -----------------------------------------------------

@check("reconciliation question routes to reconcile AND asks about scope")
async def _():
    r = await ask("Which transactions are still unreconciled?")
    assert r["spec"]["intent"] == "reconcile", r["spec"]["intent"]
    c = r.get("clarify")
    assert c is not None, "28.5% scope spread should trigger a clarify"
    assert c["kind"] == "scope", c["kind"]
    return f"intent=reconcile, asks scope: {c['options'][0]['preview']} vs {c['options'][1]['preview']}"


@check("metric nuance is disclosed, never asked (no over-asking)")
async def _():
    r = await ask("How much did we spend on vendor payouts last month?")
    assert "clarify" not in r, "metric has a strong default; must not block"
    notes = " ".join(r["notes"]).lower()
    assert "committed" in notes or "paid out" in notes, r["notes"]
    return [n for n in r["notes"] if "committed" in n.lower()][0][:88]


@check("category breakdown groups correctly")
async def _():
    r = await ask("Break down spend by category last month")
    assert r["spec"]["group_by"] == ["category"], r["spec"]["group_by"]
    assert len(r.get("rows", [])) > 5
    return f"{len(r['rows'])} categories, top={r['rows'][0][0]!r}"


@check("month-over-month comparison")
async def _():
    r = await ask("How does August 2026 compare to the month before?")
    s = r["spec"]
    assert s["intent"] == "compare", s["intent"]
    assert s["compare_period"] is not None
    return f"{s['period']['label']} vs {s['compare_period']['label']}"


async def main() -> int:
    passed = failed = 0
    print("=" * 78)
    print("PHASE 4 - EXTRACTION REGRESSION  (qwen2.5:7b-instruct)")
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
