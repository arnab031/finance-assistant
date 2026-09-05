/**
 * The ledger is Indian - UPI/NEFT/IMPS rails, IFSC codes, accounts at Indian
 * banks - so every amount in it is rupees. "en-IN" carries both halves of that:
 * the INR symbol and Indian digit grouping (1,69,299.00, not 169,299.00).
 * Counts share the locale so one screen never mixes two grouping conventions.
 *
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
  return n.toLocaleString("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * "1 row", "12 rows". Shared because it was written twice and only one copy
 * pluralised: the same result read "1 row" above the table and "1 rows" on the
 * provenance line right below it.
 */
export function countLabel(n: number, singular: string, plural = `${singular}s`) {
  return `${formatCount(n)} ${n === 1 ? singular : plural}`;
}

export function formatCount(v: unknown): string {
  const n = typeof v === "number" ? v : isNumericString(v) ? Number(v) : NaN;
  return Number.isNaN(n) ? String(v ?? "") : n.toLocaleString("en-IN");
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

/**
 * Recorded instants are stored UTC and read in IST.
 *
 * The zone is NAMED rather than left to the viewer's machine: this page is read
 * by a team working IST hours, and "was the canary run since the deploy?" has
 * to mean the same thing to all of them. The suffix says so out loud, because a
 * bare wall-clock reading is exactly what made the old rendering wrong.
 *
 * Depends on the API sending an offset (api/clock.py). Without one, JS reads
 * the string as local time and the shift silently comes back.
 */
const IST = "Asia/Kolkata";

export function formatInstant(v: string | null | undefined): string {
  if (!v) return "—";
  const t = new Date(v);
  if (Number.isNaN(t.getTime())) return String(v);
  const stamp = t.toLocaleString("en-IN", {
    timeZone: IST,
    day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false,
  });
  return `${stamp} IST`;
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
