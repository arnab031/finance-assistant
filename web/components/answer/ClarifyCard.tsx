"use client";

import type { ClarifyPayload } from "../chat/Chat";

/**
 * The clarification moment.
 *
 * The whole point is that each option carries its REAL computed number. The
 * user recognises which one they meant instantly, instead of parsing finance
 * jargon. Both figures were already computed by probe queries before this card
 * rendered, so picking one costs no extra model call.
 */
export default function ClarifyCard({
  clarify, onChoose, disabled,
}: {
  clarify: ClarifyPayload;
  onChoose: (ambiguityId: string, key: string, label: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="clarify">
      <div className="clarify-head">
        <span className="clarify-kind">{clarify.kind}</span>
        <p className="clarify-msg">{clarify.message}</p>
      </div>
      <div className="clarify-options">
        {clarify.options.map((o) => (
          <button
            key={o.key}
            className={`clarify-opt${o.key === clarify.default_key ? " is-default" : ""}`}
            onClick={() => onChoose(clarify.ambiguity_id, o.key, o.label)}
            disabled={disabled}
          >
            <span className="clarify-opt-label">{o.label}</span>
            <span className="clarify-opt-preview">{o.preview}</span>
            <span className="clarify-opt-detail">{o.detail}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
