"""
Vendor resolution: free text -> concrete vendor_ids.

Stage 1 (now): pg_trgm lexical matching. Handles typos and substrings.
Stage 2 (Phase 8): hybrid with pgvector for semantic matches that share no
trigrams - "medical supplies" vs "Hospital: Clinic/Lab Supplies".

Returns *all* plausible candidates with their spend, because the ambiguity
resolver needs to compare them and must not re-query to do it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from api.config import settings
from api.db import Database

log = logging.getLogger("tbx.entities")

# Words that are never a company name. The extractor is told this too, but
# defence in depth is cheap and the failure mode (filtering to zero rows,
# reporting $0) is exactly the kind of silent wrongness we cannot ship.
STOPWORDS = {
    "vendor", "vendors", "supplier", "suppliers", "payout", "payouts",
    "payment", "payments", "transaction", "transactions", "spend", "spending",
    "top", "all", "any", "total", "everyone", "anybody", "company", "companies",
}


@dataclass
class VendorCandidate:
    vendor_id: str
    vendor_name: str
    total_amount: Decimal
    txn_count: int
    score: float


@dataclass
class VendorMatch:
    query: str
    candidates: list[VendorCandidate]

    @property
    def best(self) -> VendorCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def combined_amount(self) -> Decimal:
        return sum((c.total_amount for c in self.candidates), Decimal(0))

    def dominance(self) -> float:
        """Share of combined spend held by the top match. 1.0 = unambiguous."""
        total = self.combined_amount
        if not self.candidates or total == 0:
            return 1.0
        return float(self.candidates[0].total_amount / total)

    def ids(self, all_candidates: bool = False) -> list[str]:
        if all_candidates:
            return [c.vendor_id for c in self.candidates]
        return [self.candidates[0].vendor_id] if self.candidates else []


def is_meaningful(query: str | None) -> bool:
    """False for generic phrases the extractor should never have put here."""
    if not query:
        return False
    cleaned = query.strip().lower()
    if len(cleaned) < 3 or len(cleaned) > 80:
        return False
    tokens = [t for t in cleaned.replace(",", " ").split() if t]
    return bool(tokens) and not all(t in STOPWORDS for t in tokens)


_LEXICAL_SQL = """
    SELECT vendor_id, vendor_name, total_amount, transaction_count,
           similarity(vendor_name, %(q)s) AS trgm,
           (vendor_name ILIKE %(like)s)   AS contains,
           0.0                            AS cos
    FROM vendors
    WHERE vendor_name %% %(q)s OR vendor_name ILIKE %(like)s
    ORDER BY contains DESC, trgm DESC, total_amount DESC
    LIMIT %(lim)s
"""

# Union of the two retrievers, then BOTH scores computed for every candidate.
# Scoring each candidate on only the signal that found it would rank a strong
# lexical match below a mediocre semantic one purely because the semantic
# retriever never returned it.
_HYBRID_SQL = """
    WITH lex AS (
        SELECT vendor_id FROM vendors
        WHERE vendor_name %% %(q)s OR vendor_name ILIKE %(like)s
        ORDER BY similarity(vendor_name, %(q)s) DESC
        LIMIT %(lim)s
    ),
    sem AS (
        SELECT entity_key AS vendor_id
        FROM semantic_index
        WHERE entity_type = 'vendor' AND embedding IS NOT NULL
        ORDER BY embedding <=> %(vec)s::vector
        LIMIT %(lim)s
    ),
    cand AS (SELECT vendor_id FROM lex UNION SELECT vendor_id FROM sem)
    SELECT v.vendor_id, v.vendor_name, v.total_amount, v.transaction_count,
           COALESCE(similarity(v.vendor_name, %(q)s), 0)      AS trgm,
           (v.vendor_name ILIKE %(like)s)                     AS contains,
           COALESCE(1 - (s.embedding <=> %(vec)s::vector), 0) AS cos
    FROM cand
    JOIN vendors v USING (vendor_id)
    LEFT JOIN semantic_index s
           ON s.entity_type = 'vendor' AND s.entity_key = v.vendor_id
"""

W_TRGM, W_COS = 0.6, 0.4
CONTAINS_FLOOR = 0.75      # a substring hit is a strong, reliable signal
SEMANTIC_ONLY_MIN = 0.60   # a candidate with no lexical overlap must be clearly close


async def resolve_vendor(
    query: str, db: Database, limit: int = 8, min_score: float = 0.25
) -> VendorMatch:
    """Rank vendors against free text. Lexical by default - see the note below.

    MEASURED: enabling the semantic leg for VENDORS is a regression, and the
    flag ships off because of it.

        query "mckeson" (a typo)
          lexical  1 candidate,  dominance 1.00  -> answers directly
          hybrid   8 candidates, dominance 0.63  -> asks a needless question

    Vendor names are proper nouns. Trigrams are the right tool for them, and
    semantically-adjacent-but-different companies are noise: they dilute
    VendorMatch.dominance(), which is what the entity ambiguity detector uses to
    decide whether to interrupt the user. More candidates means more spurious
    clarifications - the exact over-asking failure AMBIGUITY.md warns about.

    The semantic path is kept, tested and behind `enable_semantic` because it
    becomes the right answer on a dataset whose vocabulary is too large to fit
    in the extraction prompt. It is not the right answer on this one.
    """
    if not is_meaningful(query):
        return VendorMatch(query=query, candidates=[])

    params: dict = {"q": query, "like": f"%{query}%", "lim": limit * 4}
    use_semantic = settings.enable_semantic

    if use_semantic:
        try:
            from api.embed.base import get_embedder

            params["vec"] = str((await get_embedder().encode([query]))[0])
            rows = await db.fetch(_HYBRID_SQL, params)
        except Exception as exc:  # noqa: BLE001
            # Never let the optional layer break resolution.
            log.warning("semantic leg failed (%s); falling back to lexical", exc)
            use_semantic = False
            rows = await db.fetch(_LEXICAL_SQL, params)
    else:
        rows = await db.fetch(_LEXICAL_SQL, params)

    candidates: list[VendorCandidate] = []
    for r in rows:
        trgm = float(r["trgm"] or 0.0)
        cos = float(r["cos"] or 0.0)
        contains = bool(r["contains"])

        score = W_TRGM * trgm + W_COS * cos if use_semantic else trgm
        if contains:
            score = max(score, CONTAINS_FLOOR)

        # Semantic-only hits are the noisy ones: no shared characters at all, so
        # require real confidence before letting them into the candidate set.
        if not contains and trgm < 0.10 and cos < SEMANTIC_ONLY_MIN:
            continue
        if score < min_score:
            continue

        candidates.append(VendorCandidate(
            vendor_id=r["vendor_id"],
            vendor_name=r["vendor_name"],
            total_amount=r["total_amount"] or Decimal(0),
            txn_count=r["transaction_count"] or 0,
            score=round(score, 3),
        ))

    candidates.sort(key=lambda c: (-c.score, -c.total_amount))
    match = VendorMatch(query=query, candidates=candidates[:limit])
    log.info("vendor %r -> %d candidates (%s, dominance %.2f)",
             query, len(match.candidates),
             "hybrid" if use_semantic else "lexical", match.dominance())
    return match


async def resolve_semantic(query: str, entity_type: str, db: Database, limit: int = 5):
    """Phase 8. Guarded so calling it early is a no-op rather than an error."""
    if not settings.enable_semantic:
        return []
    from api.embed.base import get_embedder

    vec = (await get_embedder().encode([query]))[0]
    return await db.fetch(
        """
        SELECT entity_key, label, 1 - (embedding <=> %(v)s::vector) AS cosine
        FROM semantic_index
        WHERE entity_type = %(t)s AND embedding IS NOT NULL
        ORDER BY embedding <=> %(v)s::vector
        LIMIT %(lim)s
        """,
        {"v": str(list(vec)), "t": entity_type, "lim": limit},
    )
