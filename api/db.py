"""
Async MySQL access via aiomysql.

Ported from psycopg3/PostgreSQL when the team moved to MySQL. Two properties
carried over deliberately:

MONEY STAYS EXACT. PyMySQL maps DECIMAL -> decimal.Decimal natively, the same
way psycopg mapped NUMERIC. Nothing in this file may convert a money value to
float; that would reintroduce exactly the drift the schema avoids.

PLACEHOLDERS ARE UNCHANGED. Both drivers use pyformat (%(name)s), so the port
did not touch a single call site's parameter style - only the dialect of the SQL
around it. See MYSQL_PORT.md.

THE % TRAP. PyMySQL interpolates the query string whenever args is not None,
so a literal % in SQL becomes a placeholder and raises. psycopg had the same
hazard and _args() below solves it the same way. The stronger fix is upstream:
no generated SQL contains a literal %, which is why the month/quarter dimensions
use INTERVAL arithmetic rather than DATE_FORMAT(x, '%Y-%m-01').
"""

from __future__ import annotations

import json
import time
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import aiomysql

from api.config import settings

SQL_DIR = Path(__file__).parent / "sql" / "mysql"


def json_column(value: Any, default: Any = None) -> Any:
    """Parse a JSON column. PyMySQL hands one back as TEXT.

    psycopg decoded JSONB into a dict or list before the value ever reached
    application code, so every read site here was written assuming that. MySQL
    returns the raw string, and the failures are ugly rather than obvious:

      settled.get(...)          -> AttributeError: 'str' object has no attribute 'get'
      set(spec) | set(new)      -> a set of CHARACTERS, silently, then AttributeError
      "{}" or {}                -> "{}", because a non-empty string is truthy, so
                                   the usual `or {}` fallback never fires

    Applied explicitly at each read rather than blanket-decoding every string
    column, because nothing in the result set says which columns are JSON and
    guessing would corrupt ordinary text that happens to start with a brace.
    """
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value                     # already decoded (or a future driver)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return default


def _args(params: dict | None) -> tuple:
    """PyMySQL interpolates whenever args is not None - an empty dict included.
    A literal % in the SQL then raises. Passing nothing at all skips
    interpolation, which is what a parameterless query wants."""
    return (params,) if params else ()


def dsn_parts(dsn: str) -> dict[str, Any]:
    """mysql://user:pass@host:port/db -> aiomysql connect kwargs."""
    u = urlparse(dsn)
    return {
        "host": u.hostname or "127.0.0.1",
        "port": u.port or 3306,
        "user": unquote(u.username or "root"),
        "password": unquote(u.password or ""),
        "db": (u.path or "/").lstrip("/") or None,
    }


def split_statements(script: str) -> list[str]:
    """Split a .sql file into statements on semicolons outside quotes/comments.

    aiomysql executes one statement per call unless MULTI_STATEMENTS is enabled,
    and enabling that for the whole pool would let any injected semicolon start a
    second query. Splitting here keeps the pool single-statement.

    Quote and comment tracking is what makes this safe: a naive split on ";"
    would cut a semicolon inside a string literal or a comment.
    """
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(script)

    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""

        if quote:
            buf.append(ch)
            if ch == "\\" and nxt:            # escaped char inside a string
                buf.append(nxt)
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue

        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif ch == "-" and nxt == "-":         # line comment to end of line
            j = script.find("\n", i)
            i = n if j == -1 else j
            continue
        elif ch == "#":
            j = script.find("\n", i)
            i = n if j == -1 else j
            continue
        elif ch == "/" and nxt == "*":         # block comment
            j = script.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


class Database:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or settings.database_url
        self._pool: aiomysql.Pool | None = None

    # ---- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        self._pool = await aiomysql.create_pool(
            minsize=settings.pool_min_size,
            maxsize=settings.pool_max_size,
            autocommit=True,          # every write here is a single statement
            charset="utf8mb4",
            cursorclass=aiomysql.DictCursor,
            # DATETIME(6) carries no offset, so CURRENT_TIMESTAMP(6) records
            # whatever zone the session happens to be in - UTC on this host,
            # something else on the next one, and the stored value cannot say
            # which. Pinning the session makes "naive means UTC" an invariant
            # this app enforces. api/clock.py stamps the offset back on at the
            # API boundary; without both halves the ops page is off by 5:30.
            init_command="SET time_zone = '+00:00'",
            **dsn_parts(self._dsn),
        )

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    @property
    def pool(self) -> aiomysql.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() has not been called")
        return self._pool

    # ---- queries ---------------------------------------------------------

    async def fetch(self, sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, *_args(params))
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: dict | None = None) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, *_args(params))
            return await cur.fetchone()

    async def column(self, sql: str, params: dict | None = None) -> list[Any]:
        """First column of every row - for vocabulary loading."""
        rows = await self.fetch(sql, params)
        return [next(iter(r.values())) for r in rows]

    async def scalar(self, sql: str, params: dict | None = None) -> Any:
        row = await self.fetchone(sql, params)
        return next(iter(row.values())) if row else None

    async def execute(self, sql: str, params: dict | None = None) -> None:
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, *_args(params))

    async def insert_returning_id(self, sql: str, params: dict | None = None) -> int | None:
        """MySQL has no RETURNING. lastrowid is per-connection, so it must be
        read on the same cursor that ran the INSERT - which is why this is a
        method here rather than a second call at the call site."""
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, *_args(params))
            return cur.lastrowid or None

    async def fetch_timed(
        self, sql: str, params: dict | None = None
    ) -> tuple[list[str], list[list[Any]], int]:
        """Returns (columns, rows-as-lists, elapsed_ms) - shaped for RowsEvent."""
        t0 = time.perf_counter()
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, *_args(params))
            records = await cur.fetchall()
            # aiomysql descriptions are plain tuples; psycopg's had .name
            columns = [d[0] for d in (cur.description or [])]
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return columns, [[r[c] for c in columns] for r in records], elapsed_ms

    # ---- introspection ---------------------------------------------------
    #
    # information_schema is standard, but the schema predicate is not: Postgres
    # namespaced everything under 'public', MySQL uses the database itself.

    async def has_table(self, name: str) -> bool:
        return bool(await self.scalar(
            """SELECT COUNT(*) FROM information_schema.TABLES
               WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %(n)s""",
            {"n": name},
        ))

    async def has_column(self, table: str, column: str) -> bool:
        return bool(await self.scalar(
            """SELECT COUNT(*) FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE()
                 AND TABLE_NAME = %(t)s AND COLUMN_NAME = %(c)s""",
            {"t": table, "c": column},
        ))

    async def numeric_columns(self, table: str) -> list[str]:
        return await self.column(
            """SELECT COLUMN_NAME FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %(t)s
                 AND DATA_TYPE IN ('decimal','double','float','int','bigint',
                                   'smallint','mediumint','tinyint')
               ORDER BY ORDINAL_POSITION""",
            {"t": table},
        )

    # ---- migrations ------------------------------------------------------

    async def migrate(self) -> list[str]:
        """Apply api/sql/mysql/**.sql in order. All statements are idempotent.

        live/ runs first: it holds the domain schema the app tables and views
        have nothing to say about, and the seed data the canary measures against.
        """
        applied: list[str] = []
        paths = sorted((SQL_DIR / "live").glob("*.sql")) + sorted(SQL_DIR.glob("*.sql"))
        for path in paths:
            for stmt in split_statements(path.read_text()):
                # The constructs that make these migrations re-runnable -
                # CREATE TABLE IF NOT EXISTS and INSERT IGNORE - each raise a
                # MySQL warning on the second and every later boot. They are
                # the migration working as designed, and logging ~30 of them
                # every start would train everyone to ignore migration output.
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=Warning)
                    await self.execute(stmt)
            applied.append(path.name)
        return applied


db = Database()
