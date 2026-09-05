"use client";

import type { Assistant } from "../chat/Chat";
import StageIndicator from "../chat/StageIndicator";
import BreakdownTable from "./BreakdownTable";
import ClarifyCard from "./ClarifyCard";
import ProvenancePanel from "./ProvenancePanel";
import VerifiedBadge from "./VerifiedBadge";

// The label is set by the pipeline from what it measured, not by the model.
// Saying which measurement keeps "medium" from reading as an unexplained shrug.
/** 940 -> "0.9s", 6395 -> "6.4s". Seconds throughout: the number people care
 *  about here is whole seconds of waiting, and "6395 ms" makes them count digits. */
export function seconds(ms: number): string {
  return ms >= 100 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

const CONFIDENCE_HINT: Record<string, string> = {
  high: "Every figure was traced back to a row in the query result.",
  medium: "Figures check out, but the answer fell back to a generated summary "
        + "rather than the model's own wording.",
  low: "Some figures could not be traced to the query result, so the answer was "
     + "rebuilt directly from the data.",
};

export default function AnswerCard({
  msg, onChoose, busy,
}: {
  msg: Assistant;
  onChoose: (messageId: string, ambiguityId: string, key: string, label: string) => void;
  busy: boolean;
}) {
  const coverageNotes = msg.notes.filter((n) => n.kind === "coverage");
  const assumptions = msg.notes.filter((n) => n.kind !== "coverage");

  // A conversational reply - a greeting, or "what can you do?" - has no rows,
  // no spec and no measurable time, so every child of the meta row hides itself
  // and the row rendered as 10px of empty space under the text. Ask whether
  // there is anything to show rather than whether an answer happened.
  const hasMeta = Boolean(
    msg.verified
    || assumptions.length > 0
    || (msg.timing && msg.timing.total > 0)
    || (msg.confidence && msg.confidence !== "high"),
  );

  return (
    <div className="answer">
      {msg.error && <p className="answer-error">{msg.error}</p>}

      {coverageNotes.map((n, i) => (
        <p key={i} className="note note-coverage">{n.text}</p>
      ))}

      {msg.clarify && (
        <ClarifyCard
          clarify={msg.clarify}
          disabled={busy}
          onChoose={(a, k, l) => onChoose(msg.id, a, k, l)}
        />
      )}

      {msg.clarifiedWith && (
        <p className="note note-chosen">Using: {msg.clarifiedWith}</p>
      )}

      {msg.narration && <p className="answer-text">{msg.narration}</p>}

      {hasMeta && (
        <div className="answer-meta">
          {msg.verified && <VerifiedBadge verified={msg.verified} />}
          {msg.timing && msg.timing.total > 0 && (
            <span
              className="answer-timing"
              title={`Understanding the question ${seconds(msg.timing.extract)}`
                   + ` · database ${seconds(msg.timing.sql)}`
                   + ` · writing the answer ${seconds(msg.timing.narrate)}`}
            >
              Answered in {seconds(msg.timing.total)}
            </span>
          )}
          {msg.confidence && msg.confidence !== "high" && (
            <span className={`badge badge-${msg.confidence}`}>
              {msg.confidence} confidence
            </span>
          )}
        </div>
      )}

      {assumptions.map((n, i) => (
        <p key={i} className="note note-assumption">{n.text}</p>
      ))}

      {/* Renders before narration arrives - real numbers on screen first. */}
      {msg.rows && <BreakdownTable rows={msg.rows} />}

      {msg.stage && !msg.done && <StageIndicator stage={msg.stage} />}

      <ProvenancePanel
        spec={msg.spec}
        sql={msg.sql}
        rowCount={msg.rows?.row_count}
        elapsedMs={msg.rows?.elapsed_ms}
        timing={msg.timing}
      />
    </div>
  );
}
