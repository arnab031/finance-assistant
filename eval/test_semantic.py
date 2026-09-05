"""
Semantic index: does the fallback separate real matches from noise?

MIN_COSINE is the one magic number in api/semantic.py, and a threshold nobody
re-measures is a threshold that silently stops working when the embedding model
changes. So this does not assert the constant - it RE-DERIVES the separation
from the live index and checks the constant still sits inside it.

    ./.venv/bin/python -m eval.test_semantic
"""

from __future__ import annotations

import asyncio
import json
import sys

from api.db import db
from api.semantic import MIN_COSINE
from api.semantic import cosine
from api.embed.ollama_embed import OllamaEmbedder

# (query, expected entity_key or None if it should find nothing)
PROBES = [
    ("electronics store",      "SELECTION ELECTRONICS DAHISAR EAST"),
    ("mobile phone shop",      "SELECTION MOBILE"),
    ("electricity company",    "SELECTRICITY TWO PRIVATE LIMITED"),
    ("reliance",               "RELIANCEDIGITAL RETAIL LTD SELECT CITY SAKET DELHI"),
    ("gautam",                 "GAUTAM SINGH"),
    # should match NOTHING - these are not in the data at all
    ("zomato",                 None),
    ("airline tickets",        None),
    ("hospital",               None),
    ("software subscription",  None),
]

async def main():
    await db.connect()
    rows = await db.fetch(
        "SELECT entity_key, embedding FROM semantic_index WHERE entity_type='counterparty'")
    labels = [(r["entity_key"],
               json.loads(r["embedding"]) if isinstance(r["embedding"], (str, bytes, bytearray))
               else r["embedding"]) for r in rows]
    emb = OllamaEmbedder()
    qs = [p[0] for p in PROBES]
    qvecs = await emb.encode(qs)
    await emb.close()

    print(f"{'query':<24} {'best match':<38} {'cos':>6}  {'2nd':>6}  verdict")
    print("-" * 92)
    true_hi, false_hi = [], []
    for (q, want), qv in zip(PROBES, qvecs):
        scored = sorted(((cosine(qv, v), k) for k, v in labels), reverse=True)
        top, second = scored[0], scored[1]
        good = (top[1] == want)
        if want and good:
            true_hi.append(top[0])
            verdict = "correct"
        else:
            # A wrong best-match counts as noise, not as a match: it must stay
            # BELOW the cutoff, exactly like a query with no answer at all.
            false_hi.append(top[0])
            verdict = ("must stay below cutoff" if not want
                       else f"wrong (wanted {want[:22]}) - must stay below")
        print(f"{q:<24} {top[1][:36]:<38} {top[0]:.3f}  {second[0]:.3f}  {verdict}")

    await db.close()

    lo, hi = max(false_hi), min(true_hi)
    print()
    print(f"  highest score that should NOT match : {lo:.3f}")
    print(f"  lowest score that SHOULD match      : {hi:.3f}")
    print(f"  separating band                     : ({lo:.3f}, {hi:.3f}]")
    print(f"  MIN_COSINE                          : {MIN_COSINE}")

    problems = []
    if lo >= hi:
        problems.append(
            f"no threshold separates them: noise reaches {lo:.3f} but a real "
            f"match scores only {hi:.3f}")
    else:
        if not (lo < MIN_COSINE <= hi):
            problems.append(
                f"MIN_COSINE={MIN_COSINE} is outside ({lo:.3f}, {hi:.3f}]")
        # Margin, not just correctness: a threshold sitting on the edge works
        # today and breaks on the next phrasing.
        margin = min(MIN_COSINE - lo, hi - MIN_COSINE)
        if margin < 0.02:
            problems.append(
                f"MIN_COSINE={MIN_COSINE} clears the nearest edge by only "
                f"{margin:.3f}; put it nearer the middle of the band")

    print()
    if problems:
        for p in problems:
            print(f"  [FAIL] {p}")
        print(f"\n0 passed, {len(problems)} failed")
        return 1
    print(f"  [ok  ] every probe classified correctly, "
          f"MIN_COSINE clears both edges by >= 0.02")
    print(f"\n{len(PROBES)} passed, 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
