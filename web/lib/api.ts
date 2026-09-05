/**
 * Backend client. Talks to FastAPI directly - no Next.js proxy.
 *
 * Proxying SSE through a route handler is a well-known source of buffering
 * bugs; keeping the browser on a direct connection removes the category.
 */

import type { AskRequest, ClarifyRequest, Coverage, Event } from "./types";

export const API =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

/**
 * Stream one question. `EventSource` cannot POST, so this reads the body as a
 * ReadableStream and splits SSE frames by hand.
 */
async function* streamSSE(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<Event> {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok || !res.body) {
    throw new ApiError(res.status, `Request failed (${res.status})`);
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;

      // SSE frames are separated by a blank line. The final element is a
      // partial frame and stays in the buffer until its terminator arrives.
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";

      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        try {
          yield JSON.parse(line.slice(6)) as Event;
        } catch {
          // A malformed frame must not kill the stream.
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export function askStream(body: AskRequest, signal?: AbortSignal) {
  return streamSSE("/api/ask", body, signal);
}

export function clarifyStream(body: ClarifyRequest, signal?: AbortSignal) {
  return streamSSE("/api/clarify", body, signal);
}

export type Suggestions = {
  dataset: string; label: string; placeholder: string; suggestions: string[];
};

/** Starter chips come from the backend so they always match the loaded schema. */
export async function getSuggestions(): Promise<Suggestions | null> {
  try {
    const r = await fetch(`${API}/api/suggestions`, { cache: "no-store" });
    return r.ok ? ((await r.json()) as Suggestions) : null;
  } catch { return null; }
}

export async function getCoverage(): Promise<Coverage | null> {
  try {
    const res = await fetch(`${API}/api/coverage`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as Coverage;
  } catch {
    return null;
  }
}
