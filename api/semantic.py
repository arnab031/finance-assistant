"""
The semantic index: build it on boot, search it in Python.

WHY THIS EXISTS
---------------
Counterparty filtering is a LIKE. That finds "RELIANCE" inside
"RELIANCEDIGITAL RETAIL LTD" and it is the right default, but it can only match
characters the user typed. Asked about "the electronics store" it returns
nothing, because no substring of that phrase appears in
"SELECTION ELECTRONICS DAHISAR EAST" in a way LIKE can use.

    cos("electronics store", "SELECTION ELECTRONICS DAHISAR EAST") = high
    LIKE '%electronics store%'                                     = no rows

So the index is a FALLBACK, not a replacement: it runs only when the lexical
filter matched nothing. That ordering is deliberate and was measured on the
stand-in dataset - running semantic retrieval first turned the query "mckeson"
from one confident candidate into eight, and a needless clarifying question.
A fallback cannot cause that, because it never fires when LIKE succeeds.

WHY SEARCH RUNS IN PYTHON
-------------------------
PostgreSQL had pgvector: `embedding <=> %(vec)s::vector` with an HNSW index.
MySQL 8.4 has no vector type at all, so the embedding is a JSON array and there
is nothing in SQL to search it with. Cosine therefore runs here, over the
candidate rows loaded into memory.

That is fine at this scale and would not be at every scale. The vocabulary is
one row per distinct counterparty - 9 today, and on a real bank export the
order of thousands, which is a few MB of floats and a millisecond of numpy-free
arithmetic. It would NOT be fine on a million labels; at that point this needs a
real vector store, and the honest signal is MAX_LABELS below.

WHY IT IS NOT REBUILT EVERY BOOT
--------------------------------
Embedding is the expensive part - one Ollama round trip per batch. The
fingerprint records what the vocabulary looked like when the index was built, so
an unchanged database costs one cheap aggregate query at startup instead of
minutes of embedding calls. When the real export lands the fingerprint changes
and it rebuilds itself, which is the whole point: nobody has to remember to run
a script.
"""

from __future__ import annotations

import json
import logging
import math
import time

from api.config import settings
from api.db import Database
from api.profiles.base import SemanticSource, get_profile

log = logging.getLogger("tbx.semantic")

# Above this, loading every embedding to score in Python stops being reasonable.
# Building still succeeds; searching refuses rather than quietly getting slow,
# because a resolver that takes two seconds is worse than one that declines.
MAX_LABELS = 20_000

# MEASURED, not guessed - see eval/test_semantic.py, which re-derives this on
# every run. Against the real counterparty vocabulary with nomic-embed-text:
#
#     correct matches      0.623 .. 0.864   ("electronics store", "gautam", ...)
#     should-not-match     0.416 .. 0.494   ("zomato", "hospital", "airline tickets")
#
# so anything in (0.494, 0.623] separates them. This sits mid-gap. The first
# value tried was 0.62, which happens to work but clears the lowest TRUE match
# by 0.003 - one differently-phrased question away from silently dropping a
# match it should have found. Margin on both sides is the point.
#
# Returning noise here is worse than returning nothing, because the caller
# rewrites a filter on the strength of it.
MIN_COSINE = 0.55


def _fingerprint_sql(source_sql: str) -> str:
    """Order-independent, bounded summary of a vocabulary.

    GROUP_CONCAT over every key would be exact, but it truncates silently at
    group_concat_max_len - producing a STABLE fingerprint for CHANGED data,
    which is the one failure this must not have.
    """
    return f"""
        SELECT CONCAT_WS(':',
                   COUNT(*),
                   COALESCE(SUM(CRC32(entity_key)), 0),
                   COALESCE(MD5(MIN(entity_key)), ''),
                   COALESCE(MD5(MAX(entity_key)), '')
               ) AS fp
        FROM ({source_sql}) AS src
    """


def cosine(a: list[float], b: list[float]) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


async def _sync_labels(db: Database, source: SemanticSource) -> None:
    """Make semantic_index hold exactly the current vocabulary.

    Rows keep their embedding across a resync when the label is unchanged, so a
    vocabulary that grew by ten names costs ten embeddings rather than all of
    them. A CHANGED label drops its embedding, because the old vector describes
    text that no longer exists.
    """
    await db.execute(
        f"""
        INSERT INTO semantic_index (entity_type, entity_key, label)
        SELECT %(t)s, src.entity_key, src.label FROM ({source.sql}) AS src
        ON DUPLICATE KEY UPDATE
            embedding = IF(semantic_index.label = VALUES(label),
                           semantic_index.embedding, NULL),
            label     = VALUES(label)
        """,
        {"t": source.entity_type},
    )
    await db.execute(
        f"""
        DELETE si FROM semantic_index si
        LEFT JOIN ({source.sql}) AS src ON src.entity_key = si.entity_key
        WHERE si.entity_type = %(t)s AND src.entity_key IS NULL
        """,
        {"t": source.entity_type},
    )


async def _embed_missing(db: Database, entity_type: str) -> int:
    """Embed only the rows that have no vector. Returns how many were written."""
    from api.embed.ollama_embed import OllamaEmbedder

    rows = await db.fetch(
        """SELECT id, label FROM semantic_index
           WHERE entity_type = %(t)s AND embedding IS NULL
           ORDER BY id LIMIT %(lim)s""",
        {"t": entity_type, "lim": MAX_LABELS},
    )
    if not rows:
        return 0

    embedder = OllamaEmbedder()
    try:
        vectors = await embedder.encode([r["label"] for r in rows])
    finally:
        await embedder.close()

    for row, vec in zip(rows, vectors):
        await db.execute(
            "UPDATE semantic_index SET embedding = %(v)s WHERE id = %(id)s",
            {"v": json.dumps(vec), "id": row["id"]},
        )
    return len(rows)


async def ensure_semantic_index(db: Database) -> dict[str, str]:
    """Build or refresh the index for every source the active profile declares.

    Returns {entity_type: outcome} for logging and /api/health. Never raises:
    the index is an optional recall aid, and a dead embedding service must not
    stop the server from answering questions the lexical path handles fine.
    """
    prof = get_profile()
    outcomes: dict[str, str] = {}

    if not prof.semantic_sources:
        return outcomes
    if not settings.enable_semantic:
        # Say so explicitly. An empty table with no explanation is the reason
        # "why is semantic_index empty?" got asked twice.
        return {s.entity_type: "skipped (enable_semantic=false)"
                for s in prof.semantic_sources}

    for source in prof.semantic_sources:
        t0 = time.perf_counter()
        try:
            fp = await db.scalar(_fingerprint_sql(source.sql))
            meta = await db.fetchone(
                "SELECT * FROM semantic_index_meta WHERE entity_type = %(t)s",
                {"t": source.entity_type},
            )
            missing = await db.scalar(
                """SELECT COUNT(*) FROM semantic_index
                   WHERE entity_type = %(t)s AND embedding IS NULL""",
                {"t": source.entity_type},
            )

            fresh = (
                meta is not None
                and meta["fingerprint"] == fp
                and meta["embed_model"] == settings.embed_model
                and meta["embed_dim"] == settings.embed_dim
                and not missing
            )
            if fresh:
                outcomes[source.entity_type] = f"up to date ({meta['n_labels']} labels)"
                continue

            # A model or dimension change invalidates every vector: they are not
            # comparable across models, and a mixed table returns confident
            # nonsense rather than an error.
            if meta and (meta["embed_model"] != settings.embed_model
                         or meta["embed_dim"] != settings.embed_dim):
                log.info("embedder changed (%s/%s -> %s/%s); clearing vectors",
                         meta["embed_model"], meta["embed_dim"],
                         settings.embed_model, settings.embed_dim)
                await db.execute(
                    "UPDATE semantic_index SET embedding = NULL WHERE entity_type = %(t)s",
                    {"t": source.entity_type},
                )

            await _sync_labels(db, source)
            n_embedded = await _embed_missing(db, source.entity_type)
            n_labels = await db.scalar(
                "SELECT COUNT(*) FROM semantic_index WHERE entity_type = %(t)s",
                {"t": source.entity_type},
            )
            build_ms = int((time.perf_counter() - t0) * 1000)

            await db.execute(
                """INSERT INTO semantic_index_meta
                       (entity_type, fingerprint, embed_model, embed_dim,
                        n_labels, build_ms)
                   VALUES (%(t)s, %(fp)s, %(m)s, %(d)s, %(n)s, %(ms)s)
                   ON DUPLICATE KEY UPDATE
                       fingerprint = VALUES(fingerprint),
                       embed_model = VALUES(embed_model),
                       embed_dim   = VALUES(embed_dim),
                       n_labels    = VALUES(n_labels),
                       build_ms    = VALUES(build_ms),
                       built_at    = CURRENT_TIMESTAMP(6)""",
                {"t": source.entity_type, "fp": fp, "m": settings.embed_model,
                 "d": settings.embed_dim, "n": n_labels, "ms": build_ms},
            )
            outcomes[source.entity_type] = (
                f"built {n_embedded} of {n_labels} labels in {build_ms}ms")
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic index for %r failed: %s", source.entity_type, exc)
            outcomes[source.entity_type] = f"failed: {type(exc).__name__}"

    return outcomes


async def search(
    db: Database, entity_type: str, query: str, limit: int = 5
) -> list[tuple[str, float]]:
    """Nearest labels to `query`, as (entity_key, cosine), best first.

    Empty when the index is off, unbuilt, oversized, or nothing clears
    MIN_COSINE. Every one of those is a reason to fall back to the lexical
    answer, so they are deliberately indistinguishable to the caller.
    """
    if not settings.enable_semantic or not query:
        return []

    rows = await db.fetch(
        """SELECT entity_key, embedding FROM semantic_index
           WHERE entity_type = %(t)s AND embedding IS NOT NULL
           LIMIT %(lim)s""",
        {"t": entity_type, "lim": MAX_LABELS + 1},
    )
    if not rows or len(rows) > MAX_LABELS:
        if rows:
            log.warning("semantic index for %r exceeds %d labels; refusing to "
                        "score in Python", entity_type, MAX_LABELS)
        return []

    from api.embed.ollama_embed import OllamaEmbedder

    embedder = OllamaEmbedder()
    try:
        qvec = (await embedder.encode([query]))[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("query embedding failed: %s", exc)
        return []
    finally:
        await embedder.close()

    scored = []
    for row in rows:
        vec = row["embedding"]
        if isinstance(vec, (str, bytes, bytearray)):
            vec = json.loads(vec)
        score = cosine(qvec, vec)
        if score >= MIN_COSINE:
            scored.append((row["entity_key"], score))

    scored.sort(key=lambda p: p[1], reverse=True)
    return scored[:limit]
