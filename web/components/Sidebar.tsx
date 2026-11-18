"use client";

import Link from "next/link";
import type { Conversation } from "@/lib/types";
import { ChatIcon, DatabaseIcon, PlusIcon, TableIcon, TrashIcon, XIcon } from "./icons";

export function Brand() {
  return (
    <div className="flex items-center gap-2.5">
      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent text-accent-text">
        <DatabaseIcon size={17} />
      </div>
      <div className="leading-tight">
        <div className="text-[15px] font-semibold">eth-nl2sql</div>
        <div className="text-[11px] text-muted">Ethereum · tháng 9/2026</div>
      </div>
    </div>
  );
}

export function Sidebar({
  active,
  conversations,
  currentId,
  open,
  onClose,
  onNew,
  onOpen,
  onDelete,
}: {
  active: "chat" | "benchmark";
  conversations?: Conversation[];
  currentId?: string | null;
  open: boolean;
  onClose: () => void;
  onNew?: () => void;
  onOpen?: (id: string) => void;
  onDelete?: (id: string) => void;
}) {
  const nav = (href: string, label: string, icon: React.ReactNode, on: boolean) => (
    <Link
      href={href}
      className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm ${on ? "bg-surface-3 font-medium text-text" : "text-muted hover:bg-surface-2 hover:text-text"}`}
    >
      {icon}
      {label}
    </Link>
  );
  return (
    <>
      {open && <div className="fixed inset-0 z-40 bg-black/30 md:hidden" onClick={onClose} aria-hidden="true" />}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-[272px] flex-col border-r border-border bg-surface transition-transform md:static md:z-auto md:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}
      >
        <div className="flex items-center justify-between px-4 pt-4 pb-3">
          <Brand />
          <button type="button" onClick={onClose} className="rounded-md p-1.5 text-muted hover:bg-surface-2 md:hidden">
            <XIcon size={16} />
          </button>
        </div>

        <div className="space-y-1 px-3">
          {onNew ? (
            <button
              type="button"
              onClick={onNew}
              className="flex w-full items-center gap-2.5 rounded-lg border border-border px-2.5 py-2 text-sm font-medium hover:bg-surface-2"
            >
              <PlusIcon size={16} /> Cuộc trò chuyện mới
            </button>
          ) : (
            nav("/", "Hỏi đáp", <ChatIcon size={16} />, active === "chat")
          )}
          {onNew && nav("/", "Hỏi đáp", <ChatIcon size={16} />, active === "chat")}
          {nav("/benchmark/", "Benchmark", <TableIcon size={16} />, active === "benchmark")}
        </div>

        {conversations && (
          <div className="scroll-thin mt-4 min-h-0 flex-1 overflow-y-auto px-3 pb-3">
            <div className="px-2.5 pb-1.5 text-[11px] font-semibold tracking-wide text-faint uppercase">Lịch sử</div>
            {conversations.length === 0 && <div className="px-2.5 text-xs text-faint">Chưa có câu hỏi nào.</div>}
            <ul className="space-y-0.5">
              {conversations.map((c) => (
                <li key={c.id} className="group relative">
                  <button
                    type="button"
                    onClick={() => onOpen?.(c.id)}
                    className={`w-full truncate rounded-lg py-2 pr-8 pl-2.5 text-left text-[13px] ${c.id === currentId ? "bg-surface-3 text-text" : "text-muted hover:bg-surface-2 hover:text-text"}`}
                    title={c.title}
                  >
                    {c.title}
                  </button>
                  <button
                    type="button"
                    onClick={() => onDelete?.(c.id)}
                    className="absolute top-1/2 right-1.5 hidden -translate-y-1/2 rounded p-1 text-faint group-hover:block hover:text-bad"
                    title="Xóa"
                  >
                    <TrashIcon size={13} />
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
        {!conversations && <div className="flex-1" />}

        <div className="border-t border-border px-4 py-3 text-[11px] leading-relaxed text-faint">
          Linker luật + Qwen2.5-Coder (llama.cpp, GTX 1650) + DuckDB. Mỗi câu hỏi độc lập, không nhớ ngữ cảnh.
        </div>
      </aside>
    </>
  );
}
