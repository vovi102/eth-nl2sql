import { MENTION_TYPES } from "@/lib/labels";
import type { Linking, Span } from "@/lib/types";

interface Segment {
  span: Span;
  kind: string;
  tag?: string | null;
  title: string;
}

/** The question with every linked span marked: mentions, properties, time, unsupported. */
export function LinkedQuestion({ linking }: { linking: Linking }) {
  const segments: Segment[] = [];
  for (const m of linking.mentions) {
    const what = m.label ? `${m.label}` : m.target ?? m.text;
    segments.push({
      span: m.span,
      kind: m.type,
      tag: m.placeholder,
      title: `${MENTION_TYPES[m.type] ?? m.type}: ${what}`,
    });
  }
  for (const p of linking.properties) {
    if (p.span) segments.push({ span: p.span, kind: "property", title: `Thuộc tính: ${p.sense_id}` });
  }
  for (const u of linking.unsupported) {
    if (u.span) segments.push({ span: u.span, kind: "unsupported", title: `Ngoài phạm vi: ${u.sense_id}` });
  }
  if (linking.time) segments.push({ span: linking.time.span, kind: "time", title: "Khoảng thời gian" });

  segments.sort((a, b) => a.span[0] - b.span[0] || b.span[1] - a.span[1]);
  const q = linking.question;
  const parts: React.ReactNode[] = [];
  let pos = 0;
  for (const s of segments) {
    const [start, end] = s.span;
    if (start < pos) continue; // overlapping: keep the first
    if (start > pos) parts.push(q.slice(pos, start));
    parts.push(
      <mark key={`${start}-${end}`} className={`span-mark span-${s.kind}`} title={s.title}>
        {q.slice(start, end)}
        {s.tag && <sup className="ml-0.5 font-mono text-[10px] font-semibold">{s.tag}</sup>}
      </mark>,
    );
    pos = end;
  }
  if (pos < q.length) parts.push(q.slice(pos));
  return <p className="text-[15px] leading-8">{parts}</p>;
}

export function Legend({ kinds }: { kinds: string[] }) {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
      {kinds.map((k) => (
        <span key={k} className="inline-flex items-center gap-1.5">
          <span className={`dot span-${k}`} style={{ background: "var(--c)" }} />
          {MENTION_TYPES[k] ?? k}
        </span>
      ))}
    </div>
  );
}
