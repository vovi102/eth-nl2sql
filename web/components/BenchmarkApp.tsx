"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { formatPercent } from "@/lib/format";
import { DECISIONS, ERROR_KINDS, EX_KINDS, EXPECTED, LANGS } from "@/lib/labels";
import type { ExampleDetail, ExampleSummary, ExecutionResult, ModelInfo } from "@/lib/types";
import { useServer } from "@/lib/useServer";
import { ChatIcon, CheckIcon, MenuIcon, PlayIcon, Spinner, XIcon } from "./icons";
import { PhaseBadge } from "./ModelPicker";
import { ErrorCallout, ResultView } from "./ResultView";
import { Sidebar } from "./Sidebar";
import { SqlBlock } from "./SqlBlock";
import { StatusPill, ThemeToggle } from "./StatusPill";

type Verdict = "all" | "correct" | "wrong";

function Mark({ correct, kind }: { correct: boolean | undefined; kind?: string }) {
  if (correct === undefined) return <span className="text-faint">–</span>;
  return (
    <span
      className={`inline-flex h-6 w-6 items-center justify-center rounded-md ${correct ? "bg-ok-soft text-ok" : "bg-bad-soft text-bad"}`}
      title={kind ? (EX_KINDS[kind] ?? kind) : undefined}
    >
      {correct ? <CheckIcon size={13} /> : <XIcon size={13} />}
    </span>
  );
}

function RunButton({
  onRun,
  label = "Chạy SQL",
}: {
  onRun: () => Promise<void>;
  label?: string;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await onRun();
        } finally {
          setBusy(false);
        }
      }}
      className="inline-flex items-center gap-1 rounded-md bg-accent px-2 py-1 text-xs font-medium text-accent-text hover:bg-accent-strong disabled:opacity-60"
    >
      {busy ? <Spinner size={11} /> : <PlayIcon size={11} />} {label}
    </button>
  );
}

function Detail({
  split,
  qid,
  models,
  onClose,
}: {
  split: string;
  qid: string;
  models: ModelInfo[];
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<ExampleDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, ExecutionResult | { failed: string }>>({});

  useEffect(() => {
    setDetail(null);
    setResults({});
    setError(null);
    api
      .example(split, qid)
      .then(setDetail)
      .catch((e: Error) => setError(e.message));
  }, [split, qid]);

  const run = async (source: string) => {
    try {
      const r = await api.runSql(split, qid, source);
      setResults((x) => ({ ...x, [source]: r }));
    } catch (e) {
      setResults((x) => ({ ...x, [source]: { failed: (e as Error).message } }));
    }
  };

  const shown = (source: string) => {
    const r = results[source];
    if (!r) return null;
    // ExecutionResult has an `error` field too (null when the query ran): a failed request is `failed`
    if ("failed" in r) return <ErrorCallout kind="Lỗi" message={r.failed} />;
    return <ResultView result={r} compact />;
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
        <div className="min-w-0">
          <div className="text-xs text-muted">
            {qid} · {split}
            {detail?.template_id && (
              <>
                {" "}
                · <span className="font-mono">{detail.template_id}</span>
              </>
            )}
          </div>
          <h2 className="mt-1 text-[17px] leading-snug font-semibold">{detail?.question ?? "…"}</h2>
          {detail && (
            <div className="mt-1.5 text-xs text-muted">
              {LANGS[detail.lang ?? ""] ?? detail.lang} · {EXPECTED[detail.expected] ?? detail.expected}
            </div>
          )}
        </div>
        <button type="button" onClick={onClose} className="rounded-md p-1.5 text-muted hover:bg-surface-2">
          <XIcon size={16} />
        </button>
      </div>
      <div className="scroll-thin flex-1 space-y-5 overflow-y-auto px-5 py-4">
        {error && <ErrorCallout kind="Lỗi" message={error} />}
        {!detail && !error && <Spinner className="text-accent" />}
        {detail && (
          <>
            <Link
              href={`/?q=${encodeURIComponent(detail.question)}`}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-surface-2"
            >
              <ChatIcon size={13} /> Hỏi live câu này
            </Link>

            {detail.gold.gold_sql ? (
              <section className="space-y-2">
                <SqlBlock
                  sql={detail.gold.gold_sql}
                  title="SQL gold"
                  maxHeight="12rem"
                  actions={<RunButton onRun={() => run("gold")} />}
                />
                {shown("gold")}
              </section>
            ) : (
              <div className="rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-muted">
                Không có SQL gold: câu đúng là từ chối trả lời ({detail.expected}).
              </div>
            )}

            {models.map((m) => {
              const p = detail.predictions[m.id];
              const r = detail.results[m.id];
              if (!p) return null;
              const first = p.attempts[0];
              return (
                <section key={m.id} className="space-y-2 rounded-xl border border-border bg-surface p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold">{m.label}</span>
                      <PhaseBadge phase={m.phase} />
                    </div>
                    <div className="flex items-center gap-2">
                      {r && (
                        <span className={`text-xs font-medium ${r.correct ? "text-ok" : "text-bad"}`}>
                          {EX_KINDS[r.kind] ?? r.kind}
                        </span>
                      )}
                      <Mark correct={r?.correct} kind={r?.kind} />
                    </div>
                  </div>
                  {p.decision !== "sql" ? (
                    <p className="text-sm text-muted">{DECISIONS[p.decision]?.title ?? p.decision}</p>
                  ) : (
                    <>
                      {p.attempts.length > 1 && first?.error_kind && (
                        <p className="text-xs text-warn">
                          Lần 1 lỗi ({ERROR_KINDS[first.error_kind] ?? first.error_kind}), đã sửa ở lần 2.
                        </p>
                      )}
                      {p.sql && (
                        <SqlBlock
                          sql={p.sql}
                          title="SQL của lần chạy đã chốt"
                          maxHeight="12rem"
                          actions={<RunButton onRun={() => run(m.id)} />}
                        />
                      )}
                      {shown(m.id)}
                      <Link
                        href={`/?q=${encodeURIComponent(detail.question)}&model=${m.id}`}
                        className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
                      >
                        Hỏi live với {m.label}
                      </Link>
                    </>
                  )}
                </section>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}

export function BenchmarkApp() {
  const { models, byId, status, offline } = useServer();
  const [split, setSplit] = useState("dev");
  const [questions, setQuestions] = useState<ExampleSummary[]>([]);
  const [runModels, setRunModels] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [lang, setLang] = useState("all");
  const [template, setTemplate] = useState("all");
  const [verdictModel, setVerdictModel] = useState("");
  const [verdict, setVerdict] = useState<Verdict>("all");
  const [selected, setSelected] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  useEffect(() => {
    setLoading(true);
    setError(null);
    api
      .examples(split)
      .then((r) => {
        setQuestions(r.questions);
        setRunModels(r.models);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [split]);

  const shownModels = useMemo(() => models.filter((m) => runModels.includes(m.id)), [models, runModels]);
  const templates = useMemo(
    () => Array.from(new Set(questions.map((q) => q.template_id ?? "–"))).sort(),
    [questions],
  );

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return questions.filter((q) => {
      if (lang !== "all" && q.lang !== lang) return false;
      if (template !== "all" && (q.template_id ?? "–") !== template) return false;
      if (needle && !q.question.toLowerCase().includes(needle) && !q.question_id.includes(needle)) return false;
      if (verdict !== "all" && verdictModel) {
        const r = q.results[verdictModel];
        if (!r || r.correct !== (verdict === "correct")) return false;
      }
      return true;
    });
  }, [questions, search, lang, template, verdict, verdictModel]);

  const splits = status?.splits ?? ["dev"];

  return (
    <div className="flex h-dvh overflow-hidden">
      <Sidebar active="benchmark" open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-2 border-b border-border px-2 py-2 sm:px-4">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setSidebarOpen(true)}
              className="rounded-lg p-2 text-muted hover:bg-surface-2 md:hidden"
              aria-label="Menu"
            >
              <MenuIcon size={18} />
            </button>
            <h1 className="text-[15px] font-semibold">Benchmark</h1>
            <div className="ml-2 flex rounded-lg border border-border p-0.5">
              {["dev", "test"].map((s) => {
                const available = splits.includes(s);
                return (
                  <button
                    key={s}
                    type="button"
                    disabled={!available}
                    onClick={() => {
                      setSplit(s);
                      setSelected(null);
                    }}
                    title={available ? undefined : "Chạy eth-nl2sql serve --allow-test để xem tập test"}
                    className={`rounded-md px-2.5 py-1 text-xs font-medium ${split === s ? "bg-surface-3 text-text" : "text-muted hover:text-text"} disabled:opacity-40`}
                  >
                    {s}
                  </button>
                );
              })}
            </div>
          </div>
          <div className="flex items-center gap-1">
            <StatusPill status={status} models={byId} offline={offline} />
            <ThemeToggle />
          </div>
        </header>

        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-6xl space-y-4 px-4 py-5">
            <p className="max-w-3xl text-sm text-muted">
              Kết quả các lần chạy đã chốt trong luận văn trên tập {split}: mỗi câu, mỗi cấu hình, đúng hay sai theo EX
              (so kết quả SQL với gold). Bấm một dòng để xem SQL và chạy lại trên DuckDB.
            </p>

            <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
              {shownModels.map((m) => {
                const scored = rows.filter((q) => q.results[m.id]);
                const correct = scored.filter((q) => q.results[m.id].correct).length;
                const ex = scored.length ? correct / scored.length : null;
                return (
                  <div key={m.id} className="rounded-xl border border-border bg-surface px-3.5 py-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-xs font-medium text-muted" title={m.label}>
                        {m.label.replace("Qwen2.5-Coder ", "")}
                      </span>
                      <PhaseBadge phase={m.phase} />
                    </div>
                    <div className="mt-1 flex items-baseline gap-2">
                      <span className="text-2xl font-semibold tabular-nums">{formatPercent(ex)}</span>
                      <span className="text-xs text-muted tabular-nums">
                        {correct}/{scored.length}
                      </span>
                    </div>
                    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-3">
                      <div
                        className="h-full rounded-full"
                        style={{ width: `${(ex ?? 0) * 100}%`, background: m.phase === 2 ? "var(--accent)" : "var(--faint)" }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Tìm câu hỏi hoặc mã câu…"
                className="min-w-0 flex-1 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm outline-none focus:border-accent sm:max-w-xs"
              />
              <select
                value={lang}
                onChange={(e) => setLang(e.target.value)}
                className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm"
              >
                <option value="all">Mọi ngôn ngữ</option>
                <option value="vi">Tiếng Việt</option>
                <option value="en">English</option>
              </select>
              <select
                value={template}
                onChange={(e) => setTemplate(e.target.value)}
                className="max-w-[14rem] rounded-lg border border-border bg-surface px-2 py-1.5 text-sm"
              >
                <option value="all">Mọi template</option>
                {templates.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
              <select
                value={verdictModel}
                onChange={(e) => setVerdictModel(e.target.value)}
                className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm"
              >
                <option value="">Lọc theo model…</option>
                {shownModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
              {verdictModel && (
                <select
                  value={verdict}
                  onChange={(e) => setVerdict(e.target.value as Verdict)}
                  className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm"
                >
                  <option value="all">đúng và sai</option>
                  <option value="correct">chỉ câu đúng</option>
                  <option value="wrong">chỉ câu sai</option>
                </select>
              )}
              <span className="text-xs text-muted">{rows.length} câu</span>
            </div>

            {error && <ErrorCallout kind="Lỗi" message={error} />}
            {loading ? (
              <Spinner className="text-accent" />
            ) : (
              <div className="overflow-x-auto rounded-xl border border-border bg-surface">
                <table className="w-full text-sm">
                  <thead className="bg-surface-2 text-xs text-muted">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Mã</th>
                      <th className="px-3 py-2 text-left font-medium">Câu hỏi</th>
                      <th className="hidden px-3 py-2 text-left font-medium lg:table-cell">Template</th>
                      {shownModels.map((m) => (
                        <th key={m.id} className="px-2 py-2 text-center font-medium whitespace-nowrap" title={m.label}>
                          {m.label.replace("Qwen2.5-Coder ", "")}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((q) => (
                      <tr
                        key={q.question_id}
                        onClick={() => setSelected(q.question_id)}
                        className={`cursor-pointer border-t border-border/70 hover:bg-accent-soft/40 ${selected === q.question_id ? "bg-accent-soft/60" : ""}`}
                      >
                        <td className="px-3 py-2 font-mono text-xs whitespace-nowrap text-muted">{q.question_id}</td>
                        <td className="px-3 py-2">
                          <span className="mr-1.5 text-[10px] font-semibold text-faint uppercase">{q.lang}</span>
                          {q.question}
                          {q.expected !== "sql" && (
                            <span className="ml-1.5 rounded bg-surface-3 px-1 text-[10px] text-muted">{q.expected}</span>
                          )}
                        </td>
                        <td className="hidden px-3 py-2 font-mono text-xs text-muted lg:table-cell">{q.template_id}</td>
                        {shownModels.map((m) => (
                          <td key={m.id} className="px-2 py-2 text-center">
                            <Mark correct={q.results[m.id]?.correct} kind={q.results[m.id]?.kind} />
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </main>

      {selected && (
        <>
          <div className="fixed inset-0 z-40 bg-black/25" onClick={() => setSelected(null)} aria-hidden="true" />
          <aside className="fixed inset-y-0 right-0 z-50 w-full max-w-[640px] border-l border-border bg-bg shadow-2xl">
            <Detail split={split} qid={selected} models={shownModels} onClose={() => setSelected(null)} />
          </aside>
        </>
      )}
    </div>
  );
}
