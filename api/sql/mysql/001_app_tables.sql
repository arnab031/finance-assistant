-- Application tables: chat state, and the query log everything else reads from.
--
-- The Postgres set built these across three files (001_app_tables then
-- 003_observability's ALTERs). MySQL has no ADD COLUMN IF NOT EXISTS, so a
-- re-runnable ALTER migration is not expressible; the final shape is declared
-- once here instead. chat_* doubles as the "sample questions and answers"
-- submission deliverable.
--
-- Type translations:
--   TEXT PRIMARY KEY -> VARCHAR(n). MySQL cannot index a TEXT without a prefix
--                       length, and a key needs a whole value.
--   TIMESTAMPTZ      -> DATETIME(6). MySQL has no timezone-aware type, so the
--                       offset has to be supplied by convention instead: the
--                       pool pins its session to +00:00 (api/db.py), making
--                       every value here UTC, and api/clock.py stamps that back
--                       on before one reaches a browser. The ops page reads
--                       these as wall-clock times, so the offset DOES matter -
--                       serving them bare rendered a 12:36 IST run as 07:06.
--   JSONB            -> JSON
--   TEXT[]           -> JSON array. Read with JSON_LENGTH / JSON_EXTRACT.
--   BIGSERIAL        -> BIGINT AUTO_INCREMENT

CREATE TABLE IF NOT EXISTS chat_threads (
    thread_id  VARCHAR(64) NOT NULL PRIMARY KEY,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    -- Sticky ambiguity choices. An expression default is the only form MySQL
    -- accepts for JSON.
    settled    JSON NOT NULL DEFAULT (JSON_OBJECT())
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id   VARCHAR(64) NOT NULL PRIMARY KEY,
    thread_id    VARCHAR(64) NOT NULL,
    seq          INT         NOT NULL,
    role         ENUM('user','assistant') NOT NULL,
    question     TEXT,
    spec         JSON,
    sql_text     TEXT,
    result       JSON,
    narration    TEXT,
    verified     BOOLEAN,
    confidence   VARCHAR(32),
    latency_ms   INT,
    query_log_id BIGINT,
    created_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_msg_thread_seq (thread_id, seq),
    KEY idx_msg_thread (thread_id, seq),
    CONSTRAINT fk_msg_thread FOREIGN KEY (thread_id)
        REFERENCES chat_threads(thread_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Every query. At hour 30 this is where the deck's latency and verification
-- numbers come from - measured, not asserted. The flags below each correspond
-- to a failure mode this build has actually produced, so a spike is
-- diagnosable rather than merely alarming.
CREATE TABLE IF NOT EXISTS query_log (
    id               BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
    thread_id        VARCHAR(64),
    question         TEXT        NOT NULL,
    spec             JSON,
    sql_text         TEXT,
    row_count        INT,
    sql_ms           INT,
    llm_extract_ms   INT,
    llm_narrate_ms   INT,
    input_tokens     INT,
    output_tokens    INT,
    model            VARCHAR(128),
    verified         BOOLEAN,
    clarified        BOOLEAN     NOT NULL DEFAULT FALSE,
    error            TEXT,
    repaired         BOOLEAN     NOT NULL DEFAULT FALSE,
    coerced          BOOLEAN     NOT NULL DEFAULT FALSE,
    sanity_corrected JSON        NOT NULL DEFAULT (JSON_ARRAY()),
    template_used    BOOLEAN     NOT NULL DEFAULT FALSE,
    unverified       JSON        NOT NULL DEFAULT (JSON_ARRAY()),
    ambiguity_kind   VARCHAR(64),
    intent           VARCHAR(32),
    resumed          BOOLEAN     NOT NULL DEFAULT FALSE,
    total_ms         INT,
    created_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    KEY idx_qlog_created  (created_at DESC),
    -- Postgres had these as partial indexes (WHERE verified IS NOT NULL).
    -- MySQL has no partial index, so they cover the whole column; on this
    -- volume the difference is not measurable.
    KEY idx_qlog_verified (verified),
    KEY idx_qlog_thread   (thread_id, created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
