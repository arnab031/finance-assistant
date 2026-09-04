"""
QuerySpec -> SQL.

Pure function. No LLM, no I/O, no randomness. This module is where the numbers
come from, and it is the reason the assistant cannot hallucinate one: the model
chooses *what* to compute, this file decides *how*, and Postgres does the
arithmetic in NUMERIC.

Two rules, both absolute:

  1. Every literal is a bound parameter. No f-string ever interpolates a value
     into SQL. (Identifiers come from frozen dicts below, never from input.)

  2. Dates compile to half-open range predicates, never substr(). Measured on
     1,019,354 rows: range + idx_txn_date = 18 ms; substr() = 512 ms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from api.registry import SemanticRegistry
from api.schema import Dimension, Metric, QuerySpec

# --------------------------------------------------------------------------
# Static maps. Identifiers only - these are the *only* strings ever formatted
# into SQL, and none of them come from user or model input.
# --------------------------------------------------------------------------

METRIC_SQL: dict[Metric, str] = {
    "amount_paid":      "SUM(t.amount_paid)",
    "amount_total":     "SUM(t.amount_total)",
    "amount_pending":   "SUM(t.amount_pending)",
    "amount_retainage": "SUM(t.amount_retainage)",
    "txn_count":        "COUNT(*)",
    "voucher_count":    "COUNT(DISTINCT t.voucher_id)",
    "vendor_count":     "COUNT(DISTINCT t.vendor_id)",
    "avg_amount":       "AVG(t.amount_paid)",
}

MONEY_METRICS = {"amount_paid", "amount_total", "amount_pending", "amount_retainage",
                 "avg_amount"}

# dimension -> (sql expression, output alias, required join key)
DIMENSION_SQL: dict[Dimension, tuple[str, str, str | None]] = {
    "vendor":                ("t.vendor_name", "vendor", None),
    "category":              ("t.category_name", "category", None),
    "account":               ("t.account_name", "account", None),
    "object":                ("c.object_name", "object", "coa"),
    "department":            ("t.department_name", "department", None),
    "fund":                  ("t.fund_name", "fund", None),
    "fund_type":             ("f.fund_type_name", "fund_type", "funds"),
    "program":               ("t.program_name", "program", None),
    "month":                 ("date_trunc('month', t.transaction_date)::date", "month", None),
    "quarter":               ("date_trunc('quarter', t.transaction_date)::date", "quarter", None),
    "fiscal_year":           ("t.fiscal_year", "fiscal_year", None),
    "payment_status":        ("t.payment_status", "payment_status", None),
    "reconciliation_status": ("r.reconciliation_status", "reconciliation_status", "recon"),
}

JOIN_SQL: dict[str, str] = {
    "coa":   "JOIN chart_of_accounts c ON c.account_code = t.account_code",
    "funds": "JOIN funds f ON f.fund_code = t.fund_code",
    "recon": "JOIN reconciliation r ON r.transaction_id = t.transaction_id",
}

TIME_DIMENSIONS = {"month", "quarter", "fiscal_year"}

# filter field -> (sql expression, required join key)
FILTER_SQL: dict[str, tuple[str, str | None]] = {
    "vendor_ids":            ("t.vendor_id", None),
    "categories":            ("t.category_name", None),
    "departments":           ("t.department_name", None),
    "funds":                 ("t.fund_name", None),
    "programs":              ("t.program_name", None),
    "payment_status":        ("t.payment_status", None),
    "reconciliation_status": ("r.reconciliation_status", "recon"),
}


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


def _where(spec: QuerySpec, reg: SemanticRegistry) -> tuple[list[str], dict[str, Any], set[str]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    joins: set[str] = set()

    # --- temporal. Exactly one basis is authoritative (QuerySpec enforces). ---
    if spec.date_basis == "fiscal_year":
        if not reg.has_fiscal_year:
            raise CompileError("this dataset has no fiscal_year column")
        if spec.fiscal_year:
            clauses.append("t.fiscal_year = %(fy)s")
            params["fy"] = spec.fiscal_year
    elif spec.period is not None:
        # Half-open [start, end) -> uses idx_txn_date. Never substr().
        clauses.append("t.transaction_date >= %(p_start)s AND t.transaction_date < %(p_end)s")
        params["p_start"] = spec.period.start
        params["p_end"] = spec.period.end

    # --- list filters ---
    for field_name, (expr, join) in FILTER_SQL.items():
        values = getattr(spec.filters, field_name, None)
        if not values:
            continue
        key = f"f_{field_name}"
        clauses.append(f"{expr} = ANY(%({key})s)")
        params[key] = list(values)
        if join:
            joins.add(join)

    # --- amount bounds, applied to the metric's own column when it is money ---
    amount_col = ("t.amount_paid" if spec.metric not in MONEY_METRICS or spec.metric == "avg_amount"
                  else f"t.{spec.metric}")
    if spec.filters.min_amount is not None:
        clauses.append(f"{amount_col} >= %(min_amount)s")
        params["min_amount"] = spec.filters.min_amount
    if spec.filters.max_amount is not None:
        clauses.append(f"{amount_col} <= %(max_amount)s")
        params["max_amount"] = spec.filters.max_amount

    return clauses, params, joins


def _joins_for(spec: QuerySpec, extra: set[str]) -> list[str]:
    needed = set(extra)
    for dim in spec.group_by:
        join = DIMENSION_SQL[dim][2]
        if join:
            needed.add(join)
    if spec.intent == "reconcile":
        needed.add("recon")
    # Deterministic order so identical specs produce byte-identical SQL.
    return [k for k in ("coa", "funds", "recon") if k in needed]


def _order_by(group_by: list[Dimension], sort_desc: bool, group_aliases: list[str]) -> str:
    """Takes the EFFECTIVE grouping, not spec.group_by - `reconcile` may add a
    dimension the spec never declared, and reading it off the spec desyncs."""
    if not group_aliases or not group_by:
        return ""
    if group_by[0] in TIME_DIMENSIONS:
        # Trends read chronologically regardless of sort_desc.
        return f"ORDER BY {group_aliases[0]} ASC"
    return f"ORDER BY value {'DESC' if sort_desc else 'ASC'} NULLS LAST"


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def compile_query(spec: QuerySpec, reg: SemanticRegistry) -> CompiledQuery:
    """Compile a spec into an executable, parameterised query."""
    if spec.intent in ("clarify", "unsupported"):
        raise CompileError(f"intent '{spec.intent}' is not executable")
    if spec.intent == "anomaly":
        return _compile_anomaly(spec, reg)
    if spec.intent == "list":
        return _compile_list(spec, reg)
    if spec.intent == "compare":
        return _compile_compare(spec, reg)
    return _compile_aggregate(spec, reg)


def _compile_aggregate(spec: QuerySpec, reg: SemanticRegistry) -> CompiledQuery:
    clauses, params, join_hint = _where(spec, reg)

    group_by = list(spec.group_by)
    # `reconcile` with no grouping and no status filter means "show me the
    # breakdown". But if the caller already narrowed to specific statuses they
    # asked for a total across those - auto-grouping there would silently answer
    # a different question.
    if (spec.intent == "reconcile" and not group_by
            and not spec.filters.reconciliation_status):
        group_by = ["reconciliation_status"]

    joins = _joins_for(spec.model_copy(update={"group_by": group_by}), join_hint)
    metric_sql = METRIC_SQL[spec.metric]

    select_parts, aliases = [], []
    for dim in group_by:
        expr, alias, _ = DIMENSION_SQL[dim]
        select_parts.append(f"{expr} AS {alias}")
        aliases.append(alias)

    select_parts.append(f"{metric_sql} AS value")
    if spec.metric != "txn_count":
        select_parts.append("COUNT(*) AS txn_count")

    sql = [f"SELECT {', '.join(select_parts)}", "FROM transactions t"]
    sql += [JOIN_SQL[j] for j in joins]
    if clauses:
        sql.append("WHERE " + "\n  AND ".join(clauses))
    if aliases:
        sql.append("GROUP BY " + ", ".join(str(i + 1) for i in range(len(aliases))))
        order = _order_by(group_by, spec.sort_desc, aliases)
        if order:
            sql.append(order)
        sql.append("LIMIT %(limit)s")
        params["limit"] = spec.limit

    columns = aliases + ["value"] + ([] if spec.metric == "txn_count" else ["txn_count"])
    return CompiledQuery("\n".join(sql), params, columns, joins,
                         note=f"{spec.metric} grouped by {aliases or ['(total)']}")


def _compile_list(spec: QuerySpec, reg: SemanticRegistry) -> CompiledQuery:
    clauses, params, join_hint = _where(spec, reg)
    joins = _joins_for(spec, join_hint)

    cols = ["t.transaction_id", "t.voucher_id", "t.transaction_date", "t.vendor_name",
            "t.department_name", "t.category_name", "t.amount_paid", "t.amount_total",
            "t.payment_status"]
    if "recon" in joins:
        cols += ["r.reconciliation_status", "r.days_outstanding", "r.exception_reason"]

    sort_col = f"t.{spec.metric}" if spec.metric in MONEY_METRICS and spec.metric != "avg_amount" \
        else "t.amount_paid"

    sql = [f"SELECT {', '.join(cols)}", "FROM transactions t"]
    sql += [JOIN_SQL[j] for j in joins]
    if clauses:
        sql.append("WHERE " + "\n  AND ".join(clauses))
    sql.append(f"ORDER BY {sort_col} {'DESC' if spec.sort_desc else 'ASC'} NULLS LAST")
    sql.append("LIMIT %(limit)s")
    params["limit"] = spec.limit

    return CompiledQuery("\n".join(sql), params,
                         [c.split(".")[-1] for c in cols], joins, note="individual transactions")


def _compile_compare(spec: QuerySpec, reg: SemanticRegistry) -> CompiledQuery:
    """Two periods side by side in one pass, labelled by CASE."""
    clauses, params, join_hint = _where(spec, reg)
    joins = _joins_for(spec, join_hint)
    metric_sql = METRIC_SQL[spec.metric]

    if spec.date_basis == "fiscal_year":
        if not spec.compare_fiscal_year:
            raise CompileError("compare requires compare_fiscal_year")
        clauses = [c for c in clauses if "t.fiscal_year" not in c]
        clauses.append("t.fiscal_year = ANY(%(fys)s)")
        params.pop("fy", None)
        params["fys"] = [spec.fiscal_year, spec.compare_fiscal_year]
        bucket = ("CASE WHEN t.fiscal_year = %(fy_current)s THEN 'current' "
                  "ELSE 'previous' END")
        params["fy_current"] = spec.fiscal_year
        label_a = f"FY{spec.fiscal_year}"
        label_b = f"FY{spec.compare_fiscal_year}"
    else:
        if spec.period is None or spec.compare_period is None:
            raise CompileError("compare requires period and compare_period")
        clauses = [c for c in clauses if "t.transaction_date" not in c]
        clauses.append(
            "((t.transaction_date >= %(p_start)s AND t.transaction_date < %(p_end)s)"
            " OR (t.transaction_date >= %(c_start)s AND t.transaction_date < %(c_end)s))"
        )
        params |= {"p_start": spec.period.start, "p_end": spec.period.end,
                   "c_start": spec.compare_period.start, "c_end": spec.compare_period.end}
        bucket = ("CASE WHEN t.transaction_date >= %(p_start)s "
                  "AND t.transaction_date < %(p_end)s THEN 'current' ELSE 'previous' END")
        label_a = spec.period.label or f"{spec.period.start}..{spec.period.end}"
        label_b = spec.compare_period.label or \
            f"{spec.compare_period.start}..{spec.compare_period.end}"

    extra_dims = [DIMENSION_SQL[d] for d in spec.group_by]
    select = [f"{bucket} AS period"] + [f"{e} AS {a}" for e, a, _ in extra_dims]
    select += [f"{metric_sql} AS value", "COUNT(*) AS txn_count"]

    sql = [f"SELECT {', '.join(select)}", "FROM transactions t"]
    sql += [JOIN_SQL[j] for j in joins]
    sql.append("WHERE " + "\n  AND ".join(clauses))
    sql.append("GROUP BY " + ", ".join(str(i + 1) for i in range(1 + len(extra_dims))))
    sql.append("ORDER BY 1 DESC")

    columns = ["period"] + [a for _, a, _ in extra_dims] + ["value", "txn_count"]
    return CompiledQuery("\n".join(sql), params, columns, joins,
                         note=f"comparing {label_a} vs {label_b}")


def _compile_anomaly(spec: QuerySpec, reg: SemanticRegistry) -> CompiledQuery:
    """Payouts far above a vendor's own history. Bonus requirement in the brief."""
    if not reg.has_payouts:
        raise CompileError("this dataset has no vendor_payouts table")

    params: dict[str, Any] = {"limit": spec.limit, "min_hist": 12, "factor": 10.0,
                              "floor": spec.filters.min_amount or 1_000_000}
    date_clause = ""
    if spec.period is not None:
        date_clause = "WHERE p.payout_date >= %(p_start)s AND p.payout_date < %(p_end)s"
        params |= {"p_start": spec.period.start, "p_end": spec.period.end}

    sql = f"""
WITH stats AS (
    SELECT vendor_id, AVG(gross_amount) AS mu, COUNT(*) AS n
    FROM vendor_payouts GROUP BY vendor_id
    HAVING COUNT(*) >= %(min_hist)s AND AVG(gross_amount) > 0
)
SELECT p.payout_date, p.vendor_name, p.gross_amount AS value,
       ROUND(s.mu, 2) AS vendor_avg,
       ROUND(p.gross_amount / s.mu, 1) AS times_avg,
       p.transaction_count AS txn_count
FROM vendor_payouts p
JOIN stats s USING (vendor_id)
{date_clause}
{"AND" if date_clause else "WHERE"} p.gross_amount > s.mu * %(factor)s
  AND p.gross_amount > %(floor)s
ORDER BY times_avg DESC
LIMIT %(limit)s
""".strip()

    return CompiledQuery(
        sql, params,
        ["payout_date", "vendor_name", "value", "vendor_avg", "times_avg", "txn_count"],
        [], note="payouts >10x the vendor's own 12+ payout average",
    )


def compile_scalar(spec: QuerySpec, reg: SemanticRegistry) -> CompiledQuery:
    """
    Same filters, no grouping - one value plus a row count.

    Used by the ambiguity prober (AMBIGUITY.md §Architecture). It shares
    _where() with compile_query, so a probe can never disagree with the answer
    it is predicting.
    """
    flat = spec.model_copy(update={"group_by": [], "intent": "aggregate"})
    clauses, params, join_hint = _where(flat, reg)
    joins = _joins_for(flat, join_hint)

    sql = [f"SELECT {METRIC_SQL[spec.metric]} AS value, COUNT(*) AS n", "FROM transactions t"]
    sql += [JOIN_SQL[j] for j in joins]
    if clauses:
        sql.append("WHERE " + "\n  AND ".join(clauses))

    return CompiledQuery("\n".join(sql), params, ["value", "n"], joins, note="scalar probe")
