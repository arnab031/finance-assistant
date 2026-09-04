import Chat from "@/components/chat/Chat";
import { getCoverage } from "@/lib/api";

// Server component: coverage is fetched before first paint so the banner and
// starter questions are present immediately. Everything after this is client
// side, because a live SSE chat is not something to server-render.
export const dynamic = "force-dynamic";

const SUGGESTIONS = [
  "How much did we spend on vendor payouts last month?",
  "Which transactions are still unreconciled?",
  "Top 5 vendors by spend in the last 12 months",
  "Break down spend by category last month",
  "How much did we spend in FY2026?",
  "How much did we spend in December 2026?",
];

export default async function Page() {
  const coverage = await getCoverage();
  return (
    <main className="shell">
      <header className="header">
        <div className="brand">
          <span className="brand-mark" />
          <div>
            <h1 className="brand-title">Finance Assistant</h1>
            <p className="brand-sub">Grounded answers from your ledger</p>
          </div>
        </div>
        <p className="header-note">
          Computed in SQL · verified before display
        </p>
      </header>
      <Chat coverage={coverage} suggestions={SUGGESTIONS} />
    </main>
  );
}
