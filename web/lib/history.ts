import type { Conversation } from "./types";

// Conversations live in this browser only (localStorage), a convenience for the demo.
const KEY = "eth-nl2sql.conversations.v1";
const MAX_CONVERSATIONS = 40;

export function loadConversations(): Conversation[] {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed = raw ? (JSON.parse(raw) as Conversation[]) : [];
    // An answer cut off by a reload cannot resume.
    return parsed.map((c) => ({
      ...c,
      turns: c.turns.map((t) => ({
        ...t,
        answers: t.answers.map((a) =>
          a.status === "running" ? { ...a, status: "cancelled" as const } : a,
        ),
      })),
    }));
  } catch {
    return [];
  }
}

/** Saved without the full prompt messages (the schema is ~15k characters each). */
export function saveConversations(conversations: Conversation[]): void {
  const slim = conversations
    .filter((c) => c.turns.length > 0)
    .slice(0, MAX_CONVERSATIONS)
    .map((c) => ({
      ...c,
      turns: c.turns.map((t) => ({
        ...t,
        answers: t.answers.map((a) =>
          a.prompt ? { ...a, prompt: { ...a.prompt, messages: undefined } } : a,
        ),
      })),
    }));
  try {
    localStorage.setItem(KEY, JSON.stringify(slim));
  } catch {
    // full or blocked storage: the history just is not kept
  }
}

export function loadTheme(): "light" | "dark" {
  if (typeof document === "undefined") return "light";
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

export function saveTheme(theme: "light" | "dark"): void {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("theme", theme);
  } catch {
    // ignore
  }
}
