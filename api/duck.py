"""
DuckDB read replica for the analytical path.

WHY THIS EXISTS. The compiled queries are scan-heavy aggregates over an
immutable transaction log, grouped by low-cardinality dimensions. That is a
columnar workload, and InnoDB is a row store fighting its own design on it.
Measured on the same 10M rows, same queries, idle machine, indexes restored:

                                        MySQL       DuckDB    speedup
    group by transaction_type          26.57s       0.150s       177x
    scalar, no filter                  21.31s       0.140s       152x
    group by bank                      28.10s       0.168s       167x
    one month + group by type           5.16s       0.084s        62x
    monthly trend (45 groups)          26.24s       0.186s       141x

Both engines returned identical row shapes on all five. The filtered case is the
smallest multiple and the fairest one - a date range is where MySQL's index
genuinely helps, and 62x is what remains after it has. The same grouped
aggregate has been seen at ~120s on a LOADED machine; DuckDB does not move
either way, because it reads ~551MB of compressed columns and touches only the
columns the query names. All on the schema AS SHIPPED - no added indexes, no
added columns - which is the constraint that rules the alternatives out.

The join is the part that matters. `v_txn` denormalises bank and account onto
every transaction, and because a MySQL view is INLINED rather than materialised,
every query pays transaction -> account -> bank: one random lookup per row,
10M of them, behind a 128MB buffer pool. Removing that cost in MySQL needs either new indexes or new
columns - DDL on a database we may not own. DuckDB needs neither. It issues only
SELECTs against MySQL, so it works against a source we have read access to and
nothing more.

WHAT IT IS NOT. Not the system of record. MySQL still holds the truth and every
write - query_log, chat_threads, eval_*, semantic_index. This replica is
disposable: a full rebuild takes ~66s at 10M rows, cheap enough that incremental
sync would be complexity bought for nothing.

TWO THINGS THE ADAPTER HAS TO RECONCILE:

  1. PARAMETER STYLE. api/compile.py emits pyformat (%(name)s) because both
     psycopg and PyMySQL use it. DuckDB wants $name. `_to_duck_params` rewrites
     the placeholders rather than the compiler, so the SQL a user sees in the
     provenance panel stays the SQL that ran against MySQL.

  2. DUCKDB IS SYNCHRONOUS. Calling it directly from the event loop would block
     every other request for the duration of a scan. Every query goes through
     asyncio.to_thread, and each one takes its own cursor() - DuckDB cursors are
     independent connections over one database, which is what makes concurrent
     use safe.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

import duckdb

from api.config import settings
from api.db import dsn_parts

log = logging.getLogger("tbx.duck")

# Tables copied wholesale from MySQL. Small enough that a full copy beats any
# cleverness: 10M + 100k + 10 rows lands in a 1.4GB file in ~66 seconds.
SOURCE_TABLES = ("bank", "account", "transaction")

_PYFORMAT = re.compile(r"%\((\w+)\)s")


def _dsn() -> str:
    """MySQL DSN for ATTACH, with single quotes doubled.

    ATTACH is DDL and rejects placeholders, so the string is inlined; a quote in
    a configured password would otherwise end the literal early.
    """
    p = dsn_parts(settings.database_url)
    return (f"host={p['host']} port={p['port']} user={p['user']} "
            f"password={p['password']} database={p['db']}").replace("'", "''")


def _mysql_scalar(con, sql: str):
    """Run one SELECT on the attached MySQL and return its first value.

    mysql_query() takes the source SQL as a *literal*, not a parameter, so the
    escaping happens here rather than at each call site.
    """
    row = con.execute(
        f"SELECT * FROM mysql_query('mysql_src', '{sql.replace(chr(39), chr(39) * 2)}')"
    ).fetchone()
    return row[0] if row else None


def _source_watermark(con) -> str | None:
    """MAX(watermark) on the SOURCE as a string, or None when unconfigured.

    A string because the column may be an integer id or a timestamp, and the
    comparison only ever asks "did this move?", never "by how much". With an
    AUTO_INCREMENT id this is an index seek, so the staleness check costs
    nothing regardless of table size.
    """
    col = settings.duckdb_watermark_column
    if not col:
        return None
    value = _mysql_scalar(con, f"SELECT MAX(`{col}`) AS w FROM `transaction`")
    return None if value is None else str(value)


def _to_duck_params(sql: str, params: dict | None) -> tuple[str, list]:
    """pyformat -> DuckDB positional, preserving argument order.

    Positional rather than $name because a name may legitimately appear more
    than once in one statement (compile.py reuses %(p_start)s in both the WHERE
    and the CASE of a compare query), and repeating the value is simpler to
    reason about than relying on DuckDB's named-parameter reuse semantics.
    """
    if not params:
        return _PYFORMAT.sub("?", sql), []
    ordered: list[Any] = []

    def sub(m: re.Match) -> str:
        ordered.append(params[m.group(1)])
        return "?"

    return _PYFORMAT.sub(sub, sql), ordered


class DuckReplica:
    """Read-only analytical mirror of the MySQL database."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or settings.duckdb_path
        self._con: duckdb.DuckDBPyConnection | None = None

    # ---- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(self._path)

    async def close(self) -> None:
        if self._con is not None:
            con, self._con = self._con, None
            await asyncio.to_thread(con.close)

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            raise RuntimeError("DuckReplica.connect() has not been called")
        return self._con

    async def is_built(self) -> bool:
        """True when the replica holds data. Cheap enough to call on every boot."""
        try:
            n = await self.scalar("SELECT COUNT(*) FROM v_txn")
            return bool(n)
        except Exception:  # noqa: BLE001 - a missing table is the expected miss
            return False

    # ---- build -----------------------------------------------------------

    async def build(self) -> dict[str, Any]:
        """(Re)load every source table from MySQL, then define v_txn."""
        return await asyncio.to_thread(self._build_sync)

    def _build_sync(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        p = dsn_parts(settings.database_url)
        con = self.con
        con.execute("INSTALL mysql; LOAD mysql;")
        # Everything in this replica is UTC, matching the invariant api/db.py
        # pins on the MySQL pool ("naive means UTC").
        con.execute("SET TimeZone = 'UTC'")
        # READ_ONLY is the guarantee that matters: this attachment can never
        # write to the system of record, whatever a later query says.
        con.execute(f"ATTACH '{_dsn()}' AS mysql_src (TYPE mysql, READ_ONLY)")
        try:
            counts: dict[str, int] = {}
            for table in SOURCE_TABLES:
                select, casts = self._projection(con, p["db"], table)
                inner = f"SELECT {select} FROM `{table}`".replace("'", "''")
                con.execute(
                    f'CREATE OR REPLACE TABLE "{table}" AS '
                    f"SELECT {casts} FROM mysql_query('mysql_src', '{inner}')"
                )
                counts[table] = con.execute(
                    f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                log.info("duck: loaded %s (%s rows)", table, f"{counts[table]:,}")
            con.execute(_V_TXN_SQL)
            # Provenance lives INSIDE the replica rather than in a sidecar
            # file, so a .duckdb copied anywhere still answers "what is this a
            # copy of, and how current?" on its own.
            con.execute(
                "CREATE OR REPLACE TABLE _replica_meta ("
                " source_rows BIGINT, watermark VARCHAR, built_at TIMESTAMP)"
            )
            con.execute(
                "INSERT INTO _replica_meta VALUES (?, ?, now())",
                [counts.get("transaction", 0), _source_watermark(con)],
            )
        finally:
            con.execute("DETACH mysql_src")

        elapsed = int((time.perf_counter() - t0) * 1000)
        log.info("duck replica built in %sms: %s", elapsed, counts)
        return {"counts": counts, "build_ms": elapsed}

    @staticmethod
    def _projection(con, schema: str, table: str) -> tuple[str, str]:
        """(mysql SELECT list, duckdb cast list) for one table.

        THE TIMESTAMP TRAP. MySQL converts a TIMESTAMP column from stored UTC
        into the *session* time zone on read. api/db.py pins the app's pool to
        +00:00, but DuckDB's scanner opens its own connection and gets the
        server default - SYSTEM, which here is IST. The same column therefore
        read 5h30m apart, and every date-filtered query disagreed:

            MIN(transaction_date)  app 2022-12-31 18:30:35
                                  duck 2023-01-01 00:00:35

        Unfiltered totals still matched exactly, so this hid until a filtered
        query was compared - the worst kind of discrepancy, because it looks
        like correct data.

        The fix pulls UNIX_TIMESTAMP() instead, which is the true epoch
        regardless of session zone, and rebuilds the timestamp in UTC on this
        side. Subtracting a fixed offset would also work here (IST has no DST)
        but would be silently wrong for any source in a DST zone.

        DATETIME columns are NOT converted by MySQL and are copied untouched.
        """
        rows = con.execute(
            "SELECT column_name, data_type FROM mysql_query('mysql_src', "
            "'SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_schema = ''{schema}'' AND table_name = ''{table}'' "
            "ORDER BY ordinal_position')"
        ).fetchall()
        select, casts = [], []
        for name, dtype in rows:
            if str(dtype).lower() == "timestamp":
                select.append(f"UNIX_TIMESTAMP(`{name}`) AS `{name}`")
                # to_timestamp yields TIMESTAMPTZ; the cast drops the zone so
                # the column stays naive-UTC like its MySQL counterpart.
                casts.append(
                    f'CAST(to_timestamp(CAST("{name}" AS DOUBLE)) AS TIMESTAMP) AS "{name}"')
            else:
                select.append(f"`{name}`")
                casts.append(f'"{name}"')
        return ", ".join(select), ", ".join(casts)

    # ---- freshness -------------------------------------------------------

    async def ensure_built(self) -> dict[str, Any]:
        """Build, refresh, or reuse - decided from the data, not from a flag.

        A boolean could not express this. `rebuild_on_boot=True` paid a full
        rebuild on every start (~60s at 10M rows, ~2min at 20M); False served
        whatever happened to be on disk, including a replica built against a
        different database or before a reload. Neither is right twice running,
        so it had to be flipped by hand and remembered.

        "auto" asks the source instead, and the question is cheap by
        construction - see _is_stale_sync.
        """
        policy = settings.duckdb_refresh
        if not await self.is_built():
            return {"action": "built", **await self.build()}
        if policy == "always":
            return {"action": "rebuilt", **await self.build()}
        if policy == "never":
            return {"action": "reused", "reason": "duckdb_refresh=never"}

        stale, why = await self._is_stale()
        if stale:
            log.info("duck replica stale (%s); rebuilding", why)
            return {"action": "refreshed", "reason": why, **await self.build()}
        log.info("duck replica current (%s); reusing", why)
        return {"action": "reused", "reason": why}

    async def _is_stale(self) -> tuple[bool, str]:
        return await asyncio.to_thread(self._is_stale_sync)

    def _is_stale_sync(self) -> tuple[bool, str]:
        con = self.con
        try:
            have = con.execute(
                "SELECT source_rows, watermark FROM _replica_meta").fetchone()
        except Exception:  # noqa: BLE001 - replica predates _replica_meta
            return True, "no build metadata"
        if have is None:
            return True, "empty build metadata"
        have_rows, have_mark = have

        con.execute("INSTALL mysql; LOAD mysql;")
        con.execute(f"ATTACH '{_dsn()}' AS mysql_src (TYPE mysql, READ_ONLY)")
        try:
            mark = _source_watermark(con)
            if mark is not None:
                if str(mark) != str(have_mark):
                    return True, f"watermark {have_mark} -> {mark}"
                return False, f"watermark unchanged ({mark})"

            # No watermark configured: fall back to the APPROXIMATE row count.
            # An exact COUNT(*) is not an option - it exceeded 300s on 10M rows
            # here, dwarfing the 60s rebuild it exists to avoid.
            db_name = dsn_parts(settings.database_url)["db"]
            approx = _mysql_scalar(
                con,
                "SELECT table_rows AS n FROM information_schema.tables "
                f"WHERE table_schema = '{db_name}' AND table_name = 'transaction'",
            )
            if approx is None:
                return True, "source row count unavailable"
            approx = int(approx)
            # InnoDB's estimate drifts by a few percent even when nothing has
            # changed, so only a real difference counts as stale.
            if abs(approx - have_rows) > max(1000, 0.02 * have_rows):
                return True, f"approx rows {have_rows:,} -> {approx:,}"
            return False, f"approx rows within tolerance ({approx:,})"
        finally:
            con.execute("DETACH mysql_src")

    # ---- queries (mirror api/db.py:Database) ------------------------------

    async def fetch(self, sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        cols, rows, _ = await self.fetch_timed(sql, params)
        return [dict(zip(cols, r)) for r in rows]

    async def fetchone(self, sql: str, params: dict | None = None) -> dict[str, Any] | None:
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None

    async def column(self, sql: str, params: dict | None = None) -> list[Any]:
        _, rows, _ = await self.fetch_timed(sql, params)
        return [r[0] for r in rows]

    async def scalar(self, sql: str, params: dict | None = None) -> Any:
        _, rows, _ = await self.fetch_timed(sql, params)
        return rows[0][0] if rows else None

    async def fetch_timed(
        self, sql: str, params: dict | None = None
    ) -> tuple[list[str], list[list[Any]], int]:
        """Returns (columns, rows-as-lists, elapsed_ms) - shaped for RowsEvent."""
        return await asyncio.to_thread(self._fetch_timed_sync, sql, params)

    def _fetch_timed_sync(
        self, sql: str, params: dict | None
    ) -> tuple[list[str], list[list[Any]], int]:
        duck_sql, args = _to_duck_params(sql, params)
        t0 = time.perf_counter()
        # A fresh cursor per call: DuckDB cursors are independent connections
        # over the same database, so parallel to_thread calls cannot interleave
        # on one cursor's result set.
        cur = self.con.cursor()
        try:
            cur.execute(duck_sql, args)
            columns = [d[0] for d in (cur.description or [])]
            records = cur.fetchall()
        finally:
            cur.close()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return columns, [list(r) for r in records], elapsed_ms


# --------------------------------------------------------------------------
# v_txn, translated from api/sql/mysql/live/002_views.sql.
#
# Same columns, same semantics, so the compiler cannot tell the engines apart.
# The joins STAY here: DuckDB answers the two-join form in 51ms, so the
# denormalisation MySQL needed is simply unnecessary - which is the whole
# reason this replica can run against a database we cannot alter.
# --------------------------------------------------------------------------

_V_TXN_SQL = """
CREATE OR REPLACE VIEW v_txn AS
SELECT
    t.transaction_id,
    t.account_id,
    a.entity_id,
    a.program_id,
    a.bank_code,
    b.bank_name,
    a.account_number,
    t.transaction_date,
    CAST(t.transaction_date AS DATE)      AS transaction_day,
    t.transaction_type,
    t.description,
    t.counterparty,
    t.transaction_amount,
    CASE WHEN t.transaction_type = 'credit'
         THEN t.transaction_amount ELSE -t.transaction_amount END AS signed_amount,
    CASE WHEN t.transaction_type = 'credit'
         THEN t.transaction_amount ELSE 0 END                     AS credit_amount,
    CASE WHEN t.transaction_type = 'debit'
         THEN t.transaction_amount ELSE 0 END                     AS debit_amount,
    t.transaction_reference_id,
    CASE WHEN t.utr_number IS NULL THEN NULL
         ELSE 'UTR-' || RIGHT(CAST(t.utr_number AS VARCHAR), 4) END AS utr_masked,
    (t.utr_number IS NOT NULL)                                    AS has_utr
FROM "transaction" t
JOIN "account" a ON a.account_id = t.account_id
JOIN "bank"    b ON b.bank_code  = a.bank_code;
"""


duck = DuckReplica()


def reader():
    """The engine that answers compiled analytical queries.

    Reads go to the replica when it is enabled; every WRITE (query_log,
    chat_threads, eval_*, semantic_index) keeps using api.db.db directly, so
    MySQL stays the single system of record. Routing lives in one function so
    "which engine ran this?" has exactly one answer.
    """
    from api.db import db

    return duck if settings.use_duckdb else db
