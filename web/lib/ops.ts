/** Operations API client. Same direct-to-FastAPI approach as the chat. */
import { API } from "./api";

export type Signal = { value: number | null; threshold?: string; status: string };
export type Metrics = {
  ok: boolean; requests: number; answered: number; breaches: string[];
  signals: Record<string, Signal>;
  latency: { p50_ms: number | null; p95_ms: number | null; avg_tokens: number | null };
  recent_incidents: Incident[];
};
export type Incident = {
  id: number; issue: string; question: string;
  unverified: string[]; sanity_corrected: string[]; total_ms: number;
};
export type ModelScore = {
  model: string; run_id: string; started_at: string;
  n_total: number; n_passed: number; accuracy: number | null;
  duration_ms: number | null; p50_ms: number | null;
  numeric_passed: number; numeric_total: number;
  behaviour_passed: number; behaviour_total: number;
  spec_passed: number; spec_total: number;
};
export type RunSummary = {
  run_id: string; model: string; started_at: string;
  n_total: number; n_passed: number; duration_ms: number | null;
};
export type EvalResult = {
  question_id: string; question: string; grade: string; passed: boolean;
  expected: string; actual: string; detail: string; latency_ms: number;
};

async function get<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(`${API}${path}`, { cache: "no-store" });
    return r.ok ? ((await r.json()) as T) : null;
  } catch { return null; }
}

export const getMetrics = () => get<Metrics>("/api/metrics");
export const getScorecard = () =>
  get<{ models: ModelScore[]; history: RunSummary[] }>("/api/eval/scorecard");
export const getRun = (id: string) =>
  get<{ run: RunSummary; results: EvalResult[] }>(`/api/eval/runs/${id}`);
export const getReplay = (id: number) => get<any>(`/api/replay/${id}`);

/** Streams one verdict per question; 50 questions runs for minutes. */
export async function* runEval(
  body: { provider?: string; only?: string[]; notes?: string },
  signal?: AbortSignal,
) {
  const res = await fetch(`${API}/api/eval/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`eval failed (${res.status})`);
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    const frames = buf.split("\n\n");
    buf = frames.pop() ?? "";
    for (const f of frames) {
      const line = f.split("\n").find((l) => l.startsWith("data: "));
      if (line) { try { yield JSON.parse(line.slice(6)); } catch {} }
    }
  }
}
