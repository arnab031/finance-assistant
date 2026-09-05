-- Organizers' schema in its native dialect.
--
-- Source: "TBX - Database Schema.md" (bank / account / transaction). That brief
-- was written for MySQL; api/sql/live/001_schema.sql is the PostgreSQL
-- translation of it. This file goes back to the original, so the DDL below is
-- close to what the organizers actually ship.
--
-- Differences from the Postgres file, and why:
--   pg_trgm GIN index   -> FULLTEXT. MySQL has no trigram similarity, so
--                          "how much did we pay Reliance" becomes a FULLTEXT
--                          or LIKE search. See MYSQL_PORT.md - this is the one
--                          capability that genuinely degrades.
--   TEXT + CHECK        -> ENUM, which is what the brief itself used
--   NUMERIC(15,2)       -> DECIMAL(15,2), the same exact type under a MySQL name
--   TIMESTAMP(6)        -> DATETIME(6). MySQL's TIMESTAMP is UTC-converted and
--                          ends in 2038; a business timestamp wants neither.
--   ::date cast         -> CAST(... AS DATE)
--   || concatenation    -> CONCAT()
--
-- Unlike the Postgres set this is ONE file rather than six. The Postgres
-- migrations grew a column at a time against a live database; here the database
-- is created fresh, and MySQL has no ADD COLUMN IF NOT EXISTS to make repeated
-- ALTERs idempotent. So the final shape is declared once, counterparty and all.

-- CREATE ... IF NOT EXISTS, never DROP: api/db.py runs every migration on every
-- boot, so a DROP here would empty the database each time the server started.
-- To rebuild from scratch, drop the volume - see MYSQL_PORT.md.

CREATE TABLE IF NOT EXISTS `bank` (
    bank_code  VARCHAR(10)  NOT NULL PRIMARY KEY,
    bank_name  VARCHAR(150) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `account` (
    account_id         VARCHAR(36)   NOT NULL PRIMARY KEY,
    entity_id          VARCHAR(36)   NOT NULL,
    -- SENSITIVE. Stored as received; encrypted only at the LLM boundary
    -- (api/crypto.py), never at rest, so ordinary SQL keeps working.
    account_number     VARCHAR(20)   NOT NULL,
    program_id         INT           NOT NULL,
    available_balance  DECIMAL(15,2) NOT NULL DEFAULT 0.00,
    bank_code          VARCHAR(10)   NOT NULL,
    CONSTRAINT fk_account_bank FOREIGN KEY (bank_code) REFERENCES `bank`(bank_code),
    KEY idx_acct_bank    (bank_code),
    KEY idx_acct_entity  (entity_id),
    KEY idx_acct_program (program_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `transaction` (
    transaction_id           VARCHAR(36)   NOT NULL PRIMARY KEY,
    account_id               VARCHAR(36)   NOT NULL,
    transaction_date         DATETIME(6)   NOT NULL,
    transaction_type         ENUM('credit','debit') NOT NULL,
    description              VARCHAR(500),
    transaction_amount       DECIMAL(15,2) NOT NULL DEFAULT 0.00,
    transaction_reference_id VARCHAR(64),            -- plaintext, searchable
    utr_number               VARCHAR(256),           -- SENSITIVE: arrives encrypted
    -- Derived from `description` by ensure_counterparty() in api/narration.py,
    -- which runs at boot before the server accepts traffic. This schema has
    -- no vendor table, so without this column "who did we pay?" has no answer:
    -- every merchant and payee name is buried in free-text narration.
    counterparty             VARCHAR(255),
    CONSTRAINT fk_txn_account FOREIGN KEY (account_id) REFERENCES `account`(account_id),
    KEY idx_txn_date         (transaction_date),
    KEY idx_txn_account      (account_id, transaction_date),
    KEY idx_txn_type         (transaction_type),
    KEY idx_txn_ref          (transaction_reference_id),
    KEY idx_txn_counterparty (counterparty),
    -- Stands in for the Postgres trigram indexes. FULLTEXT matches whole words,
    -- not substrings, so it will not find "RELIANCE" inside
    -- "RELIANCEDIGITAL" the way pg_trgm did. LIKE '%...%' still works and is
    -- what the compiler emits today; on 10 rows either is instant, and on a
    -- real export this is the index to revisit first.
    FULLTEXT KEY ft_txn_description  (description),
    FULLTEXT KEY ft_txn_counterparty (counterparty)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
