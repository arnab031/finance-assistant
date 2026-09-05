"""
Profile for the organizers' schema: bank / account / transaction.

Three structural facts drive almost every choice in this file.

1. AMOUNTS ARE UNSIGNED. `transaction_amount` is always positive and direction
   lives in `transaction_type`. So "spend" is a sum over debits only, and "net
   movement" is credits minus debits. The v_txn view pre-splits this into
   credit_amount / debit_amount / signed_amount so the compiler keeps using
   plain SUM() rather than learning conditional aggregation.

2. THERE IS NO COUNTERPARTY TABLE. Every merchant, payer and payee name lives
   inside the free-text `description`. "How much did we pay Reliance" is a text
   search, not a foreign-key lookup - which is why entity_sql below searches
   description with trigrams instead of joining a vendors table.

3. TWO FIELDS ARE SENSITIVE. account_number and utr_number must never be shown
   raw. Neither is exposed here; the view serves masked forms only, so a raw
   value cannot reach an answer even if a query forgets to mask.
"""

from __future__ import annotations

from api.profiles.base import Dim, Filt, Profile, SemanticSource

DIMENSIONS = {
    "bank":             Dim("t.bank_name", "bank"),
    "account":          Dim("t.account_id", "account"),
    "entity":           Dim("t.entity_id", "entity"),
    "program":          Dim("CAST(t.program_id AS CHAR)", "program"),
    "transaction_type": Dim("t.transaction_type", "transaction_type"),
    # Derived in api/narration.py. Without it this schema cannot answer "who did
    # we pay?" at all - the name is buried in free-text description.
    "counterparty":     Dim("t.counterparty", "counterparty"),
    # MySQL has no date_trunc. The obvious replacement is
    # DATE_FORMAT(x, '%Y-%m-01'), but a literal % in generated SQL is a landmine:
    # PyMySQL interpolates the query whenever parameters are bound, so '%Y' is
    # read as a placeholder and the query raises. INTERVAL arithmetic gets the
    # same DATE with no % anywhere, so it composes with any WHERE clause.
    "month":            Dim("(DATE(t.transaction_date) - INTERVAL (DAYOFMONTH(t.transaction_date) - 1) DAY)",
                            "month"),
    "quarter":          Dim("(MAKEDATE(YEAR(t.transaction_date), 1) + INTERVAL (QUARTER(t.transaction_date) - 1) QUARTER)",
                            "quarter"),
    "day":              Dim("DATE(t.transaction_date)", "day"),
}

METRICS = {
    # "spend" means money leaving the account. Debits only.
    "debit_amount":       "SUM(t.debit_amount)",
    "credit_amount":      "SUM(t.credit_amount)",
    # credits minus debits - the only metric that can legitimately go negative
    "net_amount":         "SUM(t.signed_amount)",
    # every transaction regardless of direction; rarely what a user means
    "gross_amount":       "SUM(t.transaction_amount)",
    "txn_count":          "COUNT(*)",
    "account_count":      "COUNT(DISTINCT t.account_id)",
    "entity_count":       "COUNT(DISTINCT t.entity_id)",
    "avg_amount":         "AVG(t.transaction_amount)",
    "max_amount":         "MAX(t.transaction_amount)",
}

MONEY = frozenset({"debit_amount", "credit_amount", "net_amount",
                   "gross_amount", "avg_amount", "max_amount"})

FILTERS = {
    "transaction_type": Filt("t.transaction_type",
                             hint="'credit' or 'debit' only."),
    "banks":            Filt("t.bank_name", hint="Full bank names as listed above."),
    "account_ids":      Filt("t.account_id"),
    "entity_ids":       Filt("t.entity_id"),
    "programs":         Filt("CAST(t.program_id AS CHAR)"),
    # Prefer counterparty: it is the extracted name, so it matches what a user
    # types. description_like stays as the fallback for narrations the parser
    # could not read, and for searching reference text inside the narration.
    "counterparty_like": Filt(
        "t.counterparty", kind="text",
        hint="A company or person name. This is the ONLY place a name goes. "
             "Generic words like 'payments', 'vendors', 'transactions' are not "
             "names - omit the field for those."),
    "description_like": Filt(
        "t.description", kind="text",
        hint="Non-name narration text only: a payment channel (UPI, NEFT), a "
             "reference fragment, or a location. Prefer counterparty_like for "
             "people and companies."),
    "reference_id": Filt(
        "t.transaction_reference_id",
        hint="A bare reference or receipt number. Not the UTR."),
    # Compiled against the metric's own amount column, not this sql string -
    # see _where. Declared here so the extraction schema offers them at all.
    "min_amount": Filt("t.transaction_amount", kind="number",
                       hint="Lower bound. 'over 50000', 'above 1 lakh', "
                            "'more than 10000'."),
    "max_amount": Filt("t.transaction_amount", kind="number",
                       hint="Upper bound. 'under 5000', 'less than 1000'."),
}

# Domains the brief's schema does not contain. Matching one forces a decline in
# sanity_pass rather than trusting the model to hold the line - see
# _force_unsupported_for_absent_concepts.
ABSENT_CONCEPTS = (
    (r"\bvendors?\b|\bsuppliers?\b|\bpayouts?\b",
     "This data has no vendor or payout structure - only bank credits and "
     "debits, with the counterparty name read out of the narration text."),
    (r"\bcategor|\bchart of accounts\b|\bexpense type",
     "There are no expense categories or chart of accounts in this data."),
    (r"\bbudget|\bforecast",
     "There is no budget or forecast data here."),
    (r"\binvoices?\b|\bbills?\b|\boverdue\b",
     "There are no invoices in this data, only settled bank transactions."),
    (r"\w*reconcil\w*",
     "This data has no reconciliation status of any kind."),
    (r"\bheadcount\b|\bemployees?\b|\bpayroll\b",
     "There is no employee or payroll data here."),
    (r"\bdepartments?\b",
     "There are no departments in this data."),
)

PROMPT_RULES = """
metric
  "spend", "spent", "paid out", "outgoing", "debits"  -> debit_amount
  "spend in total", "total spend", "how much did we spend overall"
                                                      -> debit_amount

  "in total" and "overall" describe the DATE RANGE - the whole dataset - not
  the metric. They never turn a spend question into gross_amount.
  "received", "incoming", "credits", "deposits"       -> credit_amount
  "net", "net flow", "net movement"                   -> net_amount
  "total transacted", "throughput"                    -> gross_amount
  "how many transactions"                             -> txn_count
  "how many accounts"                                 -> account_count
  "who did we pay the most"                           -> group_by ["counterparty"]
  "largest transaction", "biggest single payment"     -> max_amount
  "average transaction"                               -> avg_amount

  A "single largest" question wants ONE transaction's amount, so it is
  max_amount. gross_amount would add every transaction together and report a
  total roughly twice the largest one - a wrong answer that still looks like
  money.

  Amounts in this data are ALWAYS POSITIVE; direction is transaction_type.
  Never use gross_amount for a "spend" question - it adds credits and debits
  together and answers nothing anyone asked.

group_by
  "split between credits and debits", "by direction", "credits vs debits",
  "breakdown by type" -> group_by ["transaction_type"]
  Without that grouping the two directions collapse into one number and the
  split the question asked for is not in the answer at all.

filters.min_amount / filters.max_amount
  "over 50000", "above 1 lakh", "more than 10000"  -> min_amount
  "under 5000", "below 1000"                       -> max_amount
  These are numbers, not strings, and not a description search.

filters.counterparty_like
  There is NO vendor table. Put a company or person name HERE - it is matched
  against a counterparty name extracted from the narration.
  Generic words ("payments", "transactions", "transfers", "vendors") are NOT
  names; leave the filter out entirely for those.

filters.description_like
  Only for text that is NOT a name - a reference fragment, a payment channel
  ("UPI", "NEFT"), or a location. Prefer counterparty_like for people and
  companies.

reference numbers
  A bare "reference number" or "ref no" means transaction_reference_id.
  Only use utr_number when the user says "UTR" explicitly.

sensitive data
  Account numbers and UTR numbers are masked and cannot be shown in full.
  Never claim to display one.

intent
  aggregate  - one number, optionally grouped
  list       - individual transactions
  compare    - two periods side by side
  anomaly    - unusually large transactions
  unsupported- the KIND of data is missing. This database holds ONLY bank
               statement lines: banks, accounts and credit/debit transactions.
               It has NO vendors, categories, budgets, invoices, reconciliation
               status, employees, or chart of accounts.

  A DATE outside the data is NOT unsupported. "How much did we spend in 2019?"
  is an ordinary spend question - answer it with intent aggregate and the 2019
  period. The system checks coverage itself and reports that no data exists for
  that range, which tells the user far more than "I cannot answer that".
"""

FEWSHOT: list[tuple[str, dict]] = [
    ("How much did we spend in total?",
     {"intent": "aggregate", "metric": "debit_amount", "group_by": [],
      "date_basis": "payment_date",
      "reasoning": "'in total' means all dates, not all directions - still debits only"}),

    ("How much did we spend last month?",
     {"intent": "aggregate", "metric": "debit_amount", "group_by": [],
      "date_basis": "payment_date",
      "period": {"start": "2026-08-01", "end": "2026-09-01", "label": "August 2026"},
      "reasoning": "spend means money out, so debits only"}),

    ("How much did we receive last month?",
     {"intent": "aggregate", "metric": "credit_amount", "group_by": [],
      "date_basis": "payment_date",
      "period": {"start": "2026-08-01", "end": "2026-09-01", "label": "August 2026"}}),

    ("What was the net movement across all accounts?",
     {"intent": "aggregate", "metric": "net_amount", "group_by": [],
      "date_basis": "payment_date",
      "reasoning": "no period mentioned, so all data"}),

    # A company name goes in counterparty_like, never in another filter.
    ("How much did we pay Reliance Digital?",
     {"intent": "aggregate", "metric": "debit_amount", "group_by": [],
      "date_basis": "payment_date",
      "filters": {"counterparty_like": "RELIANCE"},
      "reasoning": "a company name is a counterparty search"}),

    ("Who did we pay the most?",
     {"intent": "aggregate", "metric": "debit_amount", "group_by": ["counterparty"],
      "date_basis": "payment_date", "sort_desc": True, "limit": 10}),

    ("How much went to Gautam Singh?",
     {"intent": "aggregate", "metric": "debit_amount", "group_by": [],
      "date_basis": "payment_date",
      "filters": {"counterparty_like": "GAUTAM SINGH"}}),

    ("Break down spend by bank last quarter",
     {"intent": "aggregate", "metric": "debit_amount", "group_by": ["bank"],
      "date_basis": "payment_date",
      "period": {"start": "2026-04-01", "end": "2026-07-01", "label": "Q2 2026"}}),

    ("Show me the 10 largest debits in June 2026",
     {"intent": "list", "metric": "debit_amount", "group_by": [],
      "date_basis": "payment_date",
      "period": {"start": "2026-06-01", "end": "2026-07-01", "label": "June 2026"},
      "sort_desc": True, "limit": 10}),

    ("What was our single largest transaction?",
     {"intent": "aggregate", "metric": "max_amount", "group_by": [],
      "date_basis": "payment_date",
      "reasoning": "one transaction's amount, so MAX - never a SUM"}),

    ("Show the split between credits and debits",
     {"intent": "aggregate", "metric": "gross_amount",
      "group_by": ["transaction_type"], "date_basis": "payment_date"}),

    ("List the debits over 50000",
     {"intent": "list", "metric": "debit_amount", "group_by": [],
      "date_basis": "payment_date",
      "filters": {"transaction_type": ["debit"], "min_amount": 50000},
      "sort_desc": True, "limit": 100}),

    # A year with no data is still a spend question. Answering it lets the
    # coverage check say WHY there is nothing, instead of a blank refusal.
    ("How much did we spend in 2019?",
     {"intent": "aggregate", "metric": "debit_amount", "group_by": [],
      "date_basis": "payment_date",
      "period": {"start": "2019-01-01", "end": "2020-01-01", "label": "2019"},
      "reasoning": "outside the data, but a date range is not an unsupported question"}),

    ("Find the transaction with reference 1715499972",
     {"intent": "list", "metric": "gross_amount", "group_by": [],
      "date_basis": "payment_date",
      "filters": {"reference_id": ["1715499972"]},
      "reasoning": "a bare reference number means transaction_reference_id"}),

    ("Which transactions are still unreconciled?",
     {"intent": "unsupported", "metric": "debit_amount", "group_by": [],
      "date_basis": "payment_date",
      "reasoning": "this database has no reconciliation status"}),

    ("What is our headcount?",
     {"intent": "unsupported", "metric": "debit_amount", "group_by": [],
      "date_basis": "payment_date",
      "reasoning": "no employee data in this database"}),
]

PROFILE = Profile(
    name="bank_txn",
    label="Bank statements (bank / account / transaction)",
    database="tbx_live",

    fact="v_txn t",
    alias="t",
    date_column="t.transaction_date",
    dimensions=DIMENSIONS,
    metrics=METRICS,
    money_metrics=MONEY,
    filters=FILTERS,
    joins={},                       # v_txn is already denormalised
    list_columns=[
        "t.transaction_id", "t.transaction_date", "t.transaction_type",
        "t.transaction_amount", "t.description", "t.bank_name",
        "t.account_number",          # ciphertext -> placeholder -> user
        "t.transaction_reference_id", "t.utr_masked",
    ],

    coverage_sql="""
        SELECT CAST(MIN(transaction_date) AS DATE) AS earliest,
               CAST(MAX(transaction_date) AS DATE) AS latest,
               COUNT(*)                    AS n,
               COALESCE(SUM(CASE WHEN transaction_type='debit'
                                 THEN transaction_amount ELSE 0 END), 0) AS paid
        FROM transaction
    """,
    vocab_sql={
        # Named to match SemanticRegistry's fields. Anything absent stays empty,
        # which is how a detector learns it has nothing to detect.
        "banks":            "SELECT DISTINCT bank_name FROM bank ORDER BY 1",
        "payment_statuses": "SELECT DISTINCT transaction_type FROM transaction ORDER BY 1",
        "programs":         "SELECT DISTINCT CAST(program_id AS CHAR) FROM account ORDER BY 1",
    },
    entity_count_sql="SELECT COUNT(*) FROM account",
    money_columns_table="v_txn",
    capability_sql={
        # These decide which ambiguity detectors arm. All three are absent here,
        # so the temporal, scope and paid-vs-committed detectors disarm on their
        # own - no code change, which is the point of the registry design.
        "has_fiscal_year":    "SELECT FALSE",
        "has_reconciliation": "SELECT FALSE",
        "has_payouts":        "SELECT FALSE",
    },

    entity_kind="counterparty",
    # PostgreSQL ranked these with pg_trgm's similarity(); MySQL has no trigram
    # operator and FULLTEXT matches whole words, so it would never find
    # "RELIANCE" inside "RELIANCEDIGITAL RETAIL LTD" - which is exactly the data.
    #
    # So candidates come from LIKE and are ranked by a cheap positional score:
    # an exact match first, then a prefix match, then a match anywhere, with the
    # shortest label winning ties. Shortest matters because "SELECTION MOBILE"
    # should outrank "UMANG SELECTIONHAPURBPES" for the query "SELECTION" - the
    # extra characters are noise, not relevance.
    entity_sql="""
        SELECT counterparty AS label,
               COUNT(*)          AS txn_count,
               SUM(debit_amount) AS total_amount,
               MAX(CASE
                     WHEN counterparty = %(q)s               THEN 1.0
                     WHEN LEFT(counterparty, CHAR_LENGTH(%(q)s)) = %(q)s THEN 0.8
                     ELSE 0.5
                   END) - (LENGTH(counterparty) / 1000) AS sim
        FROM v_txn
        WHERE counterparty IS NOT NULL
          AND counterparty LIKE %(like)s
        GROUP BY counterparty
        ORDER BY sim DESC, total_amount DESC
        LIMIT %(lim)s
    """,

    # Every chip is answerable from bank/account/transaction and is drawn from
    # the canary set, so each has verified ground truth behind it.
    suggestions=[
        "How much did we spend in June 2026?",
        "How much money came in in total?",
        "Break down spending by bank",
        "Show me the 3 largest transactions",
        "How much did we pay to SELECTION?",
        "Who did we pay the most?",
        "Compare June 2026 with May 2026",
        "What was our single largest transaction?",
    ],
    placeholder="Ask about spending, credits, accounts or banks…",

    prompt_rules=PROMPT_RULES,
    fewshot=FEWSHOT,
    unsupported_note=(
        "That isn't answerable from this data. It holds bank statement lines — "
        "banks, accounts, and credit/debit transactions. There is no vendor "
        "master, no expense categories, no reconciliation status, and no "
        "budget or employee data."
    ),
    # No reconciliation table exists, so the intent cannot be compiled.
    disabled_intents=frozenset({"reconcile"}),
    absent_concepts=ABSENT_CONCEPTS,

    # EXACTLY ONE source, and deliberately not `description`.
    #
    # Embedding the raw narration would cluster by PAYMENT RAIL, not by who was
    # paid: two unrelated NEFT transfers share the shape
    # NEFT/<digits>/<BANK>/<name>, so their embeddings sit close together while
    # the same merchant reached by UPI sits far away. That is worse than having
    # no index, because it looks like it is working.
    #
    # The extracted counterparty is the opposite: a short, clean name, one row
    # per distinct payee. Everything else in this schema is an enum (two
    # values), an id, or a bank name already matched exactly - none of which
    # gain anything from an embedding.
    semantic_sources=(
        SemanticSource(
            entity_type="counterparty",
            sql="""
                SELECT counterparty AS entity_key, counterparty AS label
                FROM `transaction`
                WHERE counterparty IS NOT NULL AND counterparty <> ''
                GROUP BY counterparty
            """,
            note="filters.counterparty_like -> t.counterparty",
        ),
    ),
)
