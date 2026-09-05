-- Finance Assistant synthetic banking dataset
-- MySQL 8.0+
-- Default scale: 100,000 accounts and 10,000,000 transactions.
-- Sensitive fields are encrypted with AES-256-CBC using the test key below.
--
-- TEST / BENCHMARK SECRET KEY (keep outside production source control):
--   0f968a8718c9c62336f691c96d44cced25f1659ddbdd29db53ee69c1a3bd9bfb
-- IV:
--   266786ef98ef74266dfba439bf428639
--
-- Decrypt examples:
--   SELECT CONVERT(AES_DECRYPT(account_number, UNHEX('0f968a8718c9c62336f691c96d44cced25f1659ddbdd29db53ee69c1a3bd9bfb'), UNHEX('266786ef98ef74266dfba439bf428639')) USING utf8mb4)
--   FROM account LIMIT 1;
--
--   SELECT CONVERT(AES_DECRYPT(utr_number, UNHEX('0f968a8718c9c62336f691c96d44cced25f1659ddbdd29db53ee69c1a3bd9bfb'), UNHEX('266786ef98ef74266dfba439bf428639')) USING utf8mb4)
--   FROM transaction WHERE utr_number IS NOT NULL LIMIT 1;
--
-- IMPORTANT: this is synthetic benchmark data. Do not reuse this key in production.

SET NAMES utf8mb4;
SET SESSION block_encryption_mode = 'aes-256-cbc';

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

CREATE TABLE transaction (
    transaction_id VARCHAR(36) PRIMARY KEY,
    account_id VARCHAR(36) NOT NULL,
    transaction_date TIMESTAMP(6) NOT NULL,
    transaction_type ENUM('credit','debit') NOT NULL,
    description VARCHAR(500) DEFAULT NULL,
    transaction_amount DECIMAL(15,2) NOT NULL DEFAULT 0.00,
    transaction_reference_id VARCHAR(64) DEFAULT NULL,
    -- Encrypted UTR. Plaintext is never stored.
    utr_number VARBINARY(512) DEFAULT NULL,
    FOREIGN KEY (account_id) REFERENCES account(account_id),
    INDEX idx_tx_account_date (account_id, transaction_date),
    INDEX idx_tx_date (transaction_date),
    INDEX idx_tx_type_date (transaction_type, transaction_date),
    INDEX idx_tx_reference (transaction_reference_id)
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
SET @TX_COUNT = 10000000;
SET @START_DATE = '2023-01-01 00:00:00';
SET @END_DATE   = '2026-08-31 23:59:59';

-- ---------------------------------------------------------------------------
-- Helper sequence: 0..999,999. Cross joins produce enough rows for accounts
-- and transactions. The transaction INSERT uses only the first @TX_COUNT.
-- ---------------------------------------------------------------------------
DROP TEMPORARY TABLE IF EXISTS seq;
CREATE TEMPORARY TABLE seq (
    n INT PRIMARY KEY
) ENGINE=MEMORY;

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
-- entity_id is deliberately shared across some accounts to model customers
-- owning multiple accounts.
-- ---------------------------------------------------------------------------
INSERT INTO account (
    account_id, entity_id, account_number, program_id, available_balance, bank_code
)
SELECT
    UUID(),
    UUID(),
    AES_ENCRYPT(
        CONCAT(
            LPAD(10000000000000 + s.n, 14, '0')
        ),
        UNHEX('0f968a8718c9c62336f691c96d44cced25f1659ddbdd29db53ee69c1a3bd9bfb'),
        UNHEX('266786ef98ef74266dfba439bf428639')
    ),
    ELT(1 + MOD(s.n, 3), 21, 4, 46),
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
-- Transactions
-- Synthetic patterns:
--   ~65% debit / 35% credit
--   Indian banking descriptions (UPI/NEFT/IMPS/RTGS/FT/charges/merchant)
--   clustered account activity
--   realistic-ish amount distribution
--   encrypted UTR values
--
-- UUID() is used for transaction IDs. The transaction reference remains
-- plaintext/searchable, matching the source schema's distinction.
-- ---------------------------------------------------------------------------
INSERT INTO transaction (
    transaction_id,
    account_id,
    transaction_date,
    transaction_type,
    description,
    transaction_amount,
    transaction_reference_id,
    utr_number
)
SELECT
    UUID(),
    a.account_id,

    TIMESTAMPADD(
        SECOND,
        FLOOR(
            RAND(s.n * 7919) *
            TIMESTAMPDIFF(SECOND, @START_DATE, @END_DATE)
        ),
        @START_DATE
    ),

    CASE WHEN RAND(s.n * 104729) < 0.65 THEN 'debit' ELSE 'credit' END,

    CASE MOD(s.n, 12)
        WHEN 0 THEN CONCAT(
            'UPI-',
            ELT(1 + MOD(s.n, 8),
                'SELECTION ELECTRONICS','NAVYUG SELECTION',
                'SELECTION MOBILE','RELIANCE DIGITAL',
                'AMAZON INDIA','FLIPKART','SWIGGY','ZOMATO'
            ),
            '-XXXXXX', LPAD(MOD(s.n * 31, 10000), 4, '0')
        )
        WHEN 1 THEN CONCAT(
            'NEFT - ',
            ELT(1 + MOD(s.n, 6),
                'HDFC0001234','ICIC0001241','UTIB0002678',
                'SBIN0004567','KKBK0009876','CNRB0003210'
            ),
            ' - ',
            ELT(1 + MOD(s.n, 6),
                'SELECTION TRADERS','SELECTION MOBILE',
                'SELECTRICITY TWO PRIVATE LIMITED',
                'UMANG SELECTION HAPUR','PARESH VIKRANT GHASE',
                'RELIANCE DIGITAL RETAIL LTD'
            )
        )
        WHEN 2 THEN CONCAT(
            'IMPS/P2A/',
            LPAD(100000000000 + MOD(s.n * 7919, 899999999999), 12, '0'),
            '/SBIN/'
        )
        WHEN 3 THEN CONCAT(
            'RTGS - ',
            ELT(1 + MOD(s.n, 4),
                'HDFC BANK','ICICI BANK','AXIS BANK','STATE BANK OF INDIA'
            )
        )
        WHEN 4 THEN CONCAT(
            'FT - ',
            LPAD(10000000 + MOD(s.n * 97, 89999999), 8, '0'),
            ' - INTERNAL TRANSFER'
        )
        WHEN 5 THEN 'IMPS charges'
        WHEN 6 THEN 'Cheque Deposits'
        WHEN 7 THEN 'Cash Deposit'
        WHEN 8 THEN 'ATM CASH WITHDRAWAL'
        WHEN 9 THEN 'SALARY CREDIT'
        WHEN 10 THEN 'EMI PAYMENT - BAJAJ FINANCE'
        ELSE 'BANK CHARGES'
    END,

    ROUND(
        CASE
            WHEN MOD(s.n, 20) < 12
                THEN 100 + RAND(s.n * 1543) * 9900
            WHEN MOD(s.n, 20) < 18
                THEN 10000 + RAND(s.n * 1543) * 90000
            WHEN MOD(s.n, 20) < 20
                THEN 100000 + RAND(s.n * 1543) * 4900000
            ELSE 5000000 + RAND(s.n * 1543) * 50000000
        END,
        2
    ),

    CONCAT(
        ELT(1 + MOD(s.n, 6),
            'HDFC','S','NEFT','IMPS','UPI','RTGS'
        ),
        LPAD(1000000000 + MOD(s.n * 7919, 8999999999), 10, '0')
    ),

    CASE
        WHEN MOD(s.n, 10) < 8 THEN
            AES_ENCRYPT(
                CONCAT(
                    'UTR',
                    LPAD(
                        MOD(s.n * 104729 + 734003, 999999999999999999),
                        18,
                        '0'
                    )
                ),
                UNHEX('0f968a8718c9c62336f691c96d44cced25f1659ddbdd29db53ee69c1a3bd9bfb'),
                UNHEX('266786ef98ef74266dfba439bf428639')
            )
        ELSE NULL
    END
FROM seq s
JOIN account a
  ON a.account_id = (
      SELECT aa.account_id
      FROM account aa
      ORDER BY aa.account_id
      LIMIT 1 OFFSET MOD(s.n, @ACCOUNT_COUNT)
  )
WHERE s.n < @TX_COUNT;

DROP TEMPORARY TABLE seq;

ANALYZE TABLE bank, account, transaction;

-- ---------------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------------
SELECT
    (SELECT COUNT(*) FROM bank) AS banks,
    (SELECT COUNT(*) FROM account) AS accounts,
    (SELECT COUNT(*) FROM transaction) AS transactions;

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
FROM transaction
WHERE utr_number IS NOT NULL
LIMIT 5;
