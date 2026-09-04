#!/usr/bin/env python3
"""
Populate semantic_index - the entity-resolution layer.

    ./.venv/bin/python -m scripts.06_build_semantic_index          # build
    ./.venv/bin/python -m scripts.06_build_semantic_index --check  # inspect only

Embeds ~9,171 DISTINCT LABELS, not 1M rows: every vendor name, account, object,
department, fund, fund type, category and program. That is the whole point of a
separate table - the fact tables are never touched, so tomorrow's dataset swap
means re-running this one command.

`entity_key` must be the value the SQL compiler actually filters on, or a
resolved match cannot be turned into a WHERE clause. The SOURCES table below is
paired with api/compile.py:FILTER_SQL deliberately - keep them in step.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass

from api.config import settings
from api.db import db
from api.embed.base import get_embedder


@dataclass(frozen=True)
class Source:
    entity_type: str
    sql: str
    note: str


# entity_key -> what compile.py puts in the WHERE clause for that dimension.
SOURCES: list[Source] = [
    Source("vendor", """
        SELECT vendor_id AS entity_key, vendor_name AS label
        FROM vendors WHERE vendor_name IS NOT NULL
    """, "filters.vendor_ids"),

    Source("category", """
        SELECT DISTINCT category_name AS entity_key, category_name AS label
        FROM chart_of_accounts WHERE category_name IS NOT NULL
    """, "filters.categories -> t.category_name"),

    Source("account", """
        SELECT account_code AS entity_key,
               account_name || ' (' || COALESCE(category_name,'') || ')' AS label
        FROM chart_of_accounts WHERE account_name IS NOT NULL
    """, "group_by account"),

    Source("object", """
        SELECT DISTINCT object_name AS entity_key, object_name AS label
        FROM chart_of_accounts WHERE object_name IS NOT NULL
    """, "group_by object"),

    Source("department", """
        SELECT DISTINCT department_name AS entity_key,
               department_name || ' — ' || COALESCE(org_group_name,'') AS label
        FROM departments WHERE department_name IS NOT NULL
    """, "filters.departments -> t.department_name"),

    Source("fund", """
        SELECT DISTINCT fund_name AS entity_key,
               fund_name || ' — ' || COALESCE(fund_type_name,'') AS label
        FROM funds WHERE fund_name IS NOT NULL
    """, "filters.funds -> t.fund_name"),

    Source("fund_type", """
        SELECT DISTINCT fund_type_name AS entity_key, fund_type_name AS label
        FROM funds WHERE fund_type_name IS NOT NULL
    """, "group_by fund_type"),

    Source("program", """
        SELECT DISTINCT program_name AS entity_key, program_name AS label
        FROM transactions WHERE program_name IS NOT NULL AND program_name <> ''
    """, "filters.programs -> t.program_name"),
]


async def show_state() -> None:
    rows = await db.fetch("""
        SELECT entity_type, COUNT(*) AS n, COUNT(embedding) AS vectors
        FROM semantic_index GROUP BY 1 ORDER BY 2 DESC
    """)
    total = sum(r["n"] for r in rows)
    if not total:
        print("  semantic_index is EMPTY")
        return
    for r in rows:
        print(f"  {r['entity_type']:<12} {r['n']:>7,} labels  {r['vectors']:>7,} vectors")
    print(f"  {'TOTAL':<12} {total:>7,}")


async def build() -> int:
    embedder = get_embedder()
    print(f"embedder: {embedder.name}  ({embedder.dim} dimensions)")

    col_dim = await db.scalar("""
        SELECT atttypmod FROM pg_attribute
        WHERE attrelid = 'semantic_index'::regclass AND attname = 'embedding'
    """)
    if col_dim and col_dim != embedder.dim:
        sys.exit(
            f"\nDIMENSION MISMATCH: semantic_index.embedding is vector({col_dim}) "
            f"but {embedder.name} produces {embedder.dim}.\n"
            f"Fix api/sql/002_semantic_index.sql and drop the table, or change "
            f"EMBED_PROVIDER back."
        )

    grand_total = 0
    t_start = time.perf_counter()

    for source in SOURCES:
        rows = await db.fetch(source.sql)
        rows = [r for r in rows if (r["label"] or "").strip()]
        if not rows:
            print(f"  {source.entity_type:<12} no labels, skipping")
            continue

        labels = [r["label"].strip() for r in rows]
        t0 = time.perf_counter()
        vectors = await embedder.encode(labels)
        elapsed = time.perf_counter() - t0

        async with db.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO semantic_index (entity_type, entity_key, label, embedding)
                    VALUES (%(t)s, %(k)s, %(l)s, %(v)s::vector)
                    ON CONFLICT (entity_type, entity_key)
                    DO UPDATE SET label = EXCLUDED.label,
                                  embedding = EXCLUDED.embedding
                    """,
                    [
                        {"t": source.entity_type, "k": r["entity_key"],
                         "l": r["label"].strip(), "v": str(v)}
                        for r, v in zip(rows, vectors)
                    ],
                )

        grand_total += len(rows)
        print(f"  {source.entity_type:<12} {len(rows):>7,} labels  "
              f"{elapsed:>6.1f}s   -> {source.note}")

    await db.execute("ANALYZE semantic_index")

    print(f"\n{grand_total:,} labels embedded in {time.perf_counter() - t_start:.1f}s")
    return grand_total


async def smoke_test() -> None:
    """The pairs that justify this layer existing. pg_trgm scores ~0 on all of
    them - no shared trigrams between the question's words and the schema's."""
    embedder = get_embedder()
    probes = [
        ("medical supplies", "category"),
        ("the transit agency", "department"),
        ("IT and computers", "object"),
        ("money for building things", "category"),
    ]
    print("\nsemantic matches a trigram index cannot find:")
    for text, entity_type in probes:
        vec = (await embedder.encode([text]))[0]
        hits = await db.fetch(
            """
            SELECT label, 1 - (embedding <=> %(v)s::vector) AS cosine,
                   similarity(label, %(q)s) AS trigram
            FROM semantic_index
            WHERE entity_type = %(t)s AND embedding IS NOT NULL
            ORDER BY embedding <=> %(v)s::vector LIMIT 2
            """,
            {"v": str(vec), "t": entity_type, "q": text},
        )
        print(f'\n  "{text}"  ({entity_type})')
        for h in hits:
            print(f"      {h['label'][:52]:<54} cos {h['cosine']:.3f}  "
                  f"trgm {h['trigram']:.3f}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report state, build nothing")
    args = ap.parse_args()

    await db.connect()
    if args.check:
        await show_state()
        await db.close()
        return 0

    print("BEFORE"); await show_state(); print()
    await build()
    print("\nAFTER"); await show_state()
    await smoke_test()

    print(f"\nSet ENABLE_SEMANTIC=true in .env to switch the read path on "
          f"(currently {settings.enable_semantic}).")
    # Closed once, here - get_embedder() caches, so an early close inside a
    # helper leaves every later caller holding a shut client.
    await get_embedder().close()
    await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
