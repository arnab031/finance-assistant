import type { Coverage } from "@/lib/types";
import { formatCount, formatMoney } from "@/lib/format";

/**
 * Always visible. The single most useful thing a grounded assistant can tell
 * you up front is what it actually knows about - it makes "no data for that
 * period" legible before you ask.
 */
export default function CoverageBanner({ coverage }: { coverage: Coverage | null }) {
  if (!coverage) {
    return (
      <div className="coverage coverage-down">
        Backend unreachable — start it with{" "}
        <code>./.venv/bin/uvicorn api.main:app --port 8000</code>
      </div>
    );
  }
  return (
    <div className="coverage">
      <span className="coverage-item">
        <span className="coverage-key">Data covers</span>
        <strong>{coverage.earliest} → {coverage.latest}</strong>
      </span>
      <span className="coverage-sep" />
      <span className="coverage-item">
        <strong>{formatCount(coverage.transaction_count)}</strong>
        <span className="coverage-key">transactions</span>
      </span>
      <span className="coverage-sep" />
      <span className="coverage-item">
        <strong>{formatMoney(coverage.total_paid)}</strong>
        <span className="coverage-key">paid</span>
      </span>
    </div>
  );
}
