"use client";

import { useState } from "react";

export default function Composer({
  onSend, onStop, busy, placeholder, models, model, onModel,
}: {
  onSend: (t: string) => void; onStop: () => void; busy: boolean;
  placeholder?: string;
  models: string[]; model: string; onModel: (m: string) => void;
}) {
  const [text, setText] = useState("");

  return (
    <div className="composer-wrap">
      {/* Above the input rather than beside it. Which model answers is a
          setting, not part of the question, and given equal weight in the row
          it competed with the input for attention. Hidden entirely when there
          is one model - a select with a single option is furniture. */}
      {models.length > 1 && (
        <div className="composer-meta">
          <label className="model-select">
            <span>Answered by</span>
            <select value={model} disabled={busy}
                    onChange={(e) => onModel(e.target.value)}>
              {models.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
        </div>
      )}
      <form
        className="composer"
        onSubmit={(e) => { e.preventDefault(); onSend(text); setText(""); }}
      >
        <input
          className="composer-input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={placeholder ?? "Ask about your financial data…"}
          aria-label="Ask a question about your financial data"
          autoFocus
        />
        {busy ? (
          <button type="button" className="btn btn-stop" onClick={onStop}>Stop</button>
        ) : (
          <button type="submit" className="btn btn-send" disabled={!text.trim()}>Ask</button>
        )}
      </form>
    </div>
  );
}
