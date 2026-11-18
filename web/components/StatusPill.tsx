"use client";

import { useEffect, useState } from "react";
import { loadTheme, saveTheme } from "@/lib/history";
import type { ModelInfo, Status } from "@/lib/types";
import { MoonIcon, Spinner, SunIcon } from "./icons";

export function StatusPill({
  status,
  models,
  offline,
}: {
  status: Status | null;
  models: Record<string, ModelInfo>;
  offline: boolean;
}) {
  let tone = "text-muted";
  let dot = "var(--faint)";
  let text = "Đang kết nối…";
  let spin = false;
  let title: string | undefined;
  if (offline) {
    tone = "text-bad";
    dot = "var(--bad)";
    text = "Mất kết nối API";
  } else if (status) {
    const llama = status.llama;
    const name = (id: string | null | undefined) => (id ? (models[id]?.label ?? id) : "");
    if (!llama) {
      text = "Chỉ xem lại · không dùng GPU";
    } else if (status.warming) {
      spin = true;
      tone = "text-accent";
      text = `Chuẩn bị model ${status.warming.done + 1}/${status.warming.total}: ${name(status.warming.model)}`;
    } else if (llama.state === "loading") {
      spin = true;
      tone = "text-accent";
      text = `Đang nạp ${name(llama.model)}`;
    } else if (llama.state === "error") {
      tone = "text-bad";
      dot = "var(--bad)";
      text = "llama-server lỗi";
      title = llama.error ?? undefined;
    } else if (llama.state === "ready") {
      tone = "text-ok";
      dot = "var(--ok)";
      text = status.busy === "asking" ? `Đang trả lời · ${name(llama.model)}` : `Sẵn sàng · ${name(llama.model)}`;
    } else {
      text = "llama-server chưa chạy";
    }
  }
  return (
    <span
      className={`inline-flex max-w-[52vw] items-center gap-1.5 truncate rounded-full border border-border bg-surface px-2.5 py-1 text-xs ${tone}`}
      title={title ?? text}
    >
      {spin ? <Spinner size={11} /> : <span className="dot shrink-0" style={{ background: dot }} />}
      <span className="truncate">{text}</span>
    </span>
  );
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<"light" | "dark">("light");
  useEffect(() => setTheme(loadTheme()), []);
  return (
    <button
      type="button"
      onClick={() => {
        const next = theme === "dark" ? "light" : "dark";
        saveTheme(next);
        setTheme(next);
      }}
      className="rounded-lg p-2 text-muted hover:bg-surface-2 hover:text-text"
      title={theme === "dark" ? "Giao diện sáng" : "Giao diện tối"}
    >
      {theme === "dark" ? <SunIcon size={17} /> : <MoonIcon size={17} />}
    </button>
  );
}
