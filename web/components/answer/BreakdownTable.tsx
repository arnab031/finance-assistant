"use client";

import { useMemo, useState } from "react";
import type { RowsPayload } from "../chat/Chat";
import { columnLabel, formatCell, isMoneyColumn, isRightAligned } from "@/lib/format";
import { downloadCsv, toCsv } from "@/lib/toCsv";

/**
 * The verifiable half of every answer. The brief asks each response to pair
 * plain language with the underlying records - this is that, and it renders
 * BEFORE the narration streams.
 */
export default function BreakdownTable({ rows }: { rows: RowsPayload }) {
  const [sort, setSort] = useState<{ col: number; desc: boolean } | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return rows.rows;
    const col = sort.col;
    const numeric = isMoneyColumn(rows.columns[col]);
    return [...rows.rows].sort((a, b) => {
      const x = a[col], y = b[col];
      const cmp = numeric
        ? Number(x ?? 0) - Number(y ?? 0)
        : String(x ?? "").localeCompare(String(y ?? ""));
      return sort.desc ? -cmp : cmp;
    });
  }, [rows, sort]);

  if (rows.row_count === 0) {
    return <p className="table-empty">No matching records.</p>;
  }

  return (
    <div className="table-block">
      <div className="table-meta">
        <span>
          {rows.row_count.toLocaleString()} {rows.row_count === 1 ? "row" : "rows"}
          {rows.truncated && <em> · showing first {rows.rows.length}</em>}
          <span className="table-timing"> · {rows.elapsed_ms} ms</span>
        </span>
        <button
          className="btn btn-ghost"
          onClick={() =>
            downloadCsv(`${rows.result_id}.csv`, toCsv(rows.columns, rows.rows))
          }
        >
          Export CSV
        </button>
      </div>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              {rows.columns.map((c, i) => (
                <th
                  key={c}
                  className={isRightAligned(c) ? "num" : undefined}
                  onClick={() =>
                    setSort((s) =>
                      s?.col === i ? { col: i, desc: !s.desc } : { col: i, desc: true },
                    )
                  }
                >
                  {columnLabel(c)}
                  {sort?.col === i && <span className="sort">{sort.desc ? "▾" : "▴"}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row, r) => (
              <tr key={r}>
                {row.map((cell, c) => (
                  <td key={c} className={isRightAligned(rows.columns[c]) ? "num" : undefined}>
                    {formatCell(rows.columns[c], cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
