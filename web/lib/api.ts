import type {
  ExampleDetail,
  ExampleSummary,
  ExecutionResult,
  ModelInfo,
  Status,
} from "./types";

// Same origin when `eth-nl2sql serve` serves the built page; the API's URL in `npm run dev`.
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function detail(r: Response): Promise<string> {
  try {
    const body = await r.json();
    return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    return r.statusText || `HTTP ${r.status}`;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let r: Response;
  try {
    r = await fetch(API_URL + path, init);
  } catch {
    throw new ApiError(0, "Không kết nối được tới máy chủ API (eth-nl2sql serve).");
  }
  if (!r.ok) throw new ApiError(r.status, await detail(r));
  return r.json() as Promise<T>;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export const api = {
  status: () => request<Status>("/api/status"),
  models: () => request<{ models: ModelInfo[]; default: string }>("/api/models"),
  select: (model: string) => post<{ loading: boolean; status: Status }>("/api/models/select", { model }),
  samples: () => request<{ samples: ExampleSummary[] }>("/api/samples"),
  examples: (split: string) =>
    request<{ split: string; models: string[]; questions: ExampleSummary[] }>(
      `/api/examples?split=${encodeURIComponent(split)}`,
    ),
  example: (split: string, qid: string) =>
    request<ExampleDetail>(`/api/examples/${encodeURIComponent(split)}/${encodeURIComponent(qid)}`),
  runSql: (split: string, questionId: string, source: string) =>
    post<ExecutionResult>("/api/run-sql", { split, question_id: questionId, source }),
  cancel: () => post<{ cancelled: boolean }>("/api/cancel", {}),
};

/** POST /api/ask and hand each Server-Sent Event to `onEvent` until the stream ends. */
export async function ask(
  question: string,
  model: string,
  onEvent: (event: string, data: Record<string, unknown>) => void,
  signal: AbortSignal,
): Promise<void> {
  let r: Response;
  try {
    r = await fetch(API_URL + "/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, model }),
      signal,
    });
  } catch (e) {
    if (signal.aborted) throw e;
    throw new ApiError(0, "Không kết nối được tới máy chủ API (eth-nl2sql serve).");
  }
  if (!r.ok || !r.body) throw new ApiError(r.status, await detail(r));
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end: number;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) onEvent(event, JSON.parse(data));
    }
  }
}
