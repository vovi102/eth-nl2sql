import type {
  Answer,
  AttemptFinal,
  ExecutionResult,
  GoldCheck,
  Linking,
  PromptInfo,
} from "./types";

export function newAnswer(model: string): Answer {
  return {
    id: crypto.randomUUID(),
    model,
    status: "running",
    stage: "linking",
    startedAt: Date.now(),
    attempts: [],
  };
}

/** One event of POST /api/ask applied to the answer (see src/eth_nl2sql/api/pipeline.py). */
export function applyEvent(a: Answer, event: string, data: Record<string, unknown>): Answer {
  switch (event) {
    case "start":
      return { ...a, stage: "linking" };
    case "model_loading":
      return {
        ...a,
        stage: "loading_model",
        loading: { label: String(data.label ?? data.model), cached: Boolean(data.cached) },
      };
    case "model_ready":
      return {
        ...a,
        stage: "linking",
        loaded: { seconds: (data.seconds as number) ?? null, prefix: (data.prefix as string) ?? null },
      };
    case "retry":
      // llama-server crashed and was restarted: the question starts again
      return { ...newAnswer(a.model), id: a.id, startedAt: a.startedAt, retried: String(data.message) };
    case "linking":
      return { ...a, linking: data as unknown as Linking };
    case "prompt": {
      const prompt = data as unknown as PromptInfo;
      return { ...a, prompt, stage: prompt.decision === "sql" ? "generating" : "finished" };
    }
    case "answer":
      return {
        ...a,
        abstain: { decision: String(data.decision), answer: String(data.answer) },
        stage: "finished",
      };
    case "attempt":
      return {
        ...a,
        stage: "generating",
        attempts: [...a.attempts, { n: Number(data.n), repair: (data.repair as string) ?? null, text: "" }],
      };
    case "token": {
      const attempts = a.attempts.slice();
      const last = attempts[attempts.length - 1];
      if (last) attempts[attempts.length - 1] = { ...last, text: last.text + String(data.text) };
      return { ...a, attempts };
    }
    case "executing": {
      const attempts = a.attempts.slice();
      const last = attempts[attempts.length - 1];
      if (last) attempts[attempts.length - 1] = { ...last, executing: true };
      return { ...a, attempts, stage: "executing" };
    }
    case "attempts": {
      const finals = data.attempts as AttemptFinal[];
      const attempts = a.attempts.map((x, i) => ({ ...x, executing: false, final: finals[i] }));
      return { ...a, attempts, sql: (data.sql as string) ?? null };
    }
    case "result":
      return { ...a, result: data as unknown as ExecutionResult, stage: "checking" };
    case "gold":
      return { ...a, gold: data as unknown as GoldCheck };
    case "timing":
      return { ...a, seconds: Number(data.seconds), stage: "finished" };
    case "error":
      return { ...a, status: "error", error: String(data.message), finishedAt: Date.now() };
    case "cancelled":
      return { ...a, status: "cancelled", finishedAt: Date.now() };
    case "done":
      return a.status === "running"
        ? { ...a, status: "done", stage: "finished", finishedAt: Date.now() }
        : { ...a, finishedAt: a.finishedAt ?? Date.now() };
    default:
      return a;
  }
}
