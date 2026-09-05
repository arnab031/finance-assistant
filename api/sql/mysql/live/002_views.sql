-- Views the assistant leans on.
--
-- v_txn is the workhorse: it denormalises bank and account onto every
-- transaction (fewer joins means a small model writes correct SQL more often)
-- AND it splits the signed amount.
--
-- The signed-amount split matters more than it looks. `transaction_amount` is
-- ALWAYS POSITIVE; direction lives in `transaction_type`. So "how much did we
-- spend" is SUM over debits only, and "net movement" is credits minus debits.
-- Exposing signed_amount / credit_amount / debit_amount as columns lets the SQL
-- compiler keep using plain SUM() instead of learning conditional aggregation.

DROP VIEW IF EXISTS v_txn;
CREATE VIEW v_txn AS
SELECT
    t.transaction_id,
    t.account_id,
    a.entity_id,
    a.program_id,
    a.bank_code,
    b.bank_name,
    a.account_number,
    t.transaction_date,
    CAST(t.transaction_date AS DATE)      AS transaction_day,
    t.transaction_type,
    t.description,
    t.counterparty,
    t.transaction_amount,
    CASE WHEN t.transaction_type = 'credit'
         THEN t.transaction_amount ELSE -t.transaction_amount END AS signed_amount,
    CASE WHEN t.transaction_type = 'credit'
         THEN t.transaction_amount ELSE 0 END                     AS credit_amount,
    CASE WHEN t.transaction_type = 'debit'
         THEN t.transaction_amount ELSE 0 END                     AS debit_amount,
    t.transaction_reference_id,
    -- Masked at the view layer so a raw value cannot reach an answer even if a
    -- later query forgets to mask. The brief calls both of these sensitive.
    CASE WHEN t.utr_number IS NULL THEN NULL
         ELSE CONCAT('UTR-', RIGHT(t.utr_number, 4)) END          AS utr_masked,
    (t.utr_number IS NOT NULL)                                    AS has_utr
FROM `transaction` t
JOIN `account` a ON a.account_id = t.account_id
JOIN `bank`    b ON b.bank_code  = a.bank_code;

DROP VIEW IF EXISTS v_account;
CREATE VIEW v_account AS
SELECT
    a.account_id,
    a.entity_id,
    CONCAT('XXXXXX', RIGHT(a.account_number, 4)) AS account_number_masked,
    a.program_id,
    a.available_balance,
    a.bank_code,
    b.bank_name
FROM `account` a
JOIN `bank` b ON b.bank_code = a.bank_code;

DROP VIEW IF EXISTS v_data_coverage;
CREATE VIEW v_data_coverage AS
SELECT CAST(MIN(transaction_date) AS DATE) AS earliest,
       CAST(MAX(transaction_date) AS DATE) AS latest,
       COUNT(*)                            AS transaction_count,
       SUM(CASE WHEN transaction_type = 'debit'
                THEN transaction_amount ELSE 0 END) AS total_debits,
       SUM(CASE WHEN transaction_type = 'credit'
                THEN transaction_amount ELSE 0 END) AS total_credits
FROM `transaction`;
