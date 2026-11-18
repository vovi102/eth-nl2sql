"use client";

import { useState } from "react";
import { CheckIcon, CopyIcon } from "./icons";

const KEYWORDS = new Set(
  (
    "select from where group by order limit as and or not in is null join left right inner outer " +
    "cross full on having with distinct desc asc case when then else end between like ilike union " +
    "all over partition interval true false exists offset nulls first last filter using"
  ).split(" "),
);
const FUNCTIONS = new Set(
  (
    "count sum avg min max cast date_trunc date_part extract coalesce round abs lower upper " +
    "row_number rank dense_rank try_cast strftime epoch to_timestamp nullif greatest least " +
    "length median quantile_cont percentile_cont stddev"
  ).split(" "),
);

const TOKEN_RE =
  /(--[^\n]*)|('(?:[^']|'')*')|(\{E\d+\})|(\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+)|(.)/gi;

function highlight(sql: string) {
  const out: React.ReactNode[] = [];
  let m: RegExpExecArray | null;
  let i = 0;
  TOKEN_RE.lastIndex = 0;
  while ((m = TOKEN_RE.exec(sql)) !== null) {
    const [text, comment, string, placeholder, number, word] = m;
    let cls: string | null = null;
    if (comment) cls = "sql-comment";
    else if (string) cls = "sql-string";
    else if (placeholder) cls = "sql-placeholder";
    else if (number) cls = "sql-number";
    else if (word) {
      const lower = word.toLowerCase();
      if (KEYWORDS.has(lower)) cls = "sql-keyword";
      else if (FUNCTIONS.has(lower) && sql[TOKEN_RE.lastIndex] === "(") cls = "sql-function";
    }
    out.push(cls ? <span key={i++} className={cls}>{text}</span> : text);
  }
  return out;
}

export function CopyButton({ text, label = "Sao chép" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        } catch {
          // clipboard blocked: nothing to do
        }
      }}
      className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-muted hover:bg-surface-3 hover:text-text"
      title={label}
    >
      {copied ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
      <span className="sr-only">{label}</span>
    </button>
  );
}

export function SqlBlock({
  sql,
  title,
  actions,
  maxHeight = "20rem",
  wrap = true,
}: {
  sql: string;
  title?: React.ReactNode;
  actions?: React.ReactNode;
  maxHeight?: string;
  wrap?: boolean;
}) {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-code">
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-1.5">
        <span className="text-xs font-medium text-muted">{title ?? "SQL"}</span>
        <div className="flex items-center gap-1">
          {actions}
          <CopyButton text={sql} />
        </div>
      </div>
      <pre
        className={`scroll-thin overflow-auto px-3 py-2.5 font-mono text-[12.5px] leading-relaxed ${wrap ? "whitespace-pre-wrap break-words" : ""}`}
        style={{ maxHeight }}
      >
        <code>{highlight(sql)}</code>
      </pre>
    </div>
  );
}
