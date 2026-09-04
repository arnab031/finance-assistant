"""CLI entry: ./.venv/bin/python -m eval [--only ids] [--notes text]"""
from __future__ import annotations

import argparse, asyncio, sys
from api.config import settings
from api.db import db
from eval.harness import run_eval

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--notes", default="")
    a = ap.parse_args()

    await db.connect()
    model = (settings.ollama_model if settings.llm_provider == "ollama"
             else settings.anthropic_model)
    failures = []
    async for kind, payload in run_eval(
        db, a.api, model, a.only.split(",") if a.only else None, a.notes
    ):
        if kind == "start":
            print(f"run {payload['run_id']}  {payload['total']} questions  "
                  f"model={payload['model']}\n" + "=" * 74)
        elif kind == "result":
            r = payload
            mark = f"{GREEN}pass{OFF}" if r.passed else f"{RED}FAIL{OFF}"
            print(f"  {mark}  {r.question_id}  {r.grade:<9} {r.question[:44]:<46}"
                  f"{r.latency_ms:>6}ms")
            if not r.passed:
                failures.append(r)
                print(f"        {DIM}expected {r.expected[:60]}{OFF}")
                print(f"        {DIM}got      {r.actual[:60]}  {r.detail[:60]}{OFF}")
        else:
            p, t = payload["passed"], payload["total"]
            print("=" * 74)
            print(f"{p}/{t} passed ({p/t:.0%})  in {payload['duration_ms']/1000:.0f}s")
    await db.close()
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
