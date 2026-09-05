"""
One rule for recorded instants: store UTC, display IST.

MySQL has no timezone-aware type. A DATETIME(6) written by CURRENT_TIMESTAMP(6)
records whatever the server's session clock said, and carries no offset, so the
value alone cannot tell you which zone it meant. Serialised naively it reaches
the browser as "2026-09-05T07:06:45", which JavaScript reads as LOCAL time - so
a run that happened at 12:36 IST rendered as 07:06. Not a formatting nit: the
ops page is where you go to ask "did the canary run since the last deploy?", and
a five-and-a-half-hour error answers that question wrongly.

The fix has two halves and needs both:
  - the pool pins its session to +00:00 (api/db.py), so "naive means UTC" is an
    invariant this app enforces rather than one it inherits from the host; and
  - `as_utc` stamps that offset on the way out, so the wire carries an instant
    instead of a bare wall-clock reading.

BUSINESS DATES ARE NOT INSTANTS. `transaction_date` is a calendar date from the
ledger - it was never a moment in a timezone, and shifting it by an offset would
move a payment to the previous day. So this is applied to named audit columns
only, never blanket-applied to every datetime coming out of the database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30), "IST")

#: Columns written by the server clock. Everything else is business data.
INSTANT_COLUMNS = frozenset({"created_at", "started_at", "finished_at"})


def as_utc(value: Any) -> Any:
    """Mark a naive instant as UTC. Aware values and non-datetimes pass through."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def with_instants(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Row -> the same row with its audit timestamps made unambiguous."""
    if row is None:
        return None
    return {k: as_utc(v) if k in INSTANT_COLUMNS else v for k, v in row.items()}


def rows_with_instants(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [with_instants(r) for r in rows]  # type: ignore[misc]
