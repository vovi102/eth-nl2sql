import type { ExampleSummary } from "@/lib/types";
import { ChevronRightIcon, CpuIcon, DatabaseIcon, FileIcon, LinkIcon } from "./icons";

const FACTS = [
  ["215.033", "block"],
  ["56,9 triệu", "giao dịch"],
  ["103,6 triệu", "token transfer"],
  ["95.473", "token"],
];

const STEPS = [
  { icon: <LinkIcon size={15} />, title: "Entity linking", text: "tên → địa chỉ, thuộc tính, thời gian" },
  { icon: <FileIcon size={15} />, title: "Prompt", text: "schema + evidence với {E1}" },
  { icon: <CpuIcon size={15} />, title: "Qwen2.5-Coder", text: "sinh SQL, sửa lỗi một lần" },
  { icon: <DatabaseIcon size={15} />, title: "DuckDB", text: "thay địa chỉ, chạy SQL" },
];

/** Up to n samples, alternating languages, one per template. */
function pick(samples: ExampleSummary[], n: number): ExampleSummary[] {
  const out: ExampleSummary[] = [];
  const templates = new Set<string | null>();
  const langs = ["vi", "en"];
  for (let round = 0; out.length < n && round < samples.length; round++) {
    const lang = langs[out.length % 2];
    const next = samples.find((s) => s.lang === lang && !templates.has(s.template_id) && s.expected === "sql");
    if (!next) break;
    templates.add(next.template_id);
    out.push(next);
  }
  return out;
}

export function EmptyState({ samples, onPick }: { samples: ExampleSummary[]; onPick: (q: string) => void }) {
  const chosen = pick(samples, 6);
  return (
    <div className="mx-auto max-w-2xl pt-6 sm:pt-12">
      <h1 className="text-2xl font-semibold tracking-tight sm:text-[28px]">Hỏi dữ liệu Ethereum bằng ngôn ngữ tự nhiên</h1>
      <p className="mt-2 text-[15px] text-muted">
        Tiếng Việt hoặc tiếng Anh. Hệ thống dịch câu hỏi thành SQL và chạy trên toàn bộ dữ liệu tháng 9/2026 của
        Ethereum mainnet.
      </p>
      <div className="mt-4 flex flex-wrap gap-2">
        {FACTS.map(([n, label]) => (
          <span key={label} className="rounded-full border border-border bg-surface px-3 py-1 text-xs">
            <span className="font-semibold tabular-nums">{n}</span> <span className="text-muted">{label}</span>
          </span>
        ))}
      </div>

      <div className="mt-7 grid grid-cols-2 gap-2 sm:grid-cols-4">
        {STEPS.map((s, i) => (
          <div key={s.title} className="relative rounded-xl border border-border bg-surface px-3 py-2.5">
            <div className="flex items-center gap-2 text-sm font-medium">
              <span className="text-accent">{s.icon}</span>
              {s.title}
            </div>
            <div className="mt-0.5 text-xs text-muted">{s.text}</div>
            {i < STEPS.length - 1 && (
              <ChevronRightIcon size={14} className="absolute top-1/2 -right-2.5 z-10 hidden -translate-y-1/2 text-faint sm:block" />
            )}
          </div>
        ))}
      </div>

      {chosen.length > 0 && (
        <>
          <div className="mt-8 mb-2 text-xs font-semibold tracking-wide text-faint uppercase">Thử một câu</div>
          <div className="grid gap-2 sm:grid-cols-2">
            {chosen.map((s) => (
              <button
                key={s.question_id}
                type="button"
                onClick={() => onPick(s.question)}
                className="group rounded-xl border border-border bg-surface px-3.5 py-3 text-left text-sm hover:border-accent hover:bg-accent-soft/40"
              >
                <span className="mr-1.5 text-[10px] font-semibold text-faint uppercase">{s.lang}</span>
                {s.question}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
