-- Application tables. Idempotent: safe to re-run.
-- chat_* doubles as the "sample questions and answers" submission deliverable.

CREATE TABLE IF NOT EXISTS chat_threads (
    thread_id  TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled    JSONB NOT NULL DEFAULT '{}'::jsonb   -- sticky ambiguity choices
);

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL REFERENCES chat_threads(thread_id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user','assistant')),
    question   TEXT,
    spec       JSONB,
    sql_text   TEXT,
    result     JSONB,
    narration  TEXT,
    verified   BOOLEAN,
    confidence TEXT,
    latency_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON chat_messages (thread_id, seq);

-- Every query. At hour 30 this is where the deck's latency and verification
-- numbers come from - measured, not asserted.
CREATE TABLE IF NOT EXISTS query_log (
    id             BIGSERIAL PRIMARY KEY,
    thread_id      TEXT,
    question       TEXT NOT NULL,
    spec           JSONB,
    sql_text       TEXT,
    row_count      INTEGER,
    sql_ms         INTEGER,
    llm_extract_ms INTEGER,
    llm_narrate_ms INTEGER,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    model          TEXT,
    verified       BOOLEAN,
    clarified      BOOLEAN NOT NULL DEFAULT false,
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_qlog_created ON query_log (created_at DESC);
