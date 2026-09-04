"use client";

import { useEffect, useRef } from "react";
import type { Coverage } from "@/lib/types";
import type { Message } from "./Chat";
import AnswerCard from "../answer/AnswerCard";
import CoverageBanner from "../coverage/CoverageBanner";

export default function MessageList({
  messages, coverage, suggestions, onChoose, onSuggest,
}: {
  messages: Message[];
  coverage: Coverage | null;
  suggestions: string[];
  onChoose: (m: string, a: string, k: string, l: string) => void;
  onSuggest: (q: string) => void;
}) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const busy = messages.some((m) => m.role === "assistant" && !m.done);

  return (
    <div className="messages">
      <CoverageBanner coverage={coverage} />

      {messages.length === 0 && (
        <div className="empty">
          <h2 className="empty-title">Ask about your financial data</h2>
          <p className="empty-sub">
            Every answer is computed by SQL against Postgres and checked against
            the result before you see it.
          </p>
          <div className="suggestions">
            {suggestions.map((s) => (
              <button key={s} className="suggestion" onClick={() => onSuggest(s)}>
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {messages.map((m) =>
        m.role === "user" ? (
          <div key={m.id} className="msg msg-user"><p>{m.text}</p></div>
        ) : (
          <div key={m.id} className="msg msg-assistant">
            <AnswerCard msg={m} onChoose={onChoose} busy={busy} />
          </div>
        ),
      )}
      <div ref={end} />
    </div>
  );
}
