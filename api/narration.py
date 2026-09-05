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

    Runs at startup. It exists because counterparty is a DERIVED column: the
    parsing rules live in this module where they are tested, not in SQL where
    they are not, so no migration can fill it. Without this, a freshly created
    database answers "who did we pay?" with nothing at all and looks broken
    rather than unpopulated.

    Idempotent and incremental - it only touches rows where counterparty IS
    NULL, so a normal boot does no work and the real export gets parsed on the
    first boot after it lands. Rows whose narration yields no usable name are
    left NULL on purpose; the caller falls back to searching the raw
    description rather than filtering on a wrong guess.
    """
    rows = await db.fetch(
        """SELECT transaction_id, description FROM `transaction`
           WHERE counterparty IS NULL AND description IS NOT NULL
           LIMIT %(lim)s""",
        {"lim": batch},
    )
    filled = 0
    for row in rows:
        name = extract_counterparty(row["description"])
        if not name:
            continue
        await db.execute(
            """UPDATE `transaction` SET counterparty = %(c)s
               WHERE transaction_id = %(id)s""",
            {"c": name, "id": row["transaction_id"]},
        )
        filled += 1
    return filled
