-- Canary results.
--
-- Runs are kept so that "did my change regress anything?" and "which model is
-- better?" are both just queries. The model is recorded per run, which is what
-- makes the bake-off comparison possible at all.

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    model       TEXT NOT NULL,
    n_total     INTEGER NOT NULL DEFAULT 0,
    n_passed    INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS eval_results (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES eval_runs(run_id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    question    TEXT NOT NULL,
    grade       TEXT NOT NULL,
    passed      BOOLEAN NOT NULL,
    expected    TEXT,
    actual      TEXT,
    detail      TEXT,
    spec        JSONB,
    latency_ms  INTEGER,
    UNIQUE (run_id, question_id)
);

CREATE INDEX IF NOT EXISTS idx_eval_run    ON eval_results (run_id);
CREATE INDEX IF NOT EXISTS idx_eval_failed ON eval_results (run_id) WHERE NOT passed;
CREATE INDEX IF NOT EXISTS idx_runs_time   ON eval_runs (started_at DESC);

-- One row per model: the bake-off table, straight from measurement.
CREATE OR REPLACE VIEW v_model_scorecard AS
WITH latest AS (
    SELECT DISTINCT ON (model) model, run_id, started_at, n_total, n_passed, duration_ms
    FROM eval_runs WHERE finished_at IS NOT NULL
    ORDER BY model, started_at DESC
)
SELECT l.model, l.run_id, l.started_at, l.n_total, l.n_passed,
       ROUND(l.n_passed::numeric / NULLIF(l.n_total, 0), 4) AS accuracy,
       l.duration_ms,
       (SELECT PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY latency_ms)
          FROM eval_results r WHERE r.run_id = l.run_id) AS p50_ms,
       (SELECT COUNT(*) FILTER (WHERE passed) FROM eval_results r
         WHERE r.run_id = l.run_id AND r.grade = 'numeric')   AS numeric_passed,
       (SELECT COUNT(*) FROM eval_results r
         WHERE r.run_id = l.run_id AND r.grade = 'numeric')   AS numeric_total,
       (SELECT COUNT(*) FILTER (WHERE passed) FROM eval_results r
         WHERE r.run_id = l.run_id AND r.grade = 'behaviour') AS behaviour_passed,
       (SELECT COUNT(*) FROM eval_results r
         WHERE r.run_id = l.run_id AND r.grade = 'behaviour') AS behaviour_total,
       (SELECT COUNT(*) FILTER (WHERE passed) FROM eval_results r
         WHERE r.run_id = l.run_id AND r.grade = 'spec')      AS spec_passed,
       (SELECT COUNT(*) FROM eval_results r
         WHERE r.run_id = l.run_id AND r.grade = 'spec')      AS spec_total
FROM latest l ORDER BY accuracy DESC NULLS LAST;
