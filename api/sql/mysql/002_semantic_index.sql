-- Entity resolution index. ONE auxiliary table - no vector columns on the fact
-- tables, so a dataset swap never touches this.
--
-- THE REAL LOSS IN THIS PORT.
-- Postgres held `embedding vector(768)` with an HNSW index via pgvector, so
-- "medical supplies" -> "Hospital: Clinic/Lab Supplies" was an approximate
-- nearest-neighbour lookup. MySQL 8.4 has no vector type at all (9.0 adds one,
-- still without an ANN index), so the embedding is stored as a JSON array and
-- there is nothing to search it with in SQL.
--
-- That is survivable here only because ENABLE_SEMANTIC is false and entity
-- resolution runs on trigram/FULLTEXT matching. If semantic resolution is ever
-- switched on against MySQL, the cosine similarity has to move into Python over
-- a loaded candidate set - fine at this vocabulary size (~9k labels), not fine
-- at a million. See MYSQL_PORT.md.

CREATE TABLE IF NOT EXISTS semantic_index (
    id          BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    entity_type VARCHAR(64)  NOT NULL,   -- 'counterparty', 'bank', ...
    entity_key  VARCHAR(255) NOT NULL,   -- the value the SQL compiler filters on
    label       VARCHAR(512) NOT NULL,   -- the text that was embedded
    aliases     JSON         NOT NULL DEFAULT (JSON_ARRAY()),
    embedding   JSON,                    -- 768 floats; no ANN index in MySQL
    UNIQUE KEY uq_sem_entity (entity_type, entity_key),
    KEY idx_sem_type (entity_type),
    FULLTEXT KEY ft_sem_label (label)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- What the index was last built FROM, so a rebuild is a decision rather than a
-- ritual. Without this the choice is between rebuilding every boot (minutes of
-- embedding calls for data that has not changed) and never rebuilding (an index
-- that silently goes stale when the real export lands).
--
-- The fingerprint is order-independent and bounded: COUNT plus a checksum plus
-- the extremes. GROUP_CONCAT over every key would be exact but truncates at
-- group_concat_max_len, which fails silently and in the worst direction - a
-- stable fingerprint for changed data.
CREATE TABLE IF NOT EXISTS semantic_index_meta (
    entity_type VARCHAR(64)  NOT NULL PRIMARY KEY,
    fingerprint VARCHAR(255) NOT NULL,
    embed_model VARCHAR(128) NOT NULL,
    embed_dim   INT          NOT NULL,
    n_labels    INT          NOT NULL DEFAULT 0,
    built_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    build_ms    INT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
