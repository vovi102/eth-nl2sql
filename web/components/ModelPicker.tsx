"use client";

import { useEffect, useRef, useState } from "react";
import { formatPercent } from "@/lib/format";
import type { ModelInfo, Status } from "@/lib/types";
import { CheckIcon, ChevronDownIcon, Spinner } from "./icons";

export function PhaseBadge({ phase }: { phase: 1 | 2 }) {
  return phase === 2 ? (
    <span className="rounded-md bg-accent-soft px-1.5 py-0.5 text-[10.5px] font-semibold tracking-wide whitespace-nowrap text-accent">
      PHASE 2 · LoRA
    </span>
  ) : (
    <span className="rounded-md bg-surface-3 px-1.5 py-0.5 text-[10.5px] font-semibold tracking-wide whitespace-nowrap text-muted">
      PHASE 1
    </span>
  );
}

type ModelState = "ready" | "loading" | "idle" | "missing" | "off";

export function modelState(m: ModelInfo, status: Status | null): ModelState {
  if (m.missing.length) return "missing";
  const llama = status?.llama;
  if (!llama) return "off";
  if (llama.model === m.id && llama.state === "ready") return "ready";
  if (llama.model === m.id && llama.state === "loading") return "loading";
  return "idle";
}

function StateNote({ state }: { state: ModelState }) {
  switch (state) {
    case "ready":
      return (
        <span className="inline-flex items-center gap-1.5 text-ok">
          <span className="dot" style={{ background: "var(--ok)" }} /> đang chạy
        </span>
      );
    case "loading":
      return (
        <span className="inline-flex items-center gap-1.5 text-accent">
          <Spinner size={11} /> đang nạp
        </span>
      );
    case "missing":
      return <span className="text-bad">thiếu file model</span>;
    case "off":
      return <span className="text-faint">GPU tắt</span>;
    default:
      return <span className="text-faint">nạp khi chọn (~20 s)</span>;
  }
}

export function ModelPicker({
  models,
  selected,
  status,
  onSelect,
}: {
  models: ModelInfo[];
  selected: string;
  status: Status | null;
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const current = models.find((m) => m.id === selected);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-[15px] font-semibold hover:bg-surface-2"
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="max-w-[38vw] truncate sm:max-w-none">{current?.label ?? "Chọn model"}</span>
        {current && (
          <span className="hidden sm:inline-flex">
            <PhaseBadge phase={current.phase} />
          </span>
        )}
        <ChevronDownIcon size={15} className="text-muted" />
      </button>
      {open && (
        <div
          role="listbox"
          className="absolute top-full left-0 z-40 mt-1 w-[min(92vw,380px)] rounded-2xl border border-border bg-surface p-1.5 shadow-xl"
        >
          <div className="px-2.5 pt-1.5 pb-2 text-xs text-muted">
            Cùng một pipeline, khác model sinh SQL. EX = độ chính xác thực thi trên benchmark.
          </div>
          {models.map((m) => {
            const state = modelState(m, status);
            return (
              <button
                key={m.id}
                type="button"
                role="option"
                aria-selected={m.id === selected}
                disabled={state === "missing"}
                onClick={() => {
                  setOpen(false);
                  onSelect(m.id);
                }}
                className="flex w-full items-start gap-3 rounded-xl px-2.5 py-2.5 text-left hover:bg-surface-2 disabled:opacity-50"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold">{m.label}</span>
                    <PhaseBadge phase={m.phase} />
                  </div>
                  <div className="mt-0.5 text-xs text-muted">{m.description}</div>
                  <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                    <span className="text-muted">
                      EX dev <span className="font-semibold text-text tabular-nums">{formatPercent(m.ex.dev?.ex)}</span>
                    </span>
                    <span className="text-muted">
                      EX test <span className="font-semibold text-text tabular-nums">{formatPercent(m.ex.test?.ex)}</span>
                    </span>
                    <StateNote state={state} />
                  </div>
                </div>
                <span className="mt-1 w-4 text-accent">{m.id === selected && <CheckIcon size={16} />}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
