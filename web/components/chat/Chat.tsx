"use client";

/**
 * Chat container. Owns thread state and the SSE lifecycle.
 *
 * The reducer's action union IS the backend's SSE Event union, generated from
 * the pydantic models. Adding an event type server-side and regenerating
 * types.ts turns a missed case here into a TypeScript error rather than a
 * silent no-op.
 */

import { useCallback, useReducer, useRef, useState } from "react";
import type { Coverage, Event } from "@/lib/types";
import { askStream, clarifyStream } from "@/lib/api";
import MessageList from "./MessageList";
import Composer from "./Composer";

export type Stage = "understanding" | "checking" | "querying" | "explaining";

export type ClarifyPayload = Extract<Event, { type: "clarify" }>;
export type RowsPayload = Extract<Event, { type: "rows" }>;
export type SqlPayload = Extract<Event, { type: "sql" }>;
export type VerifiedPayload = Extract<Event, { type: "verified" }>;

export type Assistant = {
  id: string;
  role: "assistant";
  stage?: Stage;
  spec?: unknown;
  sql?: SqlPayload;
  rows?: RowsPayload;
  narration: string;
  verified?: VerifiedPayload;
  notes: { kind: string; text: string }[];
  clarify?: ClarifyPayload;
  clarifiedWith?: string;
  confidence?: "high" | "medium" | "low";
  error?: string;
  done: boolean;
};

export type Message = { id: string; role: "user"; text: string } | Assistant;

type Action =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string }
  | { kind: "event"; id: string; event: Event }
  | { kind: "fail"; id: string; message: string }
  | { kind: "resolved"; id: string; label: string };

function reduce(state: Message[], action: Action): Message[] {
  switch (action.kind) {
    case "user":
      return [...state, { id: action.id, role: "user", text: action.text }];
    case "assistant":
      return [
        ...state,
        { id: action.id, role: "assistant", narration: "", notes: [], done: false },
      ];
    case "fail":
      return state.map((m) =>
        m.id === action.id && m.role === "assistant"
          ? { ...m, error: action.message, done: true }
          : m,
      );
    case "resolved":
      return state.map((m) =>
        m.id === action.id && m.role === "assistant"
          ? { ...m, clarify: undefined, clarifiedWith: action.label }
          : m,
      );
    case "event":
      return state.map((m) => {
        if (m.id !== action.id || m.role !== "assistant") return m;
        const e = action.event;
        switch (e.type) {
          case "thread":
            // Server-issued thread id. Adopted by Chat so follow-ups and settled
            // ambiguity choices stay attached to this conversation.
            return m;
          case "stage":
            return { ...m, stage: e.stage as Stage };
          case "spec":
            return { ...m, spec: e.spec };
          case "sql":
            return { ...m, sql: e };
          case "rows":
            return { ...m, rows: e };
          case "token":
            return { ...m, narration: m.narration + e.text };
          case "verified":
            return { ...m, verified: e };
          case "note":
            return { ...m, notes: [...m.notes, { kind: e.kind, text: e.text }] };
          case "clarify":
            return { ...m, clarify: e };
          case "done":
            return { ...m, confidence: e.confidence, done: true, stage: undefined };
          case "error":
            return { ...m, error: e.message, done: true };
          default: {
            // Exhaustiveness guard: a new backend event fails the build here.
            const _never: never = e;
            return m;
          }
        }
      });
  }
}

let counter = 0;
const nextId = () => `m${++counter}`;

export default function Chat({
  coverage,
  suggestions,
}: {
  coverage: Coverage | null;
  suggestions: string[];
}) {
  const [messages, dispatch] = useReducer(reduce, []);
  const [busy, setBusy] = useState(false);
  const threadId = useRef(`t_${Math.random().toString(36).slice(2, 10)}`);
  const abort = useRef<AbortController | null>(null);

  const consume = useCallback(
    async (id: string, stream: AsyncGenerator<Event>) => {
      try {
        for await (const event of stream) dispatch({ kind: "event", id, event });
      } catch (err) {
        if ((err as Error)?.name === "AbortError") return;
        dispatch({
          kind: "fail",
          id,
          message:
            "Could not reach the backend. Is it running on port 8000?",
        });
      } finally {
        setBusy(false);
        abort.current = null;
      }
    },
    [],
  );

  const send = useCallback(
    (text: string) => {
      if (!text.trim() || busy) return;
      const answerId = nextId();
      dispatch({ kind: "user", id: nextId(), text });
      dispatch({ kind: "assistant", id: answerId });
      setBusy(true);
      abort.current = new AbortController();
      void consume(
        answerId,
        askStream({ question: text, thread_id: threadId.current }, abort.current.signal),
      );
    },
    [busy, consume],
  );

  const choose = useCallback(
    (messageId: string, ambiguityId: string, key: string, label: string) => {
      if (busy) return;
      dispatch({ kind: "resolved", id: messageId, label });
      setBusy(true);
      abort.current = new AbortController();
      void consume(
        messageId,
        clarifyStream(
          { thread_id: threadId.current, ambiguity_id: ambiguityId, chosen_key: key },
          abort.current.signal,
        ),
      );
    },
    [busy, consume],
  );

  const stop = useCallback(() => {
    abort.current?.abort();
    setBusy(false);
  }, []);

  return (
    <div className="chat">
      <MessageList
        messages={messages}
        coverage={coverage}
        suggestions={suggestions}
        onChoose={choose}
        onSuggest={send}
      />
      <Composer onSend={send} onStop={stop} busy={busy} />
    </div>
  );
}
