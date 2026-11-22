import type { Cell } from "./types";

const ADDRESS_RE = /^0x[0-9a-f]{40}$/i;
const HASH_RE = /^0x[0-9a-f]{64}$/i;
const NUMERIC_RE = /^-?\d+(\.\d+)?$/;
const TIMESTAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;

export function isAddress(v: unknown): v is string {
  return typeof v === "string" && ADDRESS_RE.test(v);
}

export function isHash(v: unknown): v is string {
  return typeof v === "string" && HASH_RE.test(v);
}

export function shortHex(v: string, head = 6, tail = 4): string {
  return v.length <= head + tail + 1 ? v : `${v.slice(0, head)}…${v.slice(-tail)}`;
}

/** Digits grouped the Vietnamese way (1.234.567,89); exact for numbers sent as text. */
function groupDigits(text: string): string {
  const negative = text.startsWith("-");
  const [int, frac] = (negative ? text.slice(1) : text).split(".");
  const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  const decimals = frac ? frac.replace(/0+$/, "") : "";
  return (negative ? "-" : "") + grouped + (decimals ? "," + decimals : "");
}

export function isNumeric(v: Cell): boolean {
  return typeof v === "number" || (typeof v === "string" && NUMERIC_RE.test(v));
}

export function formatCell(v: Cell): string {
  if (v === null) return "NULL";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return groupDigits(String(v));
    const abs = Math.abs(v);
    const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 8;
    return groupDigits(v.toFixed(digits));
  }
  if (typeof v === "string") {
    if (NUMERIC_RE.test(v)) return groupDigits(v);
    if (TIMESTAMP_RE.test(v)) return v.replace("T", " ").replace(/(\.\d+)?(Z|[+-]\d{2}:\d{2})?$/, "");
    return v;
  }
  return JSON.stringify(v);
}

export function formatSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined || Number.isNaN(s)) return "–";
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  return `${s.toFixed(s < 10 ? 1 : 0).replace(".", ",")} s`;
}

export function formatPercent(x: number | null | undefined, digits = 1): string {
  if (x === null || x === undefined) return "–";
  return `${(x * 100).toFixed(digits).replace(".", ",")}%`;
}

export function formatCount(n: number): string {
  return groupDigits(String(n));
}

export function formatDateTime(iso: string): string {
  return iso.replace("T", " ").replace(/:00$/, "");
}
