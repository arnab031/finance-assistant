-- Finance Assistant synthetic banking dataset
-- MySQL 8.0+
-- Scale: 100,000 accounts and 4,000,000 transactions.
--
-- Derived from data/seed_data.sql with three correctness/performance fixes.
-- See the "DIVERGENCE FROM seed_data.sql" note at the bottom of this header.
--
-- Sensitive fields are encrypted with AES-256-CBC using the test key below.
--
-- TEST / BENCHMARK SECRET KEY (keep outside production source control):
--   0f968a8718c9c62336f691c96d44cced25f1659ddbdd29db53ee69c1a3bd9bfb
-- IV:
--   266786ef98ef74266dfba439bf428639
--
-- Decrypt examples:
--   SELECT CONVERT(AES_DECRYPT(account_number, UNHEX('0f96...9bfb'), UNHEX('2667...8639')) USING utf8mb4)
--   FROM account LIMIT 1;
--
-- IMPORTANT: this is synthetic benchmark data. Do not reuse this key in production.
--
-- Run:
--   mysql -u <user> -p < data/seed_data_4m.sql
--
-- DIVERGENCE FROM seed_data.sql
--   1. `seq` held 10^6 rows, so `WHERE s.n < @TX_COUNT` capped the load at
--      1,000,000 regardless of @TX_COUNT. Transactions are now generated in
--      @BATCH-sized passes over `seq`, offsetting n each pass, so the count is
--      whatever @TX_COUNT says.
--   2. `seq` was ENGINE=MEMORY; 10^6 rows exceeds the default 16MB
--      max_heap_table_size and errors with "table is full". Now InnoDB.
--   3. The transaction INSERT picked its account with a correlated
--      `ORDER BY ... LIMIT 1 OFFSET MOD(s.n, @ACCOUNT_COUNT)` subquery -- an
--      ordered scan skipping up to 99,999 rows, re-run for every generated row.
--      At 4M rows that is ~2x10^11 row visits. Replaced with `acct_map`, a
--      dense idx -> account_id table joined directly on MOD(k, @ACCOUNT_COUNT).
--      Same assignment, same clustering, ~4 orders of magnitude less work.
--
--   Secondary indexes and the FK on `transaction` are also created AFTER the
--   load rather than maintained during it.

SET NAMES utf8mb4;
SET SESSION block_encryption_mode = 'aes-256-cbc';

-- Bulk-load session tuning. Both are restored at the end of the script.
SET SESSION unique_checks = 0;
SET SESSION foreign_key_checks = 0;

DROP DATABASE IF EXISTS finance_assistant;
CREATE DATABASE finance_assistant CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE finance_assistant;

CREATE TABLE bank (
    bank_code VARCHAR(10) PRIMARY KEY,
    bank_name VARCHAR(150) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE account (
    account_id VARCHAR(36) PRIMARY KEY,
    entity_id VARCHAR(36) NOT NULL,
    -- Encrypted account number. Plaintext is never stored.
    account_number VARBINARY(128) NOT NULL,
    program_id INT NOT NULL,
    available_balance DECIMAL(15,2) NOT NULL DEFAULT 0.00,
    bank_code VARCHAR(10) NOT NULL,
    FOREIGN KEY (bank_code) REFERENCES bank(bank_code),
    INDEX idx_account_entity (entity_id),
    INDEX idx_account_bank (bank_code),
    INDEX idx_account_program (program_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Secondary indexes and the account FK are added after the bulk load; building
-- them in one pass over the finished table beats maintaining four B-trees
-- across 4M random-UUID inserts.
CREATE TABLE `transaction` (
    transaction_id VARCHAR(36) PRIMARY KEY,
    account_id VARCHAR(36) NOT NULL,
    transaction_date TIMESTAMP(6) NOT NULL,
    transaction_type ENUM('credit','debit') NOT NULL,
    description VARCHAR(500) DEFAULT NULL,
    transaction_amount DECIMAL(15,2) NOT NULL DEFAULT 0.00,
    transaction_reference_id VARCHAR(64) DEFAULT NULL,
    -- Encrypted UTR. Plaintext is never stored.
    utr_number VARBINARY(512) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO bank (bank_code, bank_name) VALUES
('HDFC','HDFC BANK LIMITED'),
('ICIC','ICICI BANK LIMITED'),
('SBIN','STATE BANK OF INDIA'),
('UTIB','AXIS BANK LIMITED'),
('KKBK','KOTAK MAHINDRA BANK LIMITED'),
('CNRB','CANARA BANK'),
('UBIN','UNION BANK OF INDIA'),
('AUBL','AU SMALL FINANCE BANK LIMITED'),
('TMBL','TAMILNAD MERCANTILE BANK LIMITED'),
('RATN','RBL BANK LIMITED');

-- ---------------------------------------------------------------------------
-- Scale controls
-- ---------------------------------------------------------------------------
SET @ACCOUNT_COUNT = 100000;
SET @TX_COUNT      = 4000000;
SET @BATCH         = 1000000;   -- rows per pass; must be <= the size of `seq`
SET @START_DATE = '2023-01-01 00:00:00';
SET @END_DATE   = '2026-08-31 23:59:59';

-- ---------------------------------------------------------------------------
-- Helper sequence: 0..999,999.
--
-- InnoDB, not MEMORY: 10^6 rows overflows the default 16MB
-- max_heap_table_size. Not TEMPORARY either, because the load procedure below
-- reads it across statements and a temp table complicates re-running a failed
-- batch by hand.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS seq;
CREATE TABLE seq (n INT PRIMARY KEY) ENGINE=InnoDB;

INSERT INTO seq (n)
SELECT a.n + b.n * 10 + c.n * 100 + d.n * 1000 + e.n * 10000 + f.n * 100000
FROM
    (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL
     SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL
     SELECT 8 UNION ALL SELECT 9) a
CROSS JOIN
    (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL
     SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL
     SELECT 8 UNION ALL SELECT 9) b
CROSS JOIN
    (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL
     SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL
     SELECT 8 UNION ALL SELECT 9) c
CROSS JOIN
    (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL
     SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL
     SELECT 8 UNION ALL SELECT 9) d
CROSS JOIN
    (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL
     SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL
     SELECT 8 UNION ALL SELECT 9) e
CROSS JOIN
    (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL
     SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL
     SELECT 8 UNION ALL SELECT 9) f;

-- ---------------------------------------------------------------------------
-- Accounts
-- account_number plaintext exists only inside AES_ENCRYPT's expression.
-- ---------------------------------------------------------------------------
INSERT INTO account (
    account_id, entity_id, account_number, program_id, available_balance, bank_code
)
SELECT
    UUID(),
    UUID(),
    AES_ENCRYPT(
        LPAD(10000000000000 + s.n, 14, '0'),
        UNHEX('0f968a8718c9c62336f691c96d44cced25f1659ddbdd29db53ee69c1a3bd9bfb'),
        UNHEX('266786ef98ef74266dfba439bf428639')
    ),
    CAST(ELT(1 + MOD(s.n, 3), 21, 4, 46) AS UNSIGNED),
    ROUND(
        CASE
            WHEN MOD(s.n, 20) = 0 THEN -50000000 + RAND(s.n * 17) * 20000000
            ELSE RAND(s.n * 17) * 50000000
        END, 2
    ),
    ELT(1 + MOD(s.n, 10),
        'HDFC','ICIC','SBIN','UTIB','KKBK',
        'CNRB','UBIN','AUBL','TMBL','RATN'
    )
FROM seq s
WHERE s.n < @ACCOUNT_COUNT;

-- ---------------------------------------------------------------------------
-- Dense account index.
--
-- This is fix (3). The original picked an account per generated row with
-- `ORDER BY account_id LIMIT 1 OFFSET MOD(n, @ACCOUNT_COUNT)`, correlated --
-- so every row re-scanned `account` and skipped up to 99,999 entries. Sorting
-- once into a keyed map turns that into a primary-key lookup while preserving
-- the original account-ordering semantics exactly.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS acct_map;
CREATE TABLE acct_map (
    idx        INT PRIMARY KEY,
    account_id VARCHAR(36) NOT NULL
) ENGINE=InnoDB;

INSERT INTO acct_map (idx, account_id)
SELECT ROW_NUMBER() OVER (ORDER BY account_id) - 1, account_id
FROM account;

-- ---------------------------------------------------------------------------
-- Transactions
-- Synthetic patterns:
--   ~65% debit / 35% credit
--   Indian banking descriptions (UPI/NEFT/IMPS/RTGS/FT/charges/merchant)
--   clustered account activity
--   realistic-ish amount distribution
--   encrypted UTR values (~80% of rows)
--
-- Generated in @BATCH-sized passes over `seq`. Pass p contributes logical
-- indices k = n + p*@BATCH, so every row still gets a distinct k and the
-- seeded RAND(k * prime) patterns stay distinct across the whole 4M -- which a
-- single pass over a 10^6-row `seq` could not do.
--
-- Each pass is its own statement, so the undo log stays bounded and an
-- interrupted load does not roll back hours of work.
-- ---------------------------------------------------------------------------
DROP PROCEDURE IF EXISTS load_transactions;
DELIMITER $$
CREATE PROCEDURE load_transactions()
BEGIN
    DECLARE p   INT DEFAULT 0;
    DECLARE off BIGINT;
    DECLARE lim BIGINT;

    WHILE p * @BATCH < @TX_COUNT DO
        SET off = p * @BATCH;
        SET lim = LEAST(@BATCH, @TX_COUNT - off);

        INSERT INTO `transaction` (
            transaction_id, account_id, transaction_date, transaction_type,
            description, transaction_amount, transaction_reference_id, utr_number
        )
        SELECT
            UUID(),
            m.account_id,

            TIMESTAMPADD(
                SECOND,
                FLOOR(RAND(g.k * 7919) * TIMESTAMPDIFF(SECOND, @START_DATE, @END_DATE)),
                @START_DATE
            ),

            CASE WHEN RAND(g.k * 104729) < 0.65 THEN 'debit' ELSE 'credit' END,

            CASE MOD(g.k, 12)
                WHEN 0 THEN CONCAT(
                    'UPI-',
                    ELT(1 + MOD(g.k, 8),
                        'SELECTION ELECTRONICS','NAVYUG SELECTION',
                        'SELECTION MOBILE','RELIANCE DIGITAL',
                        'AMAZON INDIA','FLIPKART','SWIGGY','ZOMATO'
                    ),
                    '-XXXXXX', LPAD(MOD(g.k * 31, 10000), 4, '0')
                )
                WHEN 1 THEN CONCAT(
                    'NEFT - ',
                    ELT(1 + MOD(g.k, 6),
                        'HDFC0001234','ICIC0001241','UTIB0002678',
                        'SBIN0004567','KKBK0009876','CNRB0003210'
                    ),
                    ' - ',
                    ELT(1 + MOD(g.k, 6),
                        'SELECTION TRADERS','SELECTION MOBILE',
                        'SELECTRICITY TWO PRIVATE LIMITED',
                        'UMANG SELECTION HAPUR','PARESH VIKRANT GHASE',
                        'RELIANCE DIGITAL RETAIL LTD'
                    )
                )
                WHEN 2 THEN CONCAT(
                    'IMPS/P2A/',
                    LPAD(100000000000 + MOD(g.k * 7919, 899999999999), 12, '0'),
                    '/SBIN/'
                )
                WHEN 3 THEN CONCAT(
                    'RTGS - ',
                    ELT(1 + MOD(g.k, 4),
                        'HDFC BANK','ICICI BANK','AXIS BANK','STATE BANK OF INDIA'
                    )
                )
                WHEN 4 THEN CONCAT(
                    'FT - ',
                    LPAD(10000000 + MOD(g.k * 97, 89999999), 8, '0'),
                    ' - INTERNAL TRANSFER'
                )
                WHEN 5  THEN 'IMPS charges'
                WHEN 6  THEN 'Cheque Deposits'
                WHEN 7  THEN 'Cash Deposit'
                WHEN 8  THEN 'ATM CASH WITHDRAWAL'
                WHEN 9  THEN 'SALARY CREDIT'
                WHEN 10 THEN 'EMI PAYMENT - BAJAJ FINANCE'
                ELSE 'BANK CHARGES'
            END,

            ROUND(
                CASE
                    WHEN MOD(g.k, 20) < 12 THEN 100 + RAND(g.k * 1543) * 9900
                    WHEN MOD(g.k, 20) < 18 THEN 10000 + RAND(g.k * 1543) * 90000
                    WHEN MOD(g.k, 20) < 20 THEN 100000 + RAND(g.k * 1543) * 4900000
                    ELSE 5000000 + RAND(g.k * 1543) * 50000000
                END,
                2
            ),

            CONCAT(
                ELT(1 + MOD(g.k, 6), 'HDFC','S','NEFT','IMPS','UPI','RTGS'),
                LPAD(1000000000 + MOD(g.k * 7919, 8999999999), 10, '0')
            ),

            CASE
                WHEN MOD(g.k, 10) < 8 THEN
                    AES_ENCRYPT(
                        CONCAT('UTR',
                               LPAD(MOD(g.k * 104729 + 734003, 999999999999999999), 18, '0')),
                        UNHEX('0f968a8718c9c62336f691c96d44cced25f1659ddbdd29db53ee69c1a3bd9bfb'),
                        UNHEX('266786ef98ef74266dfba439bf428639')
                    )
                ELSE NULL
            END
        FROM (SELECT s.n + off AS k FROM seq s WHERE s.n < lim) g
        JOIN acct_map m ON m.idx = MOD(g.k, @ACCOUNT_COUNT);

        SET p = p + 1;
    END WHILE;
END$$
DELIMITER ;

CALL load_transactions();

DROP PROCEDURE load_transactions;

-- ---------------------------------------------------------------------------
-- Indexes + FK, built in a single pass now that the rows are in place.
-- foreign_key_checks is still 0, so the FK is added without re-validating all
-- 4M rows -- they came from acct_map, so every account_id is valid by
-- construction.
-- ---------------------------------------------------------------------------
ALTER TABLE `transaction`
    ADD INDEX idx_tx_account_date (account_id, transaction_date),
    ADD INDEX idx_tx_date (transaction_date),
    ADD INDEX idx_tx_type_date (transaction_type, transaction_date),
    ADD INDEX idx_tx_reference (transaction_reference_id),
    ADD CONSTRAINT fk_tx_account FOREIGN KEY (account_id) REFERENCES account(account_id);

DROP TABLE seq;
DROP TABLE acct_map;

SET SESSION unique_checks = 1;
SET SESSION foreign_key_checks = 1;

ANALYZE TABLE bank, account, `transaction`;

-- ---------------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------------
SELECT
    (SELECT COUNT(*) FROM bank)          AS banks,
    (SELECT COUNT(*) FROM account)       AS accounts,
    (SELECT COUNT(*) FROM `transaction`) AS transactions;

-- Distribution sanity: type mix, date span, amount buckets.
SELECT transaction_type, COUNT(*) AS n,
       ROUND(100 * COUNT(*) / (SELECT COUNT(*) FROM `transaction`), 1) AS pct
FROM `transaction` GROUP BY transaction_type;

SELECT MIN(transaction_date) AS earliest,
       MAX(transaction_date) AS latest,
       COUNT(DISTINCT account_id) AS accounts_with_tx
FROM `transaction`;

-- Safe verification: only decrypted values are displayed; the stored columns
-- themselves remain ciphertext.
SELECT
    account_id,
    CONVERT(AES_DECRYPT(account_number, UNHEX('0f968a8718c9c62336f691c96d44cced25f1659ddbdd29db53ee69c1a3bd9bfb'), UNHEX('266786ef98ef74266dfba439bf428639')) USING utf8mb4)
        AS decrypted_account_number,
    bank_code,
    program_id
FROM account
LIMIT 5;

SELECT
    transaction_id,
    CONVERT(AES_DECRYPT(utr_number, UNHEX('0f968a8718c9c62336f691c96d44cced25f1659ddbdd29db53ee69c1a3bd9bfb'), UNHEX('266786ef98ef74266dfba439bf428639')) USING utf8mb4)
        AS decrypted_utr
FROM `transaction`
WHERE utr_number IS NOT NULL
LIMIT 5;
