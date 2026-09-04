import type { VerifiedPayload } from "../chat/Chat";

/**
 * Surfaces the numeric provenance check. Every figure in the narration was
 * matched against the query result before this rendered; an amber badge means
 * the model's wording was replaced with a summary built straight from the data.
 */
export default function VerifiedBadge({ verified }: { verified: VerifiedPayload }) {
  if (verified.ok) {
    return (
      <span className="badge badge-ok" title="Every number was matched to the query result">
        ✓ {verified.numbers_checked} figure{verified.numbers_checked === 1 ? "" : "s"} traced to source
      </span>
    );
  }
  return (
    <span className="badge badge-warn" title={(verified.unverified ?? []).join(", ")}>
      ⚠ Unverified figures removed
    </span>
  );
}
