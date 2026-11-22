import { formatCount, formatDateTime, formatSeconds, shortHex } from "@/lib/format";
import { DECISIONS, ERROR_KINDS, EX_KINDS, MENTION_TYPES } from "@/lib/labels";
import type { Answer, GoldCheck, Linking, ModelInfo, PromptInfo } from "@/lib/types";
import { CheckIcon, Spinner, XIcon } from "./icons";
import { Legend, LinkedQuestion } from "./LinkedQuestion";
import { ResultView } from "./ResultView";
import { SqlBlock } from "./SqlBlock";

type StepState = "pending" | "active" | "done" | "error" | "skipped";

function Step({
  n,
  title,
  state,
  meta,
  last = false,
  children,
}: {
  n: number;
  title: string;
  state: StepState;
  meta?: React.ReactNode;
  last?: boolean;
  children?: React.ReactNode;
}) {
  const ring =
    state === "done"
      ? "border-ok bg-ok-soft text-ok"
      : state === "error"
        ? "border-bad bg-bad-soft text-bad"
        : state === "active"
          ? "border-accent bg-accent-soft text-accent"
          : "border-border-strong bg-surface text-faint";
  return (
    <li className="relative flex gap-3">
      {!last && <span className="absolute top-7 bottom-0 left-[13px] w-px bg-border" aria-hidden="true" />}
      <span
        className={`relative z-10 mt-0.5 flex h-[27px] w-[27px] shrink-0 items-center justify-center rounded-full border text-xs font-semibold ${ring}`}
      >
        {state === "active" ? <Spinner size={13} /> : state === "done" ? <CheckIcon size={13} /> : state === "error" ? <XIcon size={13} /> : n}
      </span>
      <div className="min-w-0 flex-1 pb-6">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3">
          <h4 className={`text-sm font-semibold ${state === "pending" || state === "skipped" ? "text-faint" : ""}`}>{title}</h4>
          {meta && <span className="text-xs text-muted">{meta}</span>}
        </div>
        {children && <div className="mt-2.5 space-y-3">{children}</div>}
      </div>
    </li>
  );
}

function Chip({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-md border border-border bg-surface-2 px-1.5 py-0.5 text-xs ${className}`}>
      {children}
    </span>
  );
}

function LinkingDetails({ linking }: { linking: Linking }) {
  const kinds = Array.from(
    new Set([
      ...linking.mentions.map((m) => m.type),
      ...(linking.properties.some((p) => p.span) ? ["property"] : []),
      ...(linking.time ? ["time"] : []),
      ...(linking.unsupported.length ? ["unsupported"] : []),
    ]),
  );
  return (
    <>
      <div className="rounded-lg border border-border bg-surface px-3.5 py-2.5">
        <LinkedQuestion linking={linking} />
        {kinds.length > 0 && (
          <div className="mt-2 border-t border-border pt-2">
            <Legend kinds={kinds} />
          </div>
        )}
      </div>

      {linking.mentions.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="text-left text-xs text-muted">
                <th className="py-1 pr-3 font-medium">Cụm từ</th>
                <th className="py-1 pr-3 font-medium">Loại</th>
                <th className="py-1 pr-3 font-medium">Liên kết tới</th>
                <th className="py-1 font-medium">Placeholder</th>
              </tr>
            </thead>
            <tbody>
              {linking.mentions.map((m, i) => {
                const ph = m.placeholder ? linking.placeholders[m.placeholder] : null;
                return (
                  <tr key={i} className="align-top">
                    <td className="py-1.5 pr-3">
                      <mark className={`span-mark span-${m.type}`}>{m.text}</mark>
                    </td>
                    <td className="py-1.5 pr-3 whitespace-nowrap text-muted">
                      {MENTION_TYPES[m.type] ?? m.type}
                      {m.top_n ? ` · top ${m.top_n}` : ""}
                    </td>
                    <td className="py-1.5 pr-3">
                      {m.type === "nil" ? (
                        <span className="text-bad">không có trong KB</span>
                      ) : (
                        <div>
                          <span className="font-medium">{m.label ?? (m.target ? shortHex(m.target, 10, 6) : "–")}</span>
                          {m.target && m.label && (
                            <span className="ml-1.5 font-mono text-xs text-faint" title={m.target}>
                              {m.target.startsWith("0x") ? shortHex(m.target, 8, 4) : m.target}
                            </span>
                          )}
                          {m.members && m.members.length > 0 && (
                            <div className="mt-1 text-xs text-muted">
                              Thành viên: {m.members.map((x) => x.label ?? x.id).join(", ")}
                            </div>
                          )}
                          {m.candidates.length > 1 && (
                            <details className="mt-1 text-xs">
                              <summary className="cursor-pointer text-muted hover:text-text">
                                {m.candidates.length} ứng viên
                              </summary>
                              <ul className="mt-1 space-y-0.5">
                                {m.candidates.map((c, j) => (
                                  <li key={j} className="flex gap-2 font-mono">
                                    <span className="w-10 text-right tabular-nums text-faint">{c.score.toFixed(2)}</span>
                                    <span>{c.type}</span>
                                    <span className="truncate text-muted" title={c.target}>
                                      {c.label ?? (c.target.startsWith("0x") ? shortHex(c.target) : c.target)}
                                    </span>
                                  </li>
                                ))}
                              </ul>
                            </details>
                          )}
                        </div>
                      )}
                    </td>
                    <td className="py-1.5 whitespace-nowrap">
                      {m.placeholder ? (
                        <span className="font-mono text-xs">
                          <span className="sql-placeholder">{`{${m.placeholder}}`}</span>
                          {ph && <span className="ml-1.5 text-muted">= {formatCount(ph.count)} địa chỉ</span>}
                        </span>
                      ) : (
                        <span className="text-faint">–</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex flex-wrap gap-1.5">
        {linking.properties.map((p, i) => (
          <Chip key={i}>
            <span className="font-mono text-[11.5px]" style={{ color: "var(--c-property)" }}>
              {p.sense_id}
            </span>
            {p.description && <span className="text-muted">· {p.description}</span>}
            {p.implicit && <span className="rounded bg-surface-3 px-1 text-[10px] text-muted">ngầm định</span>}
          </Chip>
        ))}
        {linking.time && (
          <Chip>
            <span style={{ color: "var(--c-time)" }}>Thời gian</span>
            <span className="font-mono text-[11.5px] text-muted">
              {formatDateTime(linking.time.start)} → {formatDateTime(linking.time.end)}
            </span>
          </Chip>
        )}
        {linking.unsupported.map((u, i) => (
          <Chip key={`u${i}`} className="border-bad/30 bg-bad-soft">
            <span className="font-mono text-[11.5px] text-bad">{u.sense_id}</span>
            {u.description && <span className="text-muted">· {u.description}</span>}
          </Chip>
        ))}
      </div>
      {linking.warnings.map((w) => (
        <p key={w} className="text-xs text-warn">
          {w.startsWith("open_address_set:")
            ? `Tập địa chỉ của ${w.split(":")[1]} là tập mở: kết quả là cận dưới.`
            : w}
        </p>
      ))}
    </>
  );
}

function PromptDetails({ prompt }: { prompt: PromptInfo }) {
  if (prompt.decision !== "sql") {
    const d = DECISIONS[prompt.decision];
    return (
      <p className="text-sm text-muted">
        {d?.title ?? prompt.decision}: không dựng prompt, không gọi model.
      </p>
    );
  }
  return (
    <>
      {prompt.tables && (
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-muted">Schema:</span>
          {prompt.tables.map((t) => (
            <Chip key={t}>
              <span className="font-mono">{t}</span>
            </Chip>
          ))}
        </div>
      )}
      <div className="rounded-lg border border-border bg-code px-3 py-2">
        <div className="mb-1 text-xs font-medium text-muted">Evidence</div>
        <ul className="space-y-0.5 font-mono text-[12px] leading-relaxed">
          {prompt.evidence.map((line, i) => (
            <li key={i} className="break-words">
              {line.split(/(\{E\d+\})/).map((part, j) =>
                /^\{E\d+\}$/.test(part) ? (
                  <span key={j} className="sql-placeholder">
                    {part}
                  </span>
                ) : (
                  part
                ),
              )}
            </li>
          ))}
        </ul>
      </div>
      {prompt.messages && prompt.messages.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted hover:text-text">
            Xem toàn bộ prompt ({formatCount(prompt.chars ?? 0)} ký tự, gồm system prompt và schema)
          </summary>
          <div className="mt-2 space-y-2">
            {prompt.messages.map((m, i) => (
              <div key={i} className="rounded-lg border border-border bg-code">
                <div className="border-b border-border px-3 py-1 font-mono text-[11px] text-muted uppercase">{m.role}</div>
                <pre className="scroll-thin max-h-72 overflow-auto px-3 py-2 font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap">
                  {m.content}
                </pre>
              </div>
            ))}
          </div>
        </details>
      )}
    </>
  );
}

export function GoldDetails({ gold, models }: { gold: GoldCheck; models: Record<string, ModelInfo> }) {
  return (
    <>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span
          className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-semibold ${gold.correct ? "bg-ok-soft text-ok" : "bg-bad-soft text-bad"}`}
        >
          {gold.correct ? <CheckIcon size={12} /> : <XIcon size={12} />}
          {EX_KINDS[gold.kind] ?? gold.kind}
        </span>
        <span className="text-xs text-muted">
          Câu {gold.question_id} của tập {gold.split}
          {gold.template_id && <> · template <span className="font-mono">{gold.template_id}</span></>}
        </span>
      </div>
      {Object.keys(gold.frozen).length > 0 && (
        <div className="flex flex-wrap gap-1.5 text-xs">
          <span className="text-muted">Lần chạy đã chốt:</span>
          {Object.entries(gold.frozen).map(([id, r]) => (
            <span
              key={id}
              className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 ${r.correct ? "border-ok/30 text-ok" : "border-bad/30 text-bad"}`}
              title={EX_KINDS[r.kind] ?? r.kind}
            >
              {r.correct ? <CheckIcon size={11} /> : <XIcon size={11} />}
              {models[id]?.label ?? id}
            </span>
          ))}
        </div>
      )}
      {gold.gold_sql && <SqlBlock sql={gold.gold_sql} title="SQL gold" maxHeight="12rem" />}
      {gold.gold_result && !gold.correct && (
        <div>
          <div className="mb-1.5 text-xs font-medium text-muted">Kết quả gold</div>
          <ResultView result={gold.gold_result} compact />
        </div>
      )}
    </>
  );
}

export function PipelineSteps({ answer, models }: { answer: Answer; models: Record<string, ModelInfo> }) {
  const a = answer;
  const running = a.status === "running";
  const failed = a.status === "error" || a.status === "cancelled";
  const asked = a.prompt?.decision === "sql";
  const label = models[a.model]?.label ?? a.model;

  const linkingState: StepState = a.linking ? "done" : running ? "active" : failed ? "error" : "pending";
  const promptState: StepState = a.prompt ? "done" : a.linking && running ? "active" : "pending";
  let genState: StepState = "pending";
  if (a.prompt && !asked) genState = "skipped";
  else if (a.attempts.length > 0) {
    if (a.attempts.every((x) => x.final)) genState = "done";
    else if (running) genState = a.stage === "generating" ? "active" : "done";
    else genState = failed ? "error" : "done";
  } else if (asked && running) genState = "active";
  const execState: StepState = !asked
    ? a.prompt
      ? "skipped"
      : "pending"
    : a.result
      ? a.result.ok
        ? "done"
        : "error"
      : a.stage === "executing" && running
        ? "active"
        : failed && a.attempts.length
          ? "error"
          : "pending";
  const genSeconds = a.attempts.reduce((s, x) => s + (x.final?.seconds ?? 0), 0);
  const tokens = a.attempts.reduce((s, x) => s + (x.final?.completion_tokens ?? 0), 0);

  return (
    <ol className="pt-1">
      <Step
        n={1}
        title="Entity linking (luật, không dùng LLM)"
        state={linkingState}
        meta={a.linking ? `${a.linking.lang === "vi" ? "Tiếng Việt" : "English"} · ${formatSeconds(a.linking.seconds)}` : undefined}
      >
        {a.linking && <LinkingDetails linking={a.linking} />}
      </Step>
      <Step n={2} title="Dựng prompt: schema và evidence" state={promptState}>
        {a.prompt && <PromptDetails prompt={a.prompt} />}
      </Step>
      <Step
        n={3}
        title={`Sinh SQL · ${label}`}
        state={genState}
        meta={genSeconds > 0 ? `${formatCount(tokens)} token · ${formatSeconds(genSeconds)}` : undefined}
      >
        {a.attempts.map((x) => (
          <div key={x.n} className="space-y-1.5">
            <div className="flex items-center gap-2 text-xs">
              <span className="font-medium">Lần {x.n}</span>
              {x.repair && <span className="rounded bg-warn-soft px-1.5 py-0.5 text-warn">sửa lỗi</span>}
              {x.final?.error_kind ? (
                <span className="text-bad">{ERROR_KINDS[x.final.error_kind] ?? x.final.error_kind}</span>
              ) : x.final ? (
                <span className="text-ok">chạy được</span>
              ) : null}
              {x.final && (
                <span className="text-faint">
                  {x.final.completion_tokens ?? "?"} token · {formatSeconds(x.final.seconds)}
                </span>
              )}
            </div>
            {x.repair && (
              <pre className="rounded-md border border-warn/30 bg-warn-soft px-2.5 py-1.5 font-mono text-[11.5px] whitespace-pre-wrap text-warn">
                {x.repair}
              </pre>
            )}
            <pre
              className={`scroll-thin max-h-64 overflow-auto rounded-lg border border-border bg-code px-3 py-2 font-mono text-[12px] leading-relaxed whitespace-pre-wrap ${running && !x.final && !x.executing ? "caret" : ""}`}
            >
              {x.text || (running ? "" : "(trống)")}
            </pre>
          </div>
        ))}
      </Step>
      <Step
        n={4}
        title="Thay placeholder và chạy trên DuckDB"
        state={execState}
        meta={a.result ? `${formatCount(a.result.row_count)} dòng · ${formatSeconds(a.result.seconds)}` : undefined}
        last={!a.gold}
      >
        {a.result?.sql_preview && (
          <SqlBlock sql={a.result.sql_preview} title="SQL đã thay địa chỉ (rút gọn)" maxHeight="12rem" />
        )}
        {a.result && !a.result.ok && <ResultView result={a.result} />}
      </Step>
      {a.gold && (
        <Step n={5} title="So với gold của benchmark" state={a.gold.correct ? "done" : "error"} last>
          <GoldDetails gold={a.gold} models={models} />
        </Step>
      )}
    </ol>
  );
}
