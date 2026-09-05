"use client";

/**
 * Operations page. Four questions, in priority order:
 *
 *   1. Is the model good enough, or do we switch?   -> scorecard
 *   2. Did my last change break something?          -> canary + history
 *   3. What is broken right now?                    -> incidents
 *   4. Is it fast enough to demo?                   -> latency
 *
 * Anything that does not answer one of those does not belong here.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import Logo from "@/components/Logo";
import { formatInstant } from "@/lib/format";
import {
  EvalResult, Incident, Metrics, ModelScore, RunSummary,
  getMetrics, getModels, getReplay, getRun, getScorecard, runEval,
} from "@/lib/ops";

type Live = { done: number; total: number; passed: number; model?: string;
              recent: EvalResult[]; running: boolean; error?: string };

const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(0)}%`;
const ms = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`;

const SIGNAL_LABELS: Record<string, string> = {
  verification_pass_rate: "Figures traceable",
  template_fallback_rate: "Narration fallback",
  repair_rate: "Spec repaired",
  coercion_rate: "Spec coerced",
  sanity_correction_rate: "Prompt ignored",
  clarify_rate: "Asked user",
  empty_result_rate: "Empty results",
  high_confidence_rate: "High confidence",
  low_confidence_rate: "Low confidence",
  error_rate: "Errors",
  p95_ms: "p95 latency",
};

/**
 * The tiles render whatever THRESHOLDS in api/main.py defines, but the labels
 * above are hand-written, so the two lists drift the moment a signal is added
 * server-side - which is how "high_confidence_rate" and "low_confidence_rate"
 * ended up printed raw and spilling out of their tiles.
 *
 * A signal with no entry now gets a readable name rather than its key. The
 * words also give the text somewhere to wrap; an unbroken 19-character key had
 * nothing to break on and simply overflowed a 112px tile.
 */
function signalLabel(key: string): string {
  const known = SIGNAL_LABELS[key];
  if (known) return known;
  const words = key.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export default function Ops() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [models, setModels] = useState<ModelScore[]>([]);
  const [history, setHistory] = useState<RunSummary[]>([]);
  const [failures, setFailures] = useState<EvalResult[]>([]);
  const [live, setLive] = useState<Live>({ done: 0, total: 0, passed: 0, recent: [], running: false });
  const [replay, setReplay] = useState<Record<number, any>>({});
  // Which model the next run targets. Empty until /api/models answers, so the
  // picker never shows a name the daemon cannot actually load.
  const [available, setAvailable] = useState<string[]>([]);
  const [chosen, setChosen] = useState<string>("");
  // Declared in EVAL_MODELS but not pulled. Named rather than dropped, so a
  // typo in .env reads as "pull this" instead of a model that never appears.
  const [missing, setMissing] = useState<string[]>([]);
  const abort = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    const [m, s] = await Promise.all([getMetrics(), getScorecard()]);
    setMetrics(m);
    setModels(s?.models ?? []);
    setHistory(s?.history ?? []);
    const newest = s?.history?.[0];
    if (newest) {
      const detail = await getRun(newest.run_id);
      setFailures((detail?.results ?? []).filter((r) => !r.passed));
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    void (async () => {
      const m = await getModels();
      if (!m) return;
      setAvailable(m.models);
      setChosen(m.default);
      setMissing(m.missing ?? []);
    })();
  }, []);

  const start = useCallback(async (provider?: string) => {
    abort.current = new AbortController();
    setLive({ done: 0, total: 50, passed: 0, recent: [], running: true });
    try {
      for await (const ev of runEval(
        { provider, model: chosen || undefined, notes: "from /ops" },
        abort.current.signal,
      )) {
        if (ev.type === "start") {
          setLive((l) => ({ ...l, total: ev.total, model: ev.model }));
        } else if (ev.type === "result") {
          setLive((l) => ({
            ...l,
            done: l.done + 1,
            passed: l.passed + (ev.passed ? 1 : 0),
            recent: [ev as EvalResult, ...l.recent].slice(0, 8),
          }));
        } else if (ev.type === "error") {
          setLive((l) => ({ ...l, running: false, error: ev.message }));
        } else if (ev.type === "done") {
          setLive((l) => ({ ...l, running: false }));
          void refresh();
        }
      }
    } catch {
      setLive((l) => ({ ...l, running: false }));
    }
  }, [refresh, chosen]);

  const latest = history[0];
  const best = models[0];

  return (
    <div className="ops">
      <header className="ops-head">
        <div>
          <a className="back-link" href="/">
            <span aria-hidden="true">&larr;</span> Back to chat
          </a>
          <div className="ops-brand">
            <Logo size={22} />
            <h1>System health</h1>
          </div>
          <p className="ops-sub">Measured, not asserted. Ground truth is recomputed every run.</p>
        </div>
        <div className={`verdict ${metrics?.ok ? "ok" : "bad"}`}>
          <span className="dot" />
          {metrics
            ? metrics.ok ? "All signals green" : `${metrics.breaches.length} breach`
            : "Backend unreachable"}
        </div>
      </header>

      {/* 1 — canary */}
      <section className="panel">
        <div className="panel-head">
          <h2>Canary</h2>
          <div className="btn-row">
            {/* A second scorecard row is the whole point of the panel, and two
                local models make one without an API key. Disabled mid-run
                because the swap is process-wide - changing it under a running
                canary would mislabel the row being written. */}
            <label className="model-pick">
              <span className="sr-only">Model to run</span>
              <select value={chosen} disabled={live.running || !available.length}
                      onChange={(e) => setChosen(e.target.value)}>
                {available.length === 0 && <option value="">loading models…</option>}
                {available.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </label>
            <button className="btn" disabled={live.running} onClick={() => start()}>
              {live.running ? `Running ${live.done}/${live.total}…` : "Run now"}
            </button>
            {missing.length > 0 && (
              <span className="hint" title="Listed in EVAL_MODELS but not pulled">
                not pulled: {missing.join(", ")}
              </span>
            )}
            {/* Disabled until the Anthropic path is set up. Re-enable by:
                  1. ./.venv/bin/pip install anthropic
                  2. ANTHROPIC_API_KEY=sk-ant-... in .env
                  3. uncomment the button below
                It fills the second scorecard row, which is the measured
                model-choice comparison the brief asks for. */}
            {/*
            <button className="btn ghost" disabled={live.running}
                    onClick={() => start("anthropic")} title="Fills the comparison row">
              Run on Haiku
            </button>
            */}
            {live.running && (
              <button className="btn ghost" onClick={() => abort.current?.abort()}>Stop</button>
            )}
          </div>
        </div>

        {live.running || live.done > 0 ? (
          <>
            <div className="score">
              <span className="score-big">{live.passed}</span>
              <span className="score-of">/ {live.total}</span>
              <span className="score-model">{live.model}</span>
            </div>
            <div className="bar"><div className="bar-fill"
              style={{ width: `${live.total ? (live.done / live.total) * 100 : 0}%` }} /></div>
            {live.error && <p className="err">{live.error}</p>}
            <ul className="ticker">
              {live.recent.map((r) => (
                <li key={r.question_id} className={r.passed ? "ok" : "bad"}>
                  <span className="mono">{r.question_id}</span>
                  <span className="grade">{r.grade}</span>
                  <span className="q">{r.question}</span>
                  <span className="mono dim">{ms(r.latency_ms)}</span>
                </li>
              ))}
            </ul>
          </>
        ) : latest ? (
          <div className="score">
            <span className="score-big">{latest.n_passed}</span>
            <span className="score-of">/ {latest.n_total}</span>
            <span className="score-model">
              {latest.model} · {ms(latest.duration_ms)} ·{" "}
              {formatInstant(latest.started_at)}
            </span>
          </div>
        ) : (
          <p className="empty">No run yet. Press <strong>Run now</strong> — 50 questions, a few minutes.</p>
        )}
      </section>

      {/* 2 — the decision this page exists for */}
      <section className="panel">
        <div className="panel-head">
          <h2>Model scorecard</h2>
          <span className="hint">
            {models.length < 2
              ? "one model measured — the Haiku comparison needs the Anthropic client installed"
              : "the model-choice deliverable, from measurement"}
          </span>
        </div>
        {models.length === 0 ? (
          <p className="empty">Run the canary to populate this.</p>
        ) : (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Model</th><th className="n">Accuracy</th><th className="n">Numeric</th>
                  <th className="n">Behaviour</th><th className="n">Spec</th>
                  <th className="n">p50</th><th className="n">Run time</th>
                </tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.model} className={m.model === best?.model ? "lead" : ""}>
                    <td className="mono">{m.model}</td>
                    <td className="n strong">{pct(m.accuracy)}</td>
                    <td className="n">{m.numeric_passed}/{m.numeric_total}</td>
                    <td className="n">{m.behaviour_passed}/{m.behaviour_total}</td>
                    <td className="n">{m.spec_passed}/{m.spec_total}</td>
                    <td className="n">{ms(m.p50_ms)}</td>
                    <td className="n">{ms(m.duration_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* 3 — live signals */}
      <section className="panel">
        <div className="panel-head">
          <h2>Signals</h2>
          <span className="hint">
            rolling 24h · {metrics?.requests ?? 0} requests, {metrics?.answered ?? 0} answered
          </span>
        </div>
        <div className="sig-grid">
          {metrics && Object.entries(metrics.signals).map(([k, s]) => (
            <div key={k} className={`sig ${s.status === "BREACH" ? "bad" : s.status === "no_data" ? "none" : "ok"}`}>
              <div className="sig-val">
                {s.value === null ? "—"
                  : k === "p95_ms" ? ms(s.value)
                  : pct(s.value)}
              </div>
              <div className="sig-name">{signalLabel(k)}</div>
              <div className="sig-thr">{s.threshold ?? ""}</div>
            </div>
          ))}
        </div>
        {metrics?.latency && (
          <p className="hint" style={{ marginTop: 12 }}>
            p50 {ms(metrics.latency.p50_ms)} · p95 {ms(metrics.latency.p95_ms)} ·{" "}
            {metrics.latency.avg_tokens ?? "—"} tokens per answer
          </p>
        )}
      </section>

      {/* 4 — what to fix */}
      <section className="panel">
        <div className="panel-head">
          <h2>Failures &amp; incidents</h2>
          <span className="hint">{failures.length} canary failures · {metrics?.recent_incidents.length ?? 0} runtime incidents</span>
        </div>

        {failures.length === 0 && (metrics?.recent_incidents.length ?? 0) === 0 ? (
          <p className="empty">Nothing to look at.</p>
        ) : (
          <ul className="fails">
            {failures.map((f) => (
              <li key={f.question_id}>
                <div className="fail-head">
                  <span className="mono tag">{f.question_id}</span>
                  <span className="grade">{f.grade}</span>
                  <span className="q">{f.question}</span>
                </div>
                <div className="fail-body">
                  <span><em>expected</em> {f.expected || "—"}</span>
                  <span><em>got</em> {f.actual || "—"}</span>
                  {f.detail && <span className="detail">{f.detail}</span>}
                </div>
              </li>
            ))}
            {metrics?.recent_incidents.map((i: Incident) => (
              <li key={`inc-${i.id}`}>
                <div className="fail-head">
                  <span className="mono tag">#{i.id}</span>
                  <span className="grade">{i.issue}</span>
                  <span className="q">{i.question}</span>
                  <button className="btn tiny" onClick={async () => {
                            const r = await getReplay(i.id);
                            setReplay((prev) => ({ ...prev, [i.id]: r }));
                          }}>
                    Replay
                  </button>
                </div>
                {replay[i.id] && (
                  <div className="fail-body replay">
                    <span className="verdict-line">{replay[i.id].verdict}</span>
                    {Object.keys(replay[i.id].spec_diff ?? {}).length > 0 && (
                      <pre>{JSON.stringify(replay[i.id].spec_diff, null, 2)}</pre>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
