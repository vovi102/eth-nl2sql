import { formatCell, formatCount, formatSeconds, isAddress, isHash, isNumeric, shortHex } from "@/lib/format";
import { ERROR_KINDS } from "@/lib/labels";
import type { Cell, ExecutionResult } from "@/lib/types";
import { AlertIcon } from "./icons";
import { CopyButton } from "./SqlBlock";

function CellView({ value }: { value: Cell }) {
  if (isAddress(value) || isHash(value)) {
    return (
      <span className="group inline-flex items-center gap-1 font-mono text-[12px]" title={value}>
        {shortHex(value, 8, 6)}
        <span className="opacity-0 group-hover:opacity-100">
          <CopyButton text={value} label="Sao chép địa chỉ" />
        </span>
      </span>
    );
  }
  const text = formatCell(value);
  return (
    <span className={value === null ? "text-faint italic" : ""} title={typeof value === "string" ? value : undefined}>
      {text}
    </span>
  );
}

export function ErrorCallout({ kind, message }: { kind: string | null; message: string | null }) {
  return (
    <div className="flex gap-2.5 rounded-lg border border-bad/30 bg-bad-soft px-3 py-2.5 text-sm">
      <AlertIcon size={16} className="mt-0.5 shrink-0 text-bad" />
      <div className="min-w-0">
        <div className="font-medium text-bad">{(kind && ERROR_KINDS[kind]) || kind || "Lỗi"}</div>
        {message && <div className="mt-0.5 break-words font-mono text-xs text-muted">{message}</div>}
      </div>
    </div>
  );
}

export function ResultView({ result, compact = false }: { result: ExecutionResult; compact?: boolean }) {
  if (!result.ok) return <ErrorCallout kind={result.error_kind} message={result.error} />;
  const { columns, rows } = result;
  const meta = (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
      <span>
        {formatCount(result.row_count)} dòng
        {result.truncated && " (đã cắt ở 10.000)"}
        {result.rows_shown < result.row_count && `, hiện ${formatCount(result.rows_shown)}`}
      </span>
      <span>DuckDB {formatSeconds(result.seconds)}</span>
    </div>
  );

  if (rows.length === 1 && columns.length === 1 && !compact) {
    const value = rows[0][0];
    return (
      <div className="space-y-2">
        <div className="rounded-xl border border-border bg-surface px-5 py-4">
          <div className="text-xs font-medium tracking-wide text-muted">{columns[0]}</div>
          <div className="mt-1 break-all text-3xl font-semibold tabular-nums tracking-tight">
            <CellView value={value} />
          </div>
        </div>
        {meta}
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="space-y-2">
        <div className="rounded-lg border border-dashed border-border-strong px-4 py-3 text-sm text-muted">
          Truy vấn chạy được nhưng không trả về dòng nào.
        </div>
        {meta}
      </div>
    );
  }

  const numeric = columns.map((_, j) => rows.every((r) => r[j] === null || isNumeric(r[j])));
  return (
    <div className="space-y-2">
      <div className={`scroll-thin overflow-auto rounded-lg border border-border bg-surface ${compact ? "max-h-64" : "max-h-96"}`}>
        <table className="w-full border-collapse text-[13px]">
          <thead className="sticky top-0 z-10 bg-surface-2">
            <tr>
              <th className="w-10 border-b border-border px-2 py-2 text-right text-xs font-medium text-faint">#</th>
              {columns.map((c, j) => (
                <th
                  key={j}
                  className={`border-b border-border px-3 py-2 text-xs font-semibold whitespace-nowrap text-muted ${numeric[j] ? "text-right" : "text-left"}`}
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className="odd:bg-surface even:bg-surface-2/40 hover:bg-accent-soft/50">
                <td className="border-b border-border/60 px-2 py-1.5 text-right text-xs text-faint tabular-nums">{i + 1}</td>
                {row.map((v, j) => (
                  <td
                    key={j}
                    className={`border-b border-border/60 px-3 py-1.5 whitespace-nowrap ${numeric[j] ? "text-right tabular-nums" : ""}`}
                  >
                    <CellView value={v} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {meta}
    </div>
  );
}
