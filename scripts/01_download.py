#!/usr/bin/env python3
"""
Download SF Vendor Payments (Vouchers) from the DataSF Socrata API.

Source : https://data.sf.gov/d/n9pm-xkyq  (City & County of San Francisco, public domain)
Window : 24 full months of voucher-level payment data.
Output : data/raw/sf_vendor_payments_YYYY-MM.csv.gz  (one file per month)

Stdlib only - no pip install required.
"""

from __future__ import annotations

import gzip
import io
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

RESOURCE = "https://data.sf.gov/resource/n9pm-xkyq.csv"
START_MONTH = (2024, 9)
END_MONTH = (2026, 8)
PAGE = 25_000
MAX_RETRIES = 5
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def months(start: tuple[int, int], end: tuple[int, int]):
    y, m = start
    while (y, m) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def month_bounds(y: int, m: int) -> tuple[str, str]:
    """Half-open [first day of month, first day of next month)."""
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return date(y, m, 1).isoformat(), nxt.isoformat()


def fetch(url: str) -> str:
    """GET with exponential backoff. Socrata occasionally 202s or times out."""
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tbx-hackathon-dataset/1.0"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            wait = 2**attempt
            print(f"    retry {attempt + 1}/{MAX_RETRIES} in {wait}s ({exc})", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"failed after {MAX_RETRIES} attempts: {url}") from last


def download_month(y: int, m: int) -> int:
    out = RAW_DIR / f"sf_vendor_payments_{y}-{m:02d}.csv.gz"
    if out.exists():
        print(f"  {y}-{m:02d}  already present, skipping", flush=True)
        with gzip.open(out, "rt", encoding="utf-8") as fh:
            return max(sum(1 for _ in fh) - 1, 0)

    lo, hi = month_bounds(y, m)
    where = f"data_as_of >= '{lo}' AND data_as_of < '{hi}'"

    header: str | None = None
    body: list[str] = []
    offset = 0

    while True:
        qs = urllib.parse.urlencode(
            {"$where": where, "$order": "voucher", "$limit": PAGE, "$offset": offset}
        )
        text = fetch(f"{RESOURCE}?{qs}")
        lines = text.splitlines(keepends=True)
        if not lines:
            break
        if header is None:
            header = lines[0]
        rows = lines[1:]
        if not rows:
            break
        body.extend(rows)
        got = len(rows)
        print(f"  {y}-{m:02d}  +{got:>6} rows (offset {offset})", flush=True)
        if got < PAGE:
            break
        offset += PAGE

    if header is None:
        print(f"  {y}-{m:02d}  no data", flush=True)
        return 0

    buf = io.StringIO()
    buf.write(header)
    buf.writelines(body)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())

    size_mb = out.stat().st_size / 1e6
    print(f"  {y}-{m:02d}  -> {out.name}  {len(body):,} rows  {size_mb:.1f} MB", flush=True)
    return len(body)


def main() -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading SF vendor payments -> {RAW_DIR}", flush=True)
    total = 0
    for y, m in months(START_MONTH, END_MONTH):
        total += download_month(y, m)
    print(f"\nDone. {total:,} rows across {len(list(months(START_MONTH, END_MONTH)))} months.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
