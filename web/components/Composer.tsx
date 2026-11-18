"use client";

import { useEffect, useRef, useState } from "react";
import type { ExampleSummary } from "@/lib/types";
import { SendIcon, SparkIcon, StopIcon } from "./icons";

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  running,
  disabled,
  hint,
  samples,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  running: boolean;
  disabled: boolean;
  hint: React.ReactNode;
  samples: ExampleSummary[];
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [showSamples, setShowSamples] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [value]);

  useEffect(() => {
    if (!showSamples) return;
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setShowSamples(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [showSamples]);

  const canSend = value.trim().length > 0 && !running && !disabled;
  return (
    <div ref={box} className="relative">
      {showSamples && samples.length > 0 && (
        <div className="scroll-thin absolute bottom-full left-0 z-30 mb-2 max-h-80 w-full overflow-y-auto rounded-2xl border border-border bg-surface p-1.5 shadow-xl">
          <div className="px-2.5 pt-1 pb-1.5 text-xs text-muted">Câu mẫu từ tập dev (mỗi template một câu mỗi ngôn ngữ)</div>
          {samples.map((s) => (
            <button
              key={s.question_id}
              type="button"
              onClick={() => {
                onChange(s.question);
                setShowSamples(false);
                ref.current?.focus();
              }}
              className="flex w-full items-baseline gap-2 rounded-lg px-2.5 py-1.5 text-left text-sm hover:bg-surface-2"
            >
              <span className="w-6 shrink-0 text-[10px] font-semibold text-faint uppercase">{s.lang}</span>
              <span className="min-w-0 flex-1">{s.question}</span>
            </button>
          ))}
        </div>
      )}
      <div className="rounded-2xl border border-border-strong bg-surface shadow-sm focus-within:border-accent">
        <textarea
          ref={ref}
          rows={1}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              if (canSend) onSend();
            }
          }}
          placeholder="Hỏi về giao dịch, token, hợp đồng… ví dụ: Uniswap nhận bao nhiêu ETH ngày 5/9?"
          className="block w-full resize-none bg-transparent px-4 pt-3.5 pb-1 text-[15px] leading-relaxed outline-none placeholder:text-faint"
        />
        <div className="flex items-center justify-between gap-2 px-2.5 pb-2.5">
          <button
            type="button"
            onClick={() => setShowSamples((s) => !s)}
            className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-muted hover:bg-surface-2 hover:text-text"
          >
            <SparkIcon size={14} /> Câu mẫu
          </button>
          {running ? (
            <button
              type="button"
              onClick={onStop}
              className="flex h-9 w-9 items-center justify-center rounded-full bg-text text-bg hover:opacity-85"
              title="Dừng"
            >
              <StopIcon size={14} />
            </button>
          ) : (
            <button
              type="button"
              onClick={onSend}
              disabled={!canSend}
              className="flex h-9 w-9 items-center justify-center rounded-full bg-accent text-accent-text hover:bg-accent-strong disabled:bg-surface-3 disabled:text-faint"
              title="Gửi (Enter)"
            >
              <SendIcon size={17} />
            </button>
          )}
        </div>
      </div>
      <div className="mt-1.5 px-1 text-center text-[11px] text-faint">{hint}</div>
    </div>
  );
}
