"""
Counterparty extraction from bank narration.

This schema has no vendor table. Every merchant, payer and payee name lives
inside the free-text `description`, mixed in with reference numbers, IFSC codes,
account numbers and channel prefixes:

    FT -  95842568 -  50200013729069 - SELECTION ELECTRONICS   DAHISAR EAST
    UPI-NAVYUG SELECTION-XXXXXX8672-AUBL0002125-103293775381-260514201735136
    IMPS/P2A/600228462725/UTIB/918020101986700/00/INET/9211/SELECTIONMALIGAI/…

Extracting the name out of that turns "who did we pay?" into an answerable
question, and gives the semantic index something worth embedding.

WHY NOT JUST EMBED THE WHOLE DESCRIPTION
----------------------------------------
Because the machine parts dominate. Two unrelated NEFT transfers share the shape
`NEFT/<digits>/<BANK>/<name>`, so an embedding of the raw string clusters by
PAYMENT RAIL rather than by counterparty. That is worse than having no index,
because it looks like it is working.

The rules below strip, in order: IFSC codes (4 letters + '0' + 6 chars), the
channel prefix, and known noise tokens (long digit runs, masked accounts, INET
and P2A markers). Whatever alphabetic run survives longest is the counterparty.

This is heuristic and will not be perfect on formats it has not seen. It is
deliberately conservative: when nothing survives, it returns None rather than
guessing, and the caller falls back to searching the raw description.
"""

from __future__ import annotations

import logging
import re

# HDFC0001241, AUBL0002125 - Indian bank branch codes.
_IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")

# The payment rail, always at the start.
_CHANNEL = re.compile(
    r"^\s*(FT|UPI|NEFT|IMPS|RTGS|ACH|ECS|POS|ATM|CHQ|R)\b[\s/\-]*", re.I)

# Machine tokens that are never part of a name.
_NOISE = re.compile(
    r"\b("
    r"P2A|P2P|INET|MOB|OW|IW|INWD\d*|OUTW\d*"      # channel sub-codes
    r"|DPF\d+|REF\d+|TXN\d+"                        # reference markers
    r"|X{3,}\d*"                                    # masked account numbers
    r"|\d{4,}"                                      # long digit runs
    r")\b",
    re.I,
)

_SEPARATORS = re.compile(r"[/|\-]+")
_KEEP = re.compile(r"[^A-Za-z& ]")
_SPACES = re.compile(r"\s+")

# A surviving fragment must have a real word in it, not two stray letters.
_HAS_WORD = re.compile(r"[A-Za-z]{3,}")

MIN_LENGTH = 3

log = logging.getLogger("tbx.narration")


def extract_counterparty(description: str | None) -> str | None:
    """Best-effort counterparty name, uppercased. None when nothing survives.

    Returning None matters: it tells the caller to fall back to a raw
    description search rather than filtering on a wrong guess, which would
    silently return the wrong rows.
    """
    if not description:
        return None

    stripped = _NOISE.sub(" ", _CHANNEL.sub(" ", _IFSC.sub(" ", description)))

    candidates = [
        part.strip() for part in _SEPARATORS.split(stripped)
        if _HAS_WORD.search(part)
    ]
    if not candidates:
        return None

    # The counterparty is the fragment carrying the most actual letters -
    # reference fragments lose because they are mostly digits.
    best = max(candidates, key=lambda p: len(_KEEP.sub("", p)))
    cleaned = _SPACES.sub(" ", _KEEP.sub(" ", best)).strip().upper()

    return cleaned if len(cleaned) >= MIN_LENGTH else None


def counterparty_or_none(description: str | None) -> str | None:
    """Alias kept explicit at call sites, so the None case is never a surprise."""
    return extract_counterparty(description)


async def ensure_counterparty(db, batch: int = 5000) -> int:
    """Populate `transaction.counterparty` for rows that do not have it yet.

    Runs at startup, BEFORE the server accepts traffic. counterparty is a
    DERIVED column - the parsing rules live in this module where they are
    tested, not in SQL where they are not - so no migration can fill it, and a
    freshly created database would otherwise answer "who did we pay?" with
    nothing and look broken rather than unpopulated.

    Blocking boot is the deliberate choice: answering on a half-parsed table
    would be quietly wrong, and quietly wrong is the failure this whole codebase
    is built to avoid. The cost is kept low by the two decisions below.

    KEYSET PAGINATION, NOT `LIMIT n`.
    The first version read one LIMIT 5000 batch per boot, so a 500k-row export
    would have needed a hundred restarts to finish. Looping on the same
    predicate instead would never terminate: rows whose narration yields no
    usable name stay NULL by design, so they would be re-read forever. Paging on
    transaction_id walks past them exactly once.

    ONE UPDATE PER NAME, NOT PER ROW.
    Distinct counterparties are far fewer than transactions - 9 across the
    sample, and on a real export thousands against hundreds of thousands of
    rows - so grouping collapses the write count by orders of magnitude.
    """
    filled = 0
    scanned = 0
    after = ""

    while True:
        rows = await db.fetch(
            """SELECT transaction_id, description FROM `transaction`
               WHERE counterparty IS NULL AND description IS NOT NULL
                 AND transaction_id > %(after)s
               ORDER BY transaction_id
               LIMIT %(lim)s""",
            {"after": after, "lim": batch},
        )
        if not rows:
            break

        after = rows[-1]["transaction_id"]
        scanned += len(rows)

        by_name: dict[str, list[str]] = {}
        for row in rows:
            name = extract_counterparty(row["description"])
            if name:
                by_name.setdefault(name, []).append(row["transaction_id"])

        for name, ids in by_name.items():
            # Chunked so one very common counterparty cannot build a statement
            # larger than max_allowed_packet.
            for i in range(0, len(ids), 1000):
                chunk = ids[i:i + 1000]
                keys = {f"id_{n}": v for n, v in enumerate(chunk)}
                placeholders = ", ".join(f"%({k})s" for k in keys)
                await db.execute(
                    f"""UPDATE `transaction` SET counterparty = %(c)s
                        WHERE transaction_id IN ({placeholders})""",
                    {"c": name, **keys},
                )
            filled += len(ids)

        if scanned % (batch * 10) == 0:
            log.info("counterparty backfill: %d scanned, %d named", scanned, filled)

    return filled
