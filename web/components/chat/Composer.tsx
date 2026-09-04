"use client";

import { useState } from "react";

export default function Composer({
  onSend, onStop, busy,
}: { onSend: (t: string) => void; onStop: () => void; busy: boolean }) {
  const [text, setText] = useState("");

  return (
    <form
      className="composer"
      onSubmit={(e) => { e.preventDefault(); onSend(text); setText(""); }}
    >
      <input
        className="composer-input"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Ask about spend, vendors, or reconciliation…"
        aria-label="Ask a question about your financial data"
        autoFocus
      />
      {busy ? (
        <button type="button" className="btn btn-stop" onClick={onStop}>Stop</button>
      ) : (
        <button type="submit" className="btn btn-send" disabled={!text.trim()}>Ask</button>
      )}
    </form>
  );
}
