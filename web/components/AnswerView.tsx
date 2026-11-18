"use client";

import { useEffect, useRef, useState } from "react";
import { formatSeconds } from "@/lib/format";
import { DECISIONS, EX_KINDS } from "@/lib/labels";
import type { Answer, ModelInfo, Turn } from "@/lib/types";
import {
  AlertIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  InfoIcon,
  RefreshIcon,
  Spinner,
  XIcon,
} from "./icons";
import { PhaseBadge } from "./ModelPicker";
import { PipelineSteps } from "./PipelineSteps";
import { ErrorCallout, ResultView } from "./ResultView";
import { SqlBlock } from "./SqlBlock";

const STAGE_TEXT: Record<string, string> = {
  loading_model: "Đang nạp model…",
  linking: "Đang linking…",
  generating: "Model đang viết SQL…",
  executing: "Đang chạy SQL trên DuckDB…",
  checking: "Đang so với gold…",
  finished: "Đang hoàn tất…",
};

function Elapsed({ answer }: { answer: Answer }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (answer.status !== "running") return;
    const t = setInterval(() => setNow(Date.now()), 200);
    return () => clearInterval(t);
  }, [answer.status]);
  const end = answer.status === "running" ? now : (answer.finishedAt ?? now);
  return <span className="tabular-nums">{formatSeconds((end - answer.startedAt) / 1000)}</span>;
}

function RetryMenu({
  models,
  current,
  onRetry,
  disabled,
}: {
  models: ModelInfo[];
  current: string;
  onRetry: (model: string) => void;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted hover:bg-surface-2 hover:text-text disabled:opacity-40"
      >
        <RefreshIcon size={13} /> Thử lại với…
        <ChevronDownIcon size={12} />
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-30 mb-1 w-64 rounded-xl border border-border bg-surface p-1 shadow-lg">
          {models.map((m) => (
            <button
              key={m.id}
              type="button"
              disabled={m.missing.length > 0}
              onClick={() => {
                setOpen(false);
                onRetry(m.id);
              }}
              className="flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-sm hover:bg-surface-2 disabled:opacity-40"
            >
              <span className="flex items-center gap-2">
                {m.label}
                {m.id === current && <span className="text-xs text-faint">(lại)</span>}
              </span>
              <PhaseBadge phase={m.phase} />
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function AnswerBody({ answer, models }: { answer: Answer; models: Record<string, ModelInfo> }) {
  const a = answer;
  const running = a.status === "running";
  const [open, setOpen] = useState(running);
  // open while the steps come in; closed once the answer is complete, so the result is in view
  // (unless the reader opened or closed the panel themselves)
  const touched = useRef(false);
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running && !touched.current) setOpen(false);
    wasRunning.current = running;
  }, [running]);

  const streaming = a.attempts[a.attempts.length - 1];
  return (
    <div className="space-y-3">
      {a.retried && (
        <div className="flex items-center gap-2 rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">
          <AlertIcon size={14} /> {a.retried}
        </div>
      )}
      {a.stage === "loading_model" && running && a.loading && (
        <div className="flex items-start gap-2.5 rounded-lg border border-border bg-surface-2 px-3 py-2.5 text-sm">
          <Spinner className="mt-0.5 text-accent" />
          <div>
            <div className="font-medium">Đang nạp {a.loading.label}</div>
            <div className="text-xs text-muted">
              Khởi động llama-server trên GPU
              {a.loading.cached ? ", rồi khôi phục prefix đã lưu (khoảng 20 s)." : " và tính prefix lần đầu (khoảng 30–60 s)."}
            </div>
          </div>
        </div>
      )}

      {a.error && <ErrorCallout kind="Lỗi" message={a.error} />}
      {a.status === "cancelled" && (
        <div className="flex items-center gap-2 text-sm text-muted">
          <InfoIcon size={14} /> Đã dừng.
        </div>
      )}

      {a.abstain && (
        <div className="rounded-xl border border-border bg-surface px-4 py-3">
          <div className="flex items-center gap-2 font-medium">
            <InfoIcon size={16} className="text-accent" />
            {DECISIONS[a.abstain.decision]?.title ?? a.abstain.decision}
          </div>
          <p className="mt-1 text-sm text-muted">{DECISIONS[a.abstain.decision]?.body}</p>
          <p className="mt-2 font-mono text-xs text-faint">{a.abstain.answer}</p>
        </div>
      )}

      {running && !a.result && !a.abstain && a.stage !== "loading_model" && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm text-muted">
            <Spinner className="text-accent" />
            <span className="pulse-soft">{STAGE_TEXT[a.stage]}</span>
          </div>
          {streaming && !open && (
            <pre className="caret scroll-thin max-h-48 overflow-auto rounded-lg border border-border bg-code px-3 py-2 font-mono text-[12px] whitespace-pre-wrap">
              {streaming.text}
            </pre>
          )}
        </div>
      )}

      {a.sql && <SqlBlock sql={a.sql} title={`SQL do ${models[a.model]?.label ?? a.model} sinh`} />}

      {a.result && a.result.ok && (
        <>
          {a.result.final_error_kind === "missing_placeholder" && (
            <div className="flex items-center gap-2 text-xs text-warn">
              <AlertIcon size={13} /> SQL không dùng placeholder mà evidence đưa ra (model tự viết địa chỉ).
            </div>
          )}
          <ResultView result={a.result} />
        </>
      )}
      {a.result && !a.result.ok && <ResultView result={a.result} />}

      {a.gold && (
        <div
          className={`flex flex-wrap items-center gap-2 rounded-lg px-3 py-2 text-sm ${a.gold.correct ? "bg-ok-soft text-ok" : "bg-bad-soft text-bad"}`}
        >
          {a.gold.correct ? <CheckIcon size={15} /> : <XIcon size={15} />}
          <span className="font-medium">
            {a.gold.correct ? "Khớp kết quả gold" : `Không khớp gold: ${EX_KINDS[a.gold.kind] ?? a.gold.kind}`}
          </span>
          <span className="text-xs opacity-80">
            (câu {a.gold.question_id} trong tập {a.gold.split})
          </span>
        </div>
      )}

      <div className="rounded-xl border border-border bg-surface-2/50">
        <button
          type="button"
          onClick={() => {
            touched.current = true;
            setOpen((o) => !o);
          }}
          className="flex w-full items-center justify-between gap-2 px-3.5 py-2.5 text-left text-sm font-medium"
        >
          <span className="flex items-center gap-2">
            {open ? <ChevronDownIcon size={15} /> : <ChevronRightIcon size={15} />}
            Các bước pipeline
          </span>
          <span className="text-xs font-normal text-muted">
            linking → prompt → model → DuckDB{a.gold ? " → gold" : ""}
          </span>
        </button>
        {open && (
          <div className="border-t border-border px-3.5 pt-4">
            <PipelineSteps answer={a} models={models} />
          </div>
        )}
      </div>
    </div>
  );
}

export function TurnView({
  turn,
  models,
  modelList,
  busy,
  onRetry,
  onSelectAnswer,
}: {
  turn: Turn;
  models: Record<string, ModelInfo>;
  modelList: ModelInfo[];
  busy: boolean;
  onRetry: (model: string) => void;
  onSelectAnswer: (index: number) => void;
}) {
  const answer = turn.answers[turn.active];
  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-accent-soft px-4 py-2.5 text-[15px] leading-relaxed">
          {turn.question}
        </div>
      </div>

      <div className="flex gap-3">
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-accent text-[11px] font-bold text-accent-text">
          SQL
        </div>
        <div className="min-w-0 flex-1 space-y-3">
          {turn.answers.length > 1 && (
            <div className="flex flex-wrap gap-1 border-b border-border">
              {turn.answers.map((x, i) => (
                <button
                  key={x.id}
                  type="button"
                  onClick={() => onSelectAnswer(i)}
                  className={`-mb-px flex items-center gap-1.5 border-b-2 px-2.5 py-1.5 text-xs font-medium ${i === turn.active ? "border-accent text-text" : "border-transparent text-muted hover:text-text"}`}
                >
                  {x.status === "running" ? (
                    <Spinner size={11} />
                  ) : x.gold ? (
                    x.gold.correct ? (
                      <CheckIcon size={12} className="text-ok" />
                    ) : (
                      <XIcon size={12} className="text-bad" />
                    )
                  ) : null}
                  {models[x.model]?.label ?? x.model}
                </button>
              ))}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            <span className="font-medium text-text">{models[answer.model]?.label ?? answer.model}</span>
            {models[answer.model] && <PhaseBadge phase={models[answer.model].phase} />}
            <span>·</span>
            <Elapsed answer={answer} />
          </div>

          <AnswerBody key={answer.id} answer={answer} models={models} />

          <div className="flex items-center gap-1">
            <RetryMenu models={modelList} current={answer.model} onRetry={onRetry} disabled={busy} />
          </div>
        </div>
      </div>
    </div>
  );
}
