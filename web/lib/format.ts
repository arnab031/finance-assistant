/**
 * Money arrives from the API as a STRING, deliberately: it was kept exact in
 * NUMERIC all the way through Postgres, and JSON numbers are float64. Parsing
 * to Number here is for display only - never for arithmetic.
 */

export function isNumericString(v: unknown): v is string {
  return typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v);
}

export function formatMoney(v: unknown): string {
  const n = typeof v === "number" ? v : isNumericString(v) ? Number(v) : NaN;
  if (Number.isNaN(n)) return String(v ?? "");
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatCount(v: unknown): string {
  const n = typeof v === "number" ? v : isNumericString(v) ? Number(v) : NaN;
  return Number.isNaN(n) ? String(v ?? "") : n.toLocaleString("en-US");
}

const MONEY_COLUMNS = new Set([
  "value", "amount_paid", "amount_total", "amount_pending", "amount_retainage",
  "gross_amount", "paid_amount", "pending_amount", "retainage_amount",
  "ledger_amount", "bank_amount", "variance", "exposure", "vendor_avg",
  "total_amount",
]);
const COUNT_COLUMNS = new Set([
  "txn_count", "transaction_count", "row_count", "vouchers", "vendors",
  "line_items", "days_outstanding", "department_count", "n",
]);

export function isMoneyColumn(name: string) {
  return MONEY_COLUMNS.has(name);
}

export function formatCell(name: string, v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (MONEY_COLUMNS.has(name)) return formatMoney(v);
  if (COUNT_COLUMNS.has(name)) return formatCount(v);
  if (typeof v === "number") return formatCount(v);
  return String(v);
}

export function columnLabel(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .replace(/\bTxn\b/, "Txns")
    .replace(/\bSql\b/, "SQL");
}

export function isRightAligned(name: string) {
  return MONEY_COLUMNS.has(name) || COUNT_COLUMNS.has(name);
}
