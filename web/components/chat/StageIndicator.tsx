import type { Stage } from "./Chat";

const STAGES: { key: Stage; label: string }[] = [
  { key: "understanding", label: "Understanding the question" },
  { key: "checking", label: "Checking coverage and ambiguity" },
  { key: "querying", label: "Querying Postgres" },
  { key: "explaining", label: "Writing the answer" },
];

export default function StageIndicator({ stage }: { stage: Stage }) {
  const idx = STAGES.findIndex((s) => s.key === stage);
  return (
    <div className="stage" role="status" aria-live="polite">
      <span className="stage-dot" />
      <span className="stage-label">{STAGES[idx]?.label ?? "Working"}</span>
      <span className="stage-steps">
        {STAGES.map((s, i) => (
          <span key={s.key} className={`stage-pip${i <= idx ? " on" : ""}`} />
        ))}
      </span>
    </div>
  );
}
