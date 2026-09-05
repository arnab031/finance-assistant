-- Canary results.
--
-- Runs are kept so that "did my change regress anything?" and "which model is
-- better?" are both just queries. The model is recorded per run, which is what
-- makes the bake-off comparison possible at all.

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id      VARCHAR(64) NOT NULL PRIMARY KEY,
    started_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    finished_at DATETIME(6),
    model       VARCHAR(128) NOT NULL,
    n_total     INT NOT NULL DEFAULT 0,
    n_passed    INT NOT NULL DEFAULT 0,
    duration_ms INT,
    notes       TEXT,
    KEY idx_runs_time (started_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS eval_results (
    id          BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id      VARCHAR(64) NOT NULL,
    question_id VARCHAR(32) NOT NULL,
    question    TEXT        NOT NULL,
    grade       VARCHAR(32) NOT NULL,
    passed      BOOLEAN     NOT NULL,
    expected    TEXT,
    actual      TEXT,
    detail      TEXT,
    spec        JSON,
    latency_ms  INT,
    UNIQUE KEY uq_eval_run_q (run_id, question_id),
    KEY idx_eval_run (run_id),
    CONSTRAINT fk_eval_run FOREIGN KEY (run_id)
        REFERENCES eval_runs(run_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
