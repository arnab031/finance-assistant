"""
Phase 6 acceptance: narration + numeric provenance.

    ./.venv/bin/python -m eval.test_narrate

Part 1 exercises verify_numbers() directly, including every false positive and
false negative found while building it.

Part 2 is the acceptance criterion proper: a stub model that DELIBERATELY
hallucinates, driven through the real narrate() path, proving the retry and the
template fallback both fire and that no invented figure survives.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, AsyncIterator

from api.narrate import narrate, template_answer, verify_numbers
from api.schema import QuerySpec

COLS = ["value", "txn_count"]
ROWS: list[list[Any]] = [[Decimal("1068521181.57"), 44807]]
CAT_COLS = ["category", "value", "txn_count"]
CAT_ROWS: list[list[Any]] = [
    ["Non-Personnel Services", Decimal("374405726.27"), 12000],
    ["Capital Outlay", Decimal("131376098.72"), 900],
]
CTX = "How much did we spend last month? August 2026"

UNIT_CASES = [
    # --- must be accepted ---
    ("exact figure", "You paid $1,068,521,181.57 across 44,807 transactions in August 2026.",
     COLS, ROWS, True),
    ("rounded to whole dollars", "Spend was $1,068,521,182 last month.", COLS, ROWS, True),
    ("truncated cents", "Capital Outlay was $131,376,098.", CAT_COLS, CAT_ROWS, True),
    ("abbreviated billions", "You spent about $1.07 billion in August 2026.", COLS, ROWS, True),
    ("abbreviated millions", "Roughly $1,068.5 million went out.", COLS, ROWS, True),
    ("structural integer", "The top 2 categories lead.", CAT_COLS, CAT_ROWS, True),
    ("year from the question", "In August 2026 the total was $1,068,521,181.57.",
     COLS, ROWS, True),
    ("empty result", "That query returned no matching records.", COLS, [], True),

    # --- must be caught ---
    ("invented total", "You spent $2,500,000,000.00 in August 2026.", COLS, ROWS, False),
    ("invented percentage (arithmetic)",
     "Spend was $1,068,521,181.57, up 12.4% on the prior month.", COLS, ROWS, False),
    ("invented transaction count",
     "There were 51,200 transactions totalling $1,068,521,181.57.", COLS, ROWS, False),
    ("subtly wrong digit ($1 off)", "You paid $1,068,521,182.57 last month.",
     COLS, ROWS, False),
    ("invented cross-row sum", "The two categories total $505,781,824.99.",
     CAT_COLS, CAT_ROWS, False),
    ("wrong magnitude", "You spent about $1.5 billion in August 2026.", COLS, ROWS, False),
    ("invented whole-dollar figure", "Capital Outlay was $141,376,098.",
     CAT_COLS, CAT_ROWS, False),
]


class StubLLM:
    """Returns scripted narrations so the failure path is testable on demand."""

    name = "stub"

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0

    async def complete_json(self, system, user, schema):  # pragma: no cover
        raise NotImplementedError

    async def stream_text(self, system: str, user: str) -> AsyncIterator[str]:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        for i in range(0, len(reply), 16):
            yield reply[i:i + 16]

    async def close(self) -> None:
        return None


async def main() -> int:
    passed = failed = 0

    print("=" * 78)
    print("PART 1 - verify_numbers()")
    print("=" * 78)
    for label, text, cols, rows, want_ok in UNIT_CASES:
        v = verify_numbers(text, cols, rows, [len(rows), 20], CTX)
        ok = v.ok == want_ok
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        verdict = "accepted" if v.ok else f"REJECTED {v.unverified}"
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label:<34} {verdict}")

    print()
    print("=" * 78)
    print("PART 2 - hallucination injected through the real narrate() path")
    print("=" * 78)
    spec = QuerySpec.model_validate({
        "intent": "aggregate", "metric": "amount_paid",
        "period": {"start": "2026-08-01", "end": "2026-09-01", "label": "August 2026"},
    })

    scenarios = [
        ("model is honest first time",
         ["We spent $1,068,521,181.57 across 44,807 transactions in August 2026."],
         1, True, False),
        ("model hallucinates, then corrects itself",
         ["We spent $2,500,000,000.00 last month, up 14% year on year.",
          "We spent $1,068,521,181.57 across 44,807 transactions."],
         2, True, False),
        ("model hallucinates twice - template fallback",
         ["We spent $2,500,000,000.00 last month.",
          "Actually it was $3,100,000,000.00."],
         2, True, True),
    ]

    for label, replies, want_calls, want_ok, want_template in scenarios:
        llm = StubLLM(replies)
        text, verdict, _ms = await narrate(
            "How much did we spend last month?", spec, COLS, ROWS, llm
        )
        is_template = text == template_answer("q", COLS, ROWS)
        problems = []
        if llm.calls != want_calls:
            problems.append(f"calls {llm.calls} != {want_calls}")
        if verdict.ok != want_ok:
            problems.append(f"ok {verdict.ok} != {want_ok}")
        if is_template != want_template:
            problems.append(f"template {is_template} != {want_template}")
        # The invariant that matters most, regardless of path taken:
        if "2,500,000,000" in text or "3,100,000,000" in text:
            problems.append("INVENTED FIGURE REACHED THE USER")

        if problems:
            failed += 1
            print(f"  [FAIL] {label}: {'; '.join(problems)}")
        else:
            passed += 1
            print(f"  [ok  ] {label}")
            print(f"         calls={llm.calls} verified={verdict.ok} "
                  f"template={is_template}")
            print(f"         -> {text[:88]}")

    print()
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
