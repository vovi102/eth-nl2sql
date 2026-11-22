// Shapes of the API of `eth-nl2sql serve` (src/eth_nl2sql/api/).

export type Cell = string | number | boolean | null | Cell[] | { [key: string]: Cell };

export interface ExScore {
  ex: number;
  correct: number;
  questions: number;
}

export interface ModelInfo {
  id: string;
  label: string;
  description: string;
  size: string;
  phase: 1 | 2;
  base: string;
  adapter: string | null;
  missing: string[];
  ex: { dev: ExScore | null; test: ExScore | null };
  loaded: boolean;
}

export interface LlamaStatus {
  state: "stopped" | "loading" | "ready" | "error";
  model: string | null;
  error: string | null;
  log: string | null;
  prefix_tokens: number;
  prefix_source: "computed" | "restored" | null;
  load_seconds: number | null;
}

export interface Status {
  llama: LlamaStatus | null;
  busy: "loading" | "asking" | null;
  allow_test: boolean;
  splits: string[];
  default_model: string;
  warming: { model: string; done: number; total: number } | null;
}

export type Span = [number, number];

export interface Candidate {
  type: string;
  target: string;
  score: number;
  label: string | null;
}

export interface Mention {
  span: Span;
  text: string;
  type: string;
  target: string | null;
  placeholder: string | null;
  top_n: number | null;
  label: string | null;
  candidates: Candidate[];
  members?: { id: string; label: string | null; addresses: number }[];
}

export interface Property {
  sense_id: string;
  span: Span | null;
  text: string | null;
  implicit: boolean;
  description: string | null;
  kind: string | null;
}

export interface TimeRange {
  start: string;
  end: string;
  span: Span;
  text: string;
}

export interface Linking {
  question: string;
  lang: string;
  mentions: Mention[];
  properties: Property[];
  time: TimeRange | null;
  unsupported: { sense_id: string; span: Span | null; text: string | null; description: string | null }[];
  warnings: string[];
  placeholders: Record<string, { count: number; sample: string[] }>;
  seconds: number;
}

export interface Message {
  role: string;
  content: string;
}

export interface PromptInfo {
  decision: string;
  answer: string | null;
  tables: string[] | null;
  evidence: string[];
  messages?: Message[];
  chars: number | null;
}

export interface AttemptFinal {
  reply: string;
  sql: string | null;
  error_kind: string | null;
  error: string | null;
  seconds: number;
  prompt_tokens: number | null;
  cached_tokens: number | null;
  completion_tokens: number | null;
}

export interface ExecutionResult {
  ok: boolean;
  error_kind: string | null;
  error: string | null;
  sql: string | null;
  sql_preview: string | null;
  columns: string[];
  rows: Cell[][];
  row_count: number;
  rows_shown: number;
  truncated: boolean;
  seconds: number;
  final_error_kind?: string | null;
}

export interface ExResult {
  kind: string;
  correct: boolean;
}

export interface GoldCheck {
  split: string;
  question_id: string;
  template_id: string | null;
  expected: string;
  kind: string;
  correct: boolean;
  gold_sql: string | null;
  gold_result: ExecutionResult | null;
  frozen: Record<string, ExResult>;
}

// ---- the page's own state of one answer, built from the events of POST /api/ask ----------

export interface LiveAttempt {
  n: number;
  repair: string | null;
  text: string;
  executing?: boolean;
  final?: AttemptFinal;
}

export type AnswerStatus = "running" | "done" | "error" | "cancelled";
export type Stage = "loading_model" | "linking" | "generating" | "executing" | "checking" | "finished";

export interface Answer {
  id: string;
  model: string;
  status: AnswerStatus;
  stage: Stage;
  startedAt: number;
  finishedAt?: number;
  loading?: { label: string; cached: boolean };
  loaded?: { seconds: number | null; prefix: string | null };
  retried?: string;
  linking?: Linking;
  prompt?: PromptInfo;
  attempts: LiveAttempt[];
  sql?: string | null;
  result?: ExecutionResult;
  abstain?: { decision: string; answer: string };
  gold?: GoldCheck;
  error?: string;
  seconds?: number;
}

export interface Turn {
  id: string;
  question: string;
  answers: Answer[];
  active: number;
}

export interface Conversation {
  id: string;
  title: string;
  updatedAt: number;
  turns: Turn[];
}

// ---- the benchmark -----------------------------------------------------------------------

export interface ExampleSummary {
  question_id: string;
  question: string;
  lang: string | null;
  template_id: string | null;
  expected: string;
  results: Record<string, ExResult>;
}

export interface Prediction {
  question_id: string;
  decision: string;
  sql: string | null;
  attempts: AttemptFinal[];
  skipped?: string;
}

export interface GoldRecord {
  question_id: string;
  question: string;
  gold_sql?: string | null;
  template_id?: string;
  mentions: { span: Span; text: string; type: string; target: string; placeholder?: string }[];
}

export interface ExampleDetail extends ExampleSummary {
  gold: GoldRecord;
  linking: GoldRecord | null;
  predictions: Record<string, Prediction>;
}
