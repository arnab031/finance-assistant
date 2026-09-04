-- Observability columns.
--
-- The original query_log declared narrate_ms / tokens / verified but nothing
-- populated them: the pipeline computed the provenance verdict on every request
-- and threw it away. These columns close that gap and add the flags that make a
-- regression diagnosable without reading transcripts.

ALTER TABLE query_log
    ADD COLUMN IF NOT EXISTS repaired         BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS coerced          BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS sanity_corrected TEXT[]  NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS template_used    BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS unverified       TEXT[]  NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS ambiguity_kind   TEXT,
    ADD COLUMN IF NOT EXISTS intent           TEXT,
    ADD COLUMN IF NOT EXISTS resumed          BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS total_ms         INTEGER;

CREATE INDEX IF NOT EXISTS idx_qlog_verified ON query_log (verified)
    WHERE verified IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_qlog_thread   ON query_log (thread_id, created_at DESC);

-- A thread must always exist, so chat_messages can reference it and so the
-- ambiguity resolver has somewhere to persist settled choices. Clients that
-- omit one get a server-generated id back.
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS query_log_id BIGINT;
