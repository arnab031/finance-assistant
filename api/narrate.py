"""
LLM call #2 - explain an already-computed result - and the numeric provenance
check that makes it safe.

The check is the last guardrail and the most important one. Everything upstream
constrains what the model may *ask for*; this constrains what it may *say*.
Every number in the narration must already exist in the result rows. If one does
not, the model invented it, and an invented figure in a finance answer is the
liability the brief opens by describing.

Ordering note: narration is generated fully, verified, and only then streamed to
the client as tokens. Streaming straight from the model would put an unverified
figure on screen before we could check it, and no retraction undoes that. The
breakdown table has already rendered by this point, so the user is looking at
real numbers during the extra second this costs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Sequence

from api.config import settings
from api.crypto import PlaceholderMap
from api.llm.base import LLM
from api.money import fmt_inr
from api.schema import QuerySpec

log = logging.getLogger("tbx.narrate")

SYSTEM = """You explain a financial result that has ALREADY been computed by a database.

HARD RULES
- You MUST NOT do arithmetic. No sums, differences, percentages, or averages
  that are not already present in the rows.
- You MUST NOT state any number that does not appear in the rows given to you.
  If you want to mention a figure and it is not there, leave it out.
- Do not speculate about causes. You are reporting, not explaining why.

STYLE
- Two or three sentences. Lead with the direct answer.
- Write FLOWING PROSE. Never use a numbered or bulleted list. A full breakdown
  table is already on screen beside your answer, so re-listing rows duplicates it.
  Name at most the single largest item, then stop.
- Money is RUPEES. Write it as ₹1,23,456.78 - the ₹ symbol and Indian digit
  grouping. Never write $ or "dollars". Round only by dropping decimals, never
  by inventing digits.
- If an assumption is listed, state it plainly in your own words.
- If there are no rows, say the query returned nothing - do not guess a reason.
- Account numbers appear as long opaque strings. Copy one EXACTLY if you mention
  it, character for character. Never invent one and never write digits in its
  place - it is a reference, not a number."""

# A number, optionally currency-prefixed, comma-grouped, decimal, percentage.
# Both symbols stay in the class: the ledger is in ₹, but a model that slips
# and writes $ must still have its digits verified rather than skipped.
_NUM_RE = re.compile(r"-?[\$\u20b9]?\s?\d[\d,]*(?:\.\d+)?%?")
_SCALE_RE = re.compile(
    r"\s*(billion|bn|million|mn|thousand|k)\b", re.I)
_SCALES = {"billion": Decimal(10) ** 9, "bn": Decimal(10) ** 9,
           "million": Decimal(10) ** 6, "mn": Decimal(10) ** 6,
           "thousand": Decimal(1000), "k": Decimal(1000)}


@dataclass
class Verification:
    ok: bool
    numbers_checked: int
    unverified: list[str]


def _canon(token: str) -> str:
    """Strip presentation - symbol, grouping, percent - down to the digits.

    Indian grouping falls out for free: "1,69,299.00" and "169,299.00" both
    canonicalise to "169299.00", so the checker is indifferent to which
    convention the model used and only ever compares values."""
    for ch in ("₹", "$", ",", "%", " "):
        token = token.replace(ch, "")
    return token.strip()


def _to_decimal(token: str) -> Decimal | None:
    try:
        return Decimal(_canon(token))
    except (InvalidOperation, ValueError):
        return None


def _renderings(v: Decimal) -> set[str]:
    """Every way a model might legitimately write this value.

    Includes TRUNCATED forms, not just rounded ones: the narration prompt tells
    the model to "round only by dropping decimals", and it does exactly that -
    131,376,098.72 came back as "131,376,098". Only accepting the rounded
    "131,376,099" flagged a correct figure as a hallucination.
    """
    out: set[str] = set()
    for places in (0, 1, 2):
        out.add(_canon(f"{v:.{places}f}"))
        out.add(_canon(str(v.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN))))
    out.add(_canon(str(v)))
    out.add(_canon(str(abs(v))))
    for scale in (Decimal(1000), Decimal(10) ** 6, Decimal(10) ** 9):
        scaled = v / scale
        for places in (0, 1, 2, 3):
            out.add(_canon(f"{scaled:.{places}f}"))
            out.add(_canon(str(scaled.quantize(Decimal(1).scaleb(-places),
                                               rounding=ROUND_DOWN))))
    return out


def _half_ulp(token: str, scale: Decimal) -> Decimal:
    """Half the last stated significant place, in the value's own units.

    "1.07 billion" states 2 decimals, so its precision is 0.005 billion = $5M -
    wide enough to accept 1,068,521,181.57 and narrow enough to reject
    1,500,000,000. "$1,068,521,182.57" also states 2 decimals but at scale 1, so
    its precision is half a cent, and a $1 discrepancy is caught.

    An earlier version added a flat 0.5% relative floor here. That was wrong: on
    a $1.07B figure it tolerated a $5.3M error, so a subtly corrupted digit
    passed verification. Legitimate rounding is already handled exactly by
    _renderings(); this path exists only for scale-word abbreviations.
    """
    frac = _canon(token).split(".")
    places = len(frac[1]) if len(frac) > 1 else 0
    if places == 0:
        # No decimals stated, so the writer may have truncated OR rounded:
        # "131,376,098" can mean anything in [131376098, 131376099).
        return Decimal(1) * scale
    return (Decimal(10) ** -places) / 2 * scale


def _context_numbers(text: str) -> tuple[set[str], list[Decimal]]:
    """Numbers the USER or the SYSTEM put in front of the model.

    A figure the user typed ("last 12 months") or that we handed over in an
    assumption note is not a model invention, and flagging it is a false
    positive. Measured: without this, narration failed verification on
    $18,427,695,791.11 and 529,979 - both quoted verbatim from the disclosure
    note the prompt explicitly asks the model to restate - and on "12" from the
    user's own phrase. Two wasted retries and a needless template fallback.
    """
    strings: set[str] = set()
    values: list[Decimal] = []
    for tok in _NUM_RE.finditer(text or ""):
        d = _to_decimal(tok.group(0))
        if d is None:
            continue
        values.append(d)
        strings |= _renderings(d)
    return strings, values


def verify_numbers(
    text: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    extra_allowed: Sequence[Decimal | int] = (),
    context: str = "",
) -> Verification:
    """Every number in `text` must be traceable to `rows`.

    Structural integers (row positions, counts, "the top 5") are allowed up to
    the number of rows returned - they are references to the result, not
    financial claims. Anything larger has to match real data.

    `context` is the question plus any period labels: years appearing there are
    the user's own words being echoed back, not figures the model produced.
    """
    allowed_values: list[Decimal] = []
    allowed_strings: set[str] = set()

    for row in rows:
        for cell in row:
            if isinstance(cell, bool) or cell is None:
                continue
            if isinstance(cell, (int, float, Decimal)):
                d = Decimal(str(cell))
                allowed_values.append(d)
                allowed_strings |= _renderings(d)
            elif isinstance(cell, str):
                # dates like 2026-08-01 legitimise their components
                for part in re.findall(r"\d+", cell):
                    allowed_strings.add(part.lstrip("0") or "0")
                    allowed_strings.add(part)

    for extra in extra_allowed:
        d = Decimal(str(extra))
        allowed_values.append(d)
        allowed_strings |= _renderings(d)

    ctx_strings, ctx_values = _context_numbers(context)
    allowed_strings |= ctx_strings
    allowed_values.extend(ctx_values)

    structural_ceiling = max(10, len(rows))
    unverified: list[str] = []
    checked = 0

    for match in _NUM_RE.finditer(text):
        token = match.group(0)
        value = _to_decimal(token)
        if value is None:
            continue
        checked += 1

        canon = _canon(token)
        if canon in allowed_strings:
            continue

        # small structural integers: "the top 5", "3 vendors"
        if value == value.to_integral_value() and abs(value) <= structural_ceiling:
            continue

        # scale words: "1.07 billion" -> candidate value, and the precision the
        # abbreviation actually claims
        tail = text[match.end(): match.end() + 12]
        scale_match = _SCALE_RE.match(tail)
        candidates: list[tuple[Decimal, Decimal]] = [(value, Decimal(1))]
        if scale_match:
            factor = _SCALES[scale_match.group(1).lower()]
            candidates.append((value * factor, factor))

        if any(
            abs(cand - av) <= _half_ulp(token, scale)
            for cand, scale in candidates
            for av in allowed_values
        ):
            continue

        unverified.append(token.strip())

    return Verification(ok=not unverified, numbers_checked=checked, unverified=unverified)


def _trim_incomplete(text: str) -> str:
    """Drop a trailing fragment left by the output-token cap.

    A narration cut mid-clause ("... MELLON with ") reads as a bug and, worse,
    as a number about to be stated. Better to end one sentence early.
    """
    text = text.strip()
    if not text or text[-1] in ".!?":
        return text
    # Sentence-ending punctuation only: a decimal point is not a sentence end.
    # Matching bare "." truncated "$754,784,356.18" to "$754,784,356." - which
    # reads as a number cut in half, the exact impression to avoid.
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", text)]
    trimmed = text[: ends[-1]].strip() if ends and ends[-1] > 40 else text
    return _drop_orphan_marker(trimmed)


_ORPHAN_RE = re.compile(r"(?:\n|^)\s*(?:\d+[.)]|[-*\u2022])\s*$")


def _drop_orphan_marker(text: str) -> str:
    """Remove a trailing list marker with nothing after it.

    When the model enumerates and the output cap cuts it mid-item, the trimmer
    backs up to the last sentence end - and "3." looks exactly like one. The
    result was an answer ending in a bare "3.", which reads as a broken UI.
    """
    while True:
        stripped = _ORPHAN_RE.sub("", text).rstrip()
        if stripped == text:
            return text
        text = stripped


def _check_placeholders(
    text: str, verdict: Verification, pmap: PlaceholderMap | None
) -> Verification:
    """An account placeholder the model invented is the same class of failure as
    an invented number: a reference to something that was never in the data.

    Without this, an unrecognised [[ACCT_Q]] would survive substitution and sit
    in the answer as literal text.
    """
    if pmap is None:
        return verdict
    unknown = pmap.unknown_in(text)
    if not unknown:
        return verdict
    return Verification(ok=False,
                        numbers_checked=verdict.numbers_checked,
                        unverified=[*verdict.unverified, *unknown])


def _fmt_cell(v: Any) -> str:
    if isinstance(v, Decimal):
        return f"{v:,.2f}"
    return "" if v is None else str(v)


def _fmt_money(v: Any) -> str:
    """Same value as _fmt_cell, but marked as rupees.

    Only the template fallback uses this. The prompt rows deliberately stay
    bare in _fmt_cell: they are data for the model to read, and a symbol on
    every cell is one more token it could copy into the wrong sentence."""
    return fmt_inr(v) if isinstance(v, Decimal) else _fmt_cell(v)


def build_user_prompt(
    question: str, columns: Sequence[str], rows: Sequence[Sequence[Any]],
    notes: Sequence[str] = (),
) -> str:
    capped = rows[: settings.max_rows_to_llm]
    lines = [f"QUESTION: {question}", "", f"COLUMNS: {', '.join(columns)}", "ROWS:"]
    if not capped:
        lines.append("  (no rows)")
    for row in capped:
        lines.append("  " + " | ".join(_fmt_cell(c) for c in row))
    if len(rows) > len(capped):
        lines.append(f"  ... these are the top {len(capped)} of {len(rows)} rows. "
                     f"Mention the count, not the missing rows.")
    if notes:
        lines += ["", "ASSUMPTIONS TO STATE:"] + [f"  - {n}" for n in notes]
    return "\n".join(lines)


def template_answer(
    question: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> str:
    """Deterministic fallback. Built only from the rows, so it is verifiable by
    construction - used when the model cannot produce a clean narration."""
    if not rows:
        return "That query returned no matching records."
    if "value" in columns:
        idx = columns.index("value")
        first = rows[0][idx]
        if len(rows) == 1:
            body = f"The result is {_fmt_money(first)}."
        else:
            label = _fmt_cell(rows[0][0])
            body = (f"{len(rows)} rows. The largest is {label} at "
                    f"{_fmt_money(first)}.")
    else:
        body = f"The query returned {len(rows)} rows."
    return f"{body} The breakdown below is the source data."


async def narrate(
    question: str,
    spec: QuerySpec,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    llm: LLM,
    notes: Sequence[str] = (),
    pmap: PlaceholderMap | None = None,
) -> tuple[str, Verification, int]:
    """Generate, verify, retry once, else fall back to a template.

    Returns (text, verification, latency_ms).
    """
    import time

    t0 = time.perf_counter()
    # Defence in depth. The views already serve masked forms, the compiler
    # refuses to select sensitive columns, and this asserts on the rows that
    # actually reach the model. A silent mask that stops working looks entirely
    # normal until someone reads the output - so it fails loudly instead.
    user = build_user_prompt(question, columns, rows, notes)
    extra = [len(rows), spec.limit]

    text = ""
    async for chunk in llm.stream_text(SYSTEM, user):
        text += chunk
    text = _trim_incomplete(text)

    context = " ".join([
        question,
        spec.period.label if spec.period else "",
        spec.fiscal_year or "",
        *notes,                     # the prompt asks the model to restate these
    ])
    # Strip issued tokens first: ciphertext is base64 and contains digits, which
    # the numeric check would otherwise read as figures the model invented.
    verdict = verify_numbers(pmap.strip(text) if pmap else text,
                             columns, rows, extra, context)
    verdict = _check_placeholders(text, verdict, pmap)
    if verdict.ok:
        return text, verdict, int((time.perf_counter() - t0) * 1000)

    log.warning("narration had unverified numbers %s - retrying", verdict.unverified)
    strict_user = (
        f"{user}\n\n"
        f"Your previous answer contained these numbers that are NOT in the rows: "
        f"{', '.join(verdict.unverified)}. "
        f"Rewrite it using ONLY numbers copied exactly from the rows above."
    )
    retry = ""
    async for chunk in llm.stream_text(SYSTEM, strict_user):
        retry += chunk
    retry = _trim_incomplete(retry)

    retry_verdict = _check_placeholders(
        retry,
        verify_numbers(pmap.strip(retry) if pmap else retry,
                       columns, rows, extra, context),
        pmap)
    if retry_verdict.ok:
        return retry, retry_verdict, int((time.perf_counter() - t0) * 1000)

    log.warning("narration still unverified %s - using template", retry_verdict.unverified)
    fallback = template_answer(question, columns, rows)
    return (
        fallback,
        verify_numbers(fallback, columns, rows, extra, context),
        int((time.perf_counter() - t0) * 1000),
    )
