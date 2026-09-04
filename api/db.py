"""
Async Postgres access via psycopg3.

psycopg3 maps NUMERIC -> decimal.Decimal natively, which is the whole reason
the money columns are NUMERIC(18,2). Nothing in this file may convert a money
value to float; that would reintroduce exactly the drift the schema avoids.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from api.config import settings

SQL_DIR = Path(__file__).parent / "sql"


class Database:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or settings.database_url
        self._pool: AsyncConnectionPool | None = None

    # ---- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        self._pool = AsyncConnectionPool(
            self._dsn,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        await self._pool.open(wait=True, timeout=15)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("Database.connect() has not been called")
        return self._pool

    # ---- queries ---------------------------------------------------------

    async def fetch(self, sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params or {})
            return await cur.fetchall()

    async def fetchone(self, sql: str, params: dict | None = None) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params or {})
            return await cur.fetchone()

    async def column(self, sql: str, params: dict | None = None) -> list[Any]:
        """First column of every row - for vocabulary loading."""
        rows = await self.fetch(sql, params)
        return [next(iter(r.values())) for r in rows]

    async def scalar(self, sql: str, params: dict | None = None) -> Any:
        row = await self.fetchone(sql, params)
        return next(iter(row.values())) if row else None

    async def execute(self, sql: str, params: dict | None = None) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql, params or {})

    async def fetch_timed(
        self, sql: str, params: dict | None = None
    ) -> tuple[list[str], list[list[Any]], int]:
        """Returns (columns, rows-as-lists, elapsed_ms) - shaped for RowsEvent."""
        t0 = time.perf_counter()
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params or {})
            records = await cur.fetchall()
            columns = [d.name for d in (cur.description or [])]
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return columns, [[r[c] for c in columns] for r in records], elapsed_ms

    # ---- introspection ---------------------------------------------------

    async def has_table(self, name: str) -> bool:
        return bool(await self.scalar(
            "SELECT to_regclass(%(n)s) IS NOT NULL", {"n": f"public.{name}"}
        ))

    async def has_column(self, table: str, column: str) -> bool:
        return bool(await self.scalar(
            """SELECT EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema='public' AND table_name=%(t)s AND column_name=%(c)s)""",
            {"t": table, "c": column},
        ))

    async def numeric_columns(self, table: str) -> list[str]:
        return await self.column(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='public' AND table_name=%(t)s
                 AND data_type IN ('numeric','double precision','real','integer','bigint')
               ORDER BY ordinal_position""",
            {"t": table},
        )

    # ---- migrations ------------------------------------------------------

    async def migrate(self) -> list[str]:
        """Apply api/sql/*.sql in filename order. All statements are idempotent."""
        applied: list[str] = []
        for path in sorted(SQL_DIR.glob("*.sql")):
            async with self.pool.connection() as conn:
                await conn.execute(path.read_text())
            applied.append(path.name)
        return applied


db = Database()
