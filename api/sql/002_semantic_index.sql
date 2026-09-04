-- Entity resolution index. ONE auxiliary table - no vector columns on the
-- fact tables, so tomorrow's dataset swap never touches this.
-- ~9,171 labels (not 1M rows): vendors, accounts, objects, categories,
-- departments, funds, programs.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS semantic_index (
    id          BIGSERIAL PRIMARY KEY,
    entity_type TEXT   NOT NULL,
    entity_key  TEXT   NOT NULL,     -- the value the SQL compiler filters on
    label       TEXT   NOT NULL,     -- the text that was embedded
    aliases     TEXT[] NOT NULL DEFAULT '{}',
    embedding   vector(768),         -- nomic-embed-text. 384 if switching to sbert.
    UNIQUE (entity_type, entity_key)
);

CREATE INDEX IF NOT EXISTS idx_sem_type ON semantic_index (entity_type);
CREATE INDEX IF NOT EXISTS idx_sem_trgm ON semantic_index USING gin (label gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sem_vec  ON semantic_index
    USING hnsw (embedding vector_cosine_ops);
