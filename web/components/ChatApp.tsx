"use client";

import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { applyEvent, newAnswer } from "@/lib/answer";
import { api, ApiError, ask } from "@/lib/api";
import { loadConversations, saveConversations } from "@/lib/history";
import type { Answer, Conversation, ExampleSummary, Turn } from "@/lib/types";
import { useServer } from "@/lib/useServer";
import { TurnView } from "./AnswerView";
import { Composer } from "./Composer";
import { EmptyState } from "./EmptyState";
import { AlertIcon, MenuIcon, XIcon } from "./icons";
import { ModelPicker } from "./ModelPicker";
import { Sidebar } from "./Sidebar";
import { StatusPill, ThemeToggle } from "./StatusPill";

export function ChatApp() {
  const params = useSearchParams();
  const { models, byId, defaultModel, status, offline, refreshStatus } = useServer();
  const [selected, setSelected] = useState("");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [samples, setSamples] = useState<ExampleSummary[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const hydrated = useRef(false);

  useEffect(() => {
    setConversations(loadConversations());
    hydrated.current = true;
  }, []);

  useEffect(() => {
    const q = params.get("q");
    const m = params.get("model");
    if (q) setInput(q);
    if (m) setSelected(m);
  }, [params]);

  useEffect(() => {
    // not on every streamed token: once the answer is complete
    if (hydrated.current && !running) saveConversations(conversations);
  }, [conversations, running]);

  useEffect(() => {
    if (!selected && defaultModel) setSelected(defaultModel);
  }, [selected, defaultModel]);

  useEffect(() => {
    api
      .samples()
      .then((r) => setSamples(r.samples))
      .catch(() => {});
  }, [offline]);

  const current = conversations.find((c) => c.id === currentId) ?? null;

  // follow the stream while the reader stays at the bottom
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [conversations, currentId]);

  const updateAnswer = useCallback(
    (convId: string, turnId: string, answerId: string, fn: (a: Answer) => Answer) => {
      setConversations((cs) =>
        cs.map((c) =>
          c.id !== convId
            ? c
            : {
                ...c,
                updatedAt: Date.now(),
                turns: c.turns.map((t) =>
                  t.id !== turnId ? t : { ...t, answers: t.answers.map((a) => (a.id === answerId ? fn(a) : a)) },
                ),
              },
        ),
      );
    },
    [],
  );

  const run = useCallback(
    async (convId: string, turnId: string, answer: Answer, question: string) => {
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      setRunning(true);
      setNotice(null);
      stick.current = true;
      const on = (event: string, data: Record<string, unknown>) =>
        updateAnswer(convId, turnId, answer.id, (a) => applyEvent(a, event, data));
      try {
        await ask(question, answer.model, on, ctrl.signal);
      } catch (e) {
        if (ctrl.signal.aborted) {
          on("cancelled", {});
        } else {
          const message = e instanceof ApiError || e instanceof Error ? e.message : String(e);
          on("error", { message });
        }
        on("done", {});
      } finally {
        abortRef.current = null;
        setRunning(false);
        refreshStatus();
      }
    },
    [updateAnswer, refreshStatus],
  );

  const send = (text?: string) => {
    const question = (text ?? input).trim();
    if (!question || running || !selected) return;
    const answer = newAnswer(selected);
    const turn: Turn = { id: crypto.randomUUID(), question, answers: [answer], active: 0 };
    let convId = currentId;
    if (!current || !convId) {
      convId = crypto.randomUUID();
      const conv: Conversation = { id: convId, title: question.slice(0, 90), updatedAt: Date.now(), turns: [turn] };
      setConversations((cs) => [conv, ...cs]);
      setCurrentId(convId);
    } else {
      const id = convId;
      setConversations((cs) => [
        ...cs.filter((c) => c.id === id).map((c) => ({ ...c, updatedAt: Date.now(), turns: [...c.turns, turn] })),
        ...cs.filter((c) => c.id !== id),
      ]);
    }
    setInput("");
    run(convId, turn.id, answer, question);
  };

  const retry = (turn: Turn, model: string) => {
    if (running || !currentId) return;
    const answer = newAnswer(model);
    const convId = currentId;
    setConversations((cs) =>
      cs.map((c) =>
        c.id !== convId
          ? c
          : {
              ...c,
              turns: c.turns.map((t) =>
                t.id !== turn.id ? t : { ...t, answers: [...t.answers, answer], active: t.answers.length },
              ),
            },
      ),
    );
    run(convId, turn.id, answer, turn.question);
  };

  const selectAnswer = (turnId: string, index: number) =>
    setConversations((cs) =>
      cs.map((c) =>
        c.id !== currentId ? c : { ...c, turns: c.turns.map((t) => (t.id === turnId ? { ...t, active: index } : t)) },
      ),
    );

  const stop = () => {
    abortRef.current?.abort();
    api.cancel().catch(() => {});
  };

  const selectModel = (id: string) => {
    setSelected(id);
    if (running || !status?.llama || status.llama.model === id) return;
    api
      .select(id)
      .then(() => refreshStatus())
      .catch((e: Error) => setNotice(e.message));
  };

  const label = byId[selected]?.label ?? selected;
  const gpuOff = status !== null && status.llama === null;

  return (
    <div className="flex h-dvh overflow-hidden">
      <Sidebar
        active="chat"
        conversations={[...conversations].sort((a, b) => b.updatedAt - a.updatedAt)}
        currentId={currentId}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onNew={() => {
          setCurrentId(null);
          setSidebarOpen(false);
        }}
        onOpen={(id) => {
          setCurrentId(id);
          setSidebarOpen(false);
        }}
        onDelete={(id) => {
          if (running && id === currentId) return;
          setConversations((cs) => cs.filter((c) => c.id !== id));
          if (id === currentId) setCurrentId(null);
        }}
      />
      <main className="flex min-w-0 flex-1 flex-col">
        <header className="relative z-30 flex items-center justify-between gap-2 border-b border-border bg-bg/80 px-2 py-2 backdrop-blur sm:px-4">
          <div className="flex min-w-0 items-center gap-1">
            <button
              type="button"
              onClick={() => setSidebarOpen(true)}
              className="rounded-lg p-2 text-muted hover:bg-surface-2 md:hidden"
              aria-label="Menu"
            >
              <MenuIcon size={18} />
            </button>
            <ModelPicker models={models} selected={selected} status={status} onSelect={selectModel} />
          </div>
          <div className="flex min-w-0 items-center gap-1">
            <StatusPill status={status} models={byId} offline={offline} />
            <ThemeToggle />
          </div>
        </header>

        <div
          ref={scroller}
          onScroll={(e) => {
            const el = e.currentTarget;
            stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
          }}
          className="scroll-thin min-h-0 flex-1 overflow-y-auto"
        >
          <div className="mx-auto w-full max-w-3xl space-y-10 px-4 py-6">
            {current ? (
              current.turns.map((t) => (
                <TurnView
                  key={t.id}
                  turn={t}
                  models={byId}
                  modelList={models}
                  busy={running || gpuOff}
                  onRetry={(m) => retry(t, m)}
                  onSelectAnswer={(i) => selectAnswer(t.id, i)}
                />
              ))
            ) : (
              <EmptyState samples={samples} onPick={(q) => send(q)} />
            )}
          </div>
        </div>

        <div className="mx-auto w-full max-w-3xl px-3 pb-3 sm:px-4 sm:pb-4">
          {(notice || gpuOff) && (
            <div className="mb-2 flex items-start gap-2 rounded-lg border border-warn/30 bg-warn-soft px-3 py-2 text-xs text-warn">
              <AlertIcon size={14} className="mt-px shrink-0" />
              <span className="flex-1">
                {notice ??
                  "Máy chủ đang chạy với --no-llama: không hỏi live được, chỉ xem lại kết quả ở trang Benchmark."}
              </span>
              {notice && (
                <button type="button" onClick={() => setNotice(null)} className="shrink-0">
                  <XIcon size={13} />
                </button>
              )}
            </div>
          )}
          <Composer
            value={input}
            onChange={setInput}
            onSend={() => send()}
            onStop={stop}
            running={running}
            disabled={offline || gpuOff || !selected}
            samples={samples}
            hint={
              <>
                Đang dùng <span className="font-medium text-muted">{label}</span>. Kết quả là SQL chạy thật trên DuckDB;
                mỗi câu hỏi độc lập.
              </>
            }
          />
        </div>
      </main>
    </div>
  );
}
