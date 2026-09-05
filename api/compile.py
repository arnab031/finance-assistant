"""
QuerySpec -> SQL.

Pure function. No LLM, no I/O, no randomness. This module is where the numbers
come from, and it is the reason the assistant cannot hallucinate one: the model
chooses *what* to compute, this file decides *how*, and Postgres does the
arithmetic in NUMERIC.

Every domain-specific map - which dimensions exist, what a metric means, how a
filter becomes a WHERE clause - now comes from the active Profile
(api/profiles/). The compilation LOGIC below is identical for both datasets;
only the vocabulary differs.

Two rules, both absolute:

  1. Every literal is a bound parameter. No f-string ever interpolates a value
     into SQL. (Identifiers come from the profile, never from input.)

  2. Dates compile to half-open range predicates, never substr(). Measured on
     1,019,354 rows: range + idx_txn_date = 18 ms; substr() = 512 ms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from api.profiles.base import Profile, get_profile
from api.registry import SemanticRegistry
from api.schema import QuerySpec


class CompileError(ValueError):
    """The spec is structurally valid but cannot be compiled against this data."""


@dataclass
class CompiledQuery:
    sql: str
    params: dict[str, Any]
    columns: list[str]
    joins: list[str] = field(default_factory=list)
    note: str = ""

    def explain(self) -> str:
        return f"{self.note} | joins: {', '.join(self.joins) or 'none'}"


# --------------------------------------------------------------------------
# Fragment builders
# --------------------------------------------------------------------------


def _in_list(col: str, key: str, values: list, params: dict) -> str:
    """`col IN (%(k_0)s, %(k_1)s, ...)`, binding each value separately.

    MySQL has no array type, so PostgreSQL's `= ANY(%(k)s)` - one placeholder
    bound to a whole Python list - has no equivalent and the list has to be
    spread across N placeholders. Every value is still a bound parameter; the
    only thing built by string interpolation is the placeholder NAMES, which
    are generated here and never derived from user input.

    An empty list yields a predicate that is false rather than invalid SQL:
    `IN ()` is a syntax error in MySQL, and silently dropping the clause would
    turn "none of these" into "all rows", which is the more dangerous failure.
    """
    if not values:
        return "1 = 0"
    names = []
    for i, v in enumerate(values):
        name = f"{key}_{i}"
        params[name] = v
        names.append(f"%({name})s")
    return f"{col} IN ({', '.join(names)})"


def _where(
    spec: QuerySpec, reg: SemanticRegistry, prof: Profile
) -> tuple[list[str], dict[str, Any], set[str]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    joins: set[str] = set()
    date_col = prof.date_column

    # --- temporal. Exactly one basis is authoritative (QuerySpec enforces). ---
    if spec.date_basis == "fiscal_year":
        if not reg.has_fiscal_year or "fiscal_year" not in prof.dimensions:
            raise CompileError("this dataset has no fiscal_year column")
        if spec.fiscal_year:
            clauses.append(f"{prof.dimensions['fiscal_year'].sql} = %(fy)s")
            params["fy"] = spec.fiscal_year
    elif spec.period is not None:
        # Half-open [start, end) -> uses the date index. Never substr().
        clauses.append(f"{date_col} >= %(p_start)s AND {date_col} < %(p_end)s")
        params["p_start"] = spec.period.start
        params["p_end"] = spec.period.end

    # --- list filters, driven entirely by the profile ---
    for field_name, filt in prof.filters.items():
        values = getattr(spec.filters, field_name, None)
        if not values:
            continue

        # Amount bounds are declared as filters so the extraction schema offers
        # them, but they compile against the metric's own column below.
        if filt.kind == "number":
            continue

        # Free-text search is the counterparty mechanism on datasets with no
        # entity table, so it is a LIKE, not a set membership test.
        if field_name.endswith("_like"):
            key = f"f_{field_name}"
            # MySQL has no ILIKE. The tables are utf8mb4_0900_ai_ci, so plain
            # LIKE is already case- and accent-insensitive.
            clauses.append(f"{filt.sql} LIKE %({key})s")
            params[key] = f"%{values}%"
        else:
            key = f"f_{field_name}"
            clauses.append(_in_list(filt.sql, key, list(values), params))

        if filt.join:
            joins.add(filt.join)

    # --- amount bounds, applied to the metric's own column when it is money ---
    amount_col = _amount_column(spec, prof)
    if spec.filters.min_amount is not None:
        op = ">" if spec.filters.min_amount_exclusive else ">="
        clauses.append(f"{amount_col} {op} %(min_amount)s")
        params["min_amount"] = spec.filters.min_amount
    if spec.filters.max_amount is not None:
        op = "<" if spec.filters.max_amount_exclusive else "<="
        clauses.append(f"{amount_col} {op} %(max_amount)s")
        params["max_amount"] = spec.filters.max_amount

    return clauses, params, joins


def _amount_column(spec: QuerySpec, prof: Profile) -> str:
    """The raw per-row column an amount bound or a list sort applies to.

    Derived from the metric's own aggregate expression, so "spend over 50000"
    bounds debits and "received over 50000" bounds credits, and a profile can
    name its columns whatever it likes.

    THE FALLBACK USED TO BE `next(iter(prof.money_metrics))`. money_metrics is a
    FROZENSET, so that returned a different column on every process - measured
    across five runs: net_amount, debit_amount, max_amount, avg_amount,
    avg_amount. "How many transactions over 50000?" therefore answered 0, 2 or 3
    depending on the interpreter's hash seed, and picking credit_amount silently
    compared every debit's 0.00 against the threshold.

    It only became reachable when min_amount/max_amount were added to the
    extraction schema; before that nothing could set a bound on a non-money
    metric. The bound's own Filt already names the raw column, which is what a
    Filt is for, so the fallback now asks it instead of guessing.
    """
    if spec.metric in prof.money_metrics:
        expr = prof.metrics[spec.metric]
        inner = expr[expr.find("(") + 1: expr.rfind(")")]
        if inner and "," not in inner:
            return inner

    for name in ("min_amount", "max_amount"):
        filt = prof.filters.get(name)
        if filt is not None:
            return filt.sql

    # Deterministic last resort: the lowest-named money metric, not an
    # arbitrary one. A profile declaring neither bound gets a stable answer.
    for metric in sorted(prof.money_metrics):
        expr = prof.metrics[metric]
        return expr[expr.find("(") + 1: expr.rfind(")")]
    return f"{prof.alias}.amount"


def _joins_for(group_by: list[str], extra: set[str], prof: Profile) -> list[str]:
    needed = set(extra)
    for dim in group_by:
        join = prof.dimensions[dim].join
        if join:
            needed.add(join)
    # Deterministic order so identical specs produce byte-identical SQL.
    return [k for k in prof.joins if k in needed]


def _order_by(group_by: list[str], sort_desc: bool, aliases: list[str],
              prof: Profile) -> str:
    """Takes the EFFECTIVE grouping, not spec.group_by - `reconcile` may add a
    dimension the spec never declared, and reading it off the spec desyncs."""
    if not aliases or not group_by:
        return ""
    if group_by[0] in prof.time_dimensions:
        # Trends read chronologically regardless of sort_desc.
        return f"ORDER BY {aliases[0]} ASC"
    # MySQL has no NULLS LAST, and its default differs by direction: nulls sort
    # first ascending, last descending. `col IS NULL` yields 0/1, so ordering by
    # it first puts non-nulls ahead in BOTH directions - the Postgres behaviour
    # this replaces, stated explicitly rather than left to a default.
    return f"ORDER BY value IS NULL, value {'DESC' if sort_desc else 'ASC'}"


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def compile_query(spec: QuerySpec, reg: SemanticRegistry) -> CompiledQuery:
    """Compile a spec into an executable, parameterised query."""
    prof = get_profile()

    if spec.intent in ("clarify", "unsupported"):
        raise CompileError(f"intent '{spec.intent}' is not executable")
    if spec.intent in prof.disabled_intents:
        raise CompileError(
            f"intent '{spec.intent}' is not supported by the {prof.label} dataset")
    if spec.intent == "anomaly":
        return _compile_anomaly(spec, reg, prof)
    if spec.intent == "list":
        return _compile_list(spec, reg, prof)
    if spec.intent == "compare":
        return _compile_compare(spec, reg, prof)
    return _compile_aggregate(spec, reg, prof)


def _compile_aggregate(spec: QuerySpec, reg: SemanticRegistry,
                       prof: Profile) -> CompiledQuery:
    clauses, params, join_hint = _where(spec, reg, prof)

    group_by = list(spec.group_by)
    # `reconcile` with no grouping and no status filter means "show me the
    # breakdown". But if the caller already narrowed to specific statuses they
    # asked for a total across those - auto-grouping there would silently answer
    # a different question.
    if (spec.intent == "reconcile" and not group_by
            and not spec.filters.reconciliation_status
            and "reconciliation_status" in prof.dimensions):
        group_by = ["reconciliation_status"]

    joins = _joins_for(group_by, join_hint, prof)
    metric_sql = prof.metrics[spec.metric]

    select_parts, aliases = [], []
    for dim in group_by:
        d = prof.dimensions[dim]
        select_parts.append(f"{d.sql} AS {d.alias}")
        aliases.append(d.alias)

    select_parts.append(f"{metric_sql} AS value")
    if spec.metric != "txn_count":
        select_parts.append("COUNT(*) AS txn_count")

    sql = [f"SELECT {', '.join(select_parts)}", f"FROM {prof.fact}"]
    sql += [prof.joins[j] for j in joins]
    if clauses:
        sql.append("WHERE " + "\n  AND ".join(clauses))
    if aliases:
        sql.append("GROUP BY " + ", ".join(str(i + 1) for i in range(len(aliases))))
        order = _order_by(group_by, spec.sort_desc, aliases, prof)
        if order:
            sql.append(order)
        sql.append("LIMIT %(limit)s")
        params["limit"] = spec.limit

    columns = aliases + ["value"] + ([] if spec.metric == "txn_count" else ["txn_count"])
    return CompiledQuery("\n".join(sql), params, columns, joins,
                         note=f"{spec.metric} grouped by {aliases or ['(total)']}")


def _compile_list(spec: QuerySpec, reg: SemanticRegistry,
                  prof: Profile) -> CompiledQuery:
    clauses, params, join_hint = _where(spec, reg, prof)
    joins = _joins_for(list(spec.group_by), join_hint, prof)

    # account_number is selectable now: the column holds ciphertext, and
    # placeholderise() decrypts it into a placeholder before anything sees it.
    # Blocking the column here would break the feature it was meant to protect.
    cols = list(prof.list_columns)
    if "recon" in joins:
        cols += ["r.reconciliation_status", "r.days_outstanding", "r.exception_reason"]

    sort_col = _amount_column(spec, prof)

    sql = [f"SELECT {', '.join(cols)}", f"FROM {prof.fact}"]
    sql += [prof.joins[j] for j in joins]
    if clauses:
        sql.append("WHERE " + "\n  AND ".join(clauses))
    sql.append(f"ORDER BY {sort_col} IS NULL, "
               f"{sort_col} {'DESC' if spec.sort_desc else 'ASC'}")
    sql.append("LIMIT %(limit)s")
    params["limit"] = spec.limit

    return CompiledQuery("\n".join(sql), params,
                         [c.split(".")[-1] for c in cols], joins,
                         note="individual transactions")


def _compile_compare(spec: QuerySpec, reg: SemanticRegistry,
                     prof: Profile) -> CompiledQuery:
    """Two periods side by side in one pass, labelled by CASE."""
    clauses, params, join_hint = _where(spec, reg, prof)
    joins = _joins_for(list(spec.group_by), join_hint, prof)
    metric_sql = prof.metrics[spec.metric]
    date_col = prof.date_column

    if spec.date_basis == "fiscal_year":
        if not spec.compare_fiscal_year:
            raise CompileError("compare requires compare_fiscal_year")
        fy_col = prof.dimensions["fiscal_year"].sql
        clauses = [c for c in clauses if fy_col not in c]
        params.pop("fy", None)
        clauses.append(_in_list(
            fy_col, "fys", [spec.fiscal_year, spec.compare_fiscal_year], params))
        bucket = f"CASE WHEN {fy_col} = %(fy_current)s THEN 'current' ELSE 'previous' END"
        params["fy_current"] = spec.fiscal_year
        label_a, label_b = f"FY{spec.fiscal_year}", f"FY{spec.compare_fiscal_year}"
    else:
        if spec.period is None or spec.compare_period is None:
            raise CompileError("compare requires period and compare_period")
        clauses = [c for c in clauses if date_col not in c]
        clauses.append(
            f"(({date_col} >= %(p_start)s AND {date_col} < %(p_end)s)"
            f" OR ({date_col} >= %(c_start)s AND {date_col} < %(c_end)s))"
        )
        params |= {"p_start": spec.period.start, "p_end": spec.period.end,
                   "c_start": spec.compare_period.start,
                   "c_end": spec.compare_period.end}
        bucket = (f"CASE WHEN {date_col} >= %(p_start)s AND {date_col} < %(p_end)s "
                  f"THEN 'current' ELSE 'previous' END")
        label_a = spec.period.label or f"{spec.period.start}..{spec.period.end}"
        label_b = spec.compare_period.label or \
            f"{spec.compare_period.start}..{spec.compare_period.end}"

    extra = [prof.dimensions[d] for d in spec.group_by]
    select = [f"{bucket} AS period"] + [f"{d.sql} AS {d.alias}" for d in extra]
    select += [f"{metric_sql} AS value", "COUNT(*) AS txn_count"]

    sql = [f"SELECT {', '.join(select)}", f"FROM {prof.fact}"]
    sql += [prof.joins[j] for j in joins]
    sql.append("WHERE " + "\n  AND ".join(clauses))
    sql.append("GROUP BY " + ", ".join(str(i + 1) for i in range(1 + len(extra))))
    sql.append("ORDER BY 1 DESC")

    columns = ["period"] + [d.alias for d in extra] + ["value", "txn_count"]
    return CompiledQuery("\n".join(sql), params, columns, joins,
                         note=f"comparing {label_a} vs {label_b}")


def _compile_anomaly(spec: QuerySpec, reg: SemanticRegistry,
                     prof: Profile) -> CompiledQuery:
    """Outliers against the entity's own history. Bonus requirement in the brief.

    Had a vendor_payments branch comparing each payout to that vendor's own
    12-payout average. That profile and its vendor_payouts table are gone, so
    the branch went with them.
    """
    # Bank statements: no counterparty history to compare against, so the
    # baseline is the account's own transaction profile.
    clauses, params, _ = _where(spec, reg, prof)
    params["limit"] = spec.limit
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
WITH stats AS (
    SELECT account_id, AVG(transaction_amount) AS mu,
           STDDEV_POP(transaction_amount) AS sd, COUNT(*) AS n
    FROM {prof.fact}
    GROUP BY account_id
    HAVING COUNT(*) >= 5 AND STDDEV_POP(transaction_amount) > 0
)
SELECT t.transaction_date, t.bank_name, t.transaction_type,
       t.transaction_amount AS value,
       ROUND(s.mu, 2) AS account_avg,
       ROUND((t.transaction_amount - s.mu) / s.sd, 1) AS sigma,
       LEFT(t.description, 60) AS description
FROM {prof.fact}
JOIN stats s USING (account_id)
{where}
{"AND" if where else "WHERE"} t.transaction_amount > s.mu + 3 * s.sd
ORDER BY sigma DESC
LIMIT %(limit)s
""".strip()
    return CompiledQuery(
        sql, params,
        ["transaction_date", "bank_name", "transaction_type", "value",
         "account_avg", "sigma", "description"],
        [], note="transactions more than 3 sigma above the account's own mean")


def compile_scalar(spec: QuerySpec, reg: SemanticRegistry) -> CompiledQuery:
    """
    Same filters, no grouping - one value plus a row count.

    Used by the ambiguity prober (AMBIGUITY.md). It shares _where() with
    compile_query, so a probe can never disagree with the answer it predicts.
    """
    prof = get_profile()
    flat = spec.model_copy(update={"group_by": [], "intent": "aggregate"})
    clauses, params, join_hint = _where(flat, reg, prof)
    joins = _joins_for([], join_hint, prof)

    sql = [f"SELECT {prof.metrics[spec.metric]} AS value, COUNT(*) AS n",
           f"FROM {prof.fact}"]
    sql += [prof.joins[j] for j in joins]
    if clauses:
        sql.append("WHERE " + "\n  AND ".join(clauses))

    return CompiledQuery("\n".join(sql), params, ["value", "n"], joins,
                         note="scalar probe")
