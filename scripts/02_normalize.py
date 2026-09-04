#!/usr/bin/env python3
"""
Normalize raw SF vendor-payment vouchers into the six-table schema the TBX
problem statement asks for, then load everything into SQLite.

Inputs  : data/raw/sf_vendor_payments_YYYY-MM.csv.gz
Outputs : data/processed/*.csv  and  data/finance.db

REAL (sourced from DataSF, unmodified):
    vendors, chart_of_accounts, departments, funds, transactions, vendor_payouts

SYNTHESIZED (deterministic, seeded - see reconciliation.csv + DATA_DICTIONARY.md):
    reconciliation status, bank statement refs, payment_method

The SF source publishes paid / pending / retainage amounts but not bank-level
reconciliation state, so that layer is generated. It is seeded off a hash of the
transaction id, so every run produces byte-identical output.

Stdlib only - no pip install required.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "data" / "processed"
DB_PATH = ROOT / "data" / "finance.db"

SEED = "tbx-bvp-2026"

# ---------------------------------------------------------------- helpers


def _u64(salt: str, key: str) -> int:
    return int(hashlib.blake2b(f"{SEED}:{salt}:{key}".encode(), digest_size=8).hexdigest(), 16)


def unit(salt: str, key: str) -> float:
    """Deterministic pseudo-random float in [0, 1)."""
    return _u64(salt, key) / 2**64


def pick(salt: str, key: str, options: list[str], weights: list[float]) -> str:
    r = unit(salt, key) * sum(weights)
    acc = 0.0
    for opt, w in zip(options, weights):
        acc += w
        if r < acc:
            return opt
    return options[-1]


def money(raw: str) -> float:
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return 0.0


def iso_date(raw: str) -> str:
    """'2026-05-12 00:00:00-07:00' -> '2026-05-12'."""
    return (raw or "")[:10]


def raw_files() -> list[Path]:
    files = sorted(RAW_DIR.glob("sf_vendor_payments_*.csv.gz"))
    if not files:
        sys.exit(f"No raw files in {RAW_DIR}. Run scripts/01_download.py first.")
    return files


def stream_rows():
    for path in raw_files():
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            yield from csv.DictReader(fh)


def write_csv(name: str, fieldnames: list[str], rows) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(OUT_DIR / name, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)
            n += 1
    return n


# ---------------------------------------------------------------- pass 1: dimensions


def build_dimensions():
    vendors: dict[str, dict] = {}
    accounts: dict[str, dict] = {}
    departments: dict[str, dict] = {}
    funds: dict[str, dict] = {}
    programs: dict[str, str] = {}
    vendor_cat: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    max_date = ""
    total = 0

    for r in stream_rows():
        total += 1
        name = (r.get("vendor") or "").strip()
        if not name:
            continue

        d = iso_date(r.get("data_as_of", ""))
        max_date = max(max_date, d)
        amt = money(r.get("vouchers_paid", "")) + money(r.get("vouchers_pending", "")) + money(
            r.get("vouchers_pending_retainage", "")
        )

        v = vendors.get(name)
        if v is None:
            v = vendors[name] = {
                "vendor_name": name,
                "is_nonprofit": "N",
                "first_payment_date": d,
                "last_payment_date": d,
                "total_amount": 0.0,
                "transaction_count": 0,
            }
        if (r.get("non_profit_indicator") or "").strip().upper().startswith("Y"):
            v["is_nonprofit"] = "Y"
        if d:
            if not v["first_payment_date"] or d < v["first_payment_date"]:
                v["first_payment_date"] = d
            if d > v["last_payment_date"]:
                v["last_payment_date"] = d
        v["total_amount"] += amt
        v["transaction_count"] += 1
        if r.get("character"):
            vendor_cat[name][r["character"]] += 1

        sub = (r.get("sub_object_code") or "").strip()
        if sub and sub not in accounts:
            accounts[sub] = {
                "account_code": sub,
                "account_name": (r.get("sub_object") or "").strip(),
                "object_code": (r.get("object_code") or "").strip(),
                "object_name": (r.get("object") or "").strip(),
                "category_code": (r.get("character_code") or "").strip(),
                "category_name": (r.get("character") or "").strip(),
                "account_type": "Expense",
            }

        dep = (r.get("department_code") or "").strip()
        if dep and dep not in departments:
            departments[dep] = {
                "department_code": dep,
                "department_name": (r.get("department") or "").strip(),
                "org_group_code": (r.get("organization_group_code") or "").strip(),
                "org_group_name": (r.get("organization_group") or "").strip(),
            }

        fnd = (r.get("fund_code") or "").strip()
        if fnd and fnd not in funds:
            funds[fnd] = {
                "fund_code": fnd,
                "fund_name": (r.get("fund") or "").strip(),
                "fund_type_code": (r.get("fund_type_code") or "").strip(),
                "fund_type_name": (r.get("fund_type") or "").strip(),
                "fund_category_code": (r.get("fund_category_code") or "").strip(),
                "fund_category_name": (r.get("fund_category") or "").strip(),
            }

        pcode = (r.get("program_code") or "").strip()
        if pcode and pcode not in programs:
            programs[pcode] = (r.get("program") or "").strip()

    # stable surrogate ids from alphabetical order
    vendor_ids: dict[str, str] = {}
    for i, name in enumerate(sorted(vendors), start=1):
        vendor_ids[name] = f"V{i:05d}"
        v = vendors[name]
        v["vendor_id"] = vendor_ids[name]
        v["total_amount"] = round(v["total_amount"], 2)
        cats = vendor_cat.get(name)
        v["primary_category"] = max(cats.items(), key=lambda kv: kv[1])[0] if cats else ""
        # payment terms vary by vendor - part of the synthesized AP layer
        v["payment_terms"] = pick(
            "terms", name, ["Net 30", "Net 45", "Net 15", "Net 60", "Due on Receipt"],
            [0.54, 0.16, 0.14, 0.11, 0.05],
        )

    return {
        "vendors": vendors,
        "vendor_ids": vendor_ids,
        "accounts": accounts,
        "departments": departments,
        "funds": funds,
        "programs": programs,
        "max_date": max_date,
        "total_rows": total,
    }


# ---------------------------------------------------------------- reconciliation model

RECON_FIELDS = [
    "reconciliation_id",
    "transaction_id",
    "voucher_id",
    "vendor_id",
    "transaction_date",
    "ledger_amount",
    "bank_amount",
    "variance",
    "reconciliation_status",
    "bank_statement_ref",
    "matched_date",
    "days_outstanding",
    "exception_reason",
]


def reconcile(txn_id: str, voucher: str, vendor_id: str, txn_date: str, amount: float,
              as_of: date) -> dict:
    """Deterministic synthetic reconciliation state for one transaction."""
    try:
        d = datetime.strptime(txn_date, "%Y-%m-%d").date()
        age = (as_of - d).days
    except ValueError:
        d, age = as_of, 0

    # older items are far more likely to have cleared
    if age < 15:
        p_ok = 0.55
    elif age < 45:
        p_ok = 0.82
    elif age < 90:
        p_ok = 0.93
    else:
        p_ok = 0.965
    # very large payments get extra scrutiny and clear more slowly
    if abs(amount) >= 1_000_000:
        p_ok -= 0.10
    elif abs(amount) >= 250_000:
        p_ok -= 0.04
    # credits / reversals are messier
    if amount < 0:
        p_ok -= 0.15

    roll = unit("recon", txn_id)
    ref = f"BNK-{d.year}{d.month:02d}-{_u64('stmt', txn_id) % 1_000_000:06d}"

    if roll < p_ok:
        lag = 1 + int(unit("lag", txn_id) * 9)
        matched = min(as_of, date.fromordinal(d.toordinal() + lag))
        return {
            "reconciliation_id": f"R{txn_id[1:]}",
            "transaction_id": txn_id,
            "voucher_id": voucher,
            "vendor_id": vendor_id,
            "transaction_date": txn_date,
            "ledger_amount": amount,
            "bank_amount": amount,
            "variance": 0.0,
            "reconciliation_status": "Reconciled",
            "bank_statement_ref": ref,
            "matched_date": matched.isoformat(),
            "days_outstanding": 0,
            "exception_reason": "",
        }

    # unmatched bucket - split into three realistic failure modes
    kind = pick(
        "kind", txn_id,
        ["Unreconciled", "Partially Matched", "Disputed"],
        [0.62, 0.26, 0.12],
    )

    if kind == "Partially Matched":
        # small over/under payment against the bank line
        drift = round(amount * (0.001 + unit("drift", txn_id) * 0.02), 2)
        if unit("sign", txn_id) < 0.5:
            drift = -drift
        bank = round(amount + drift, 2)
        reason = pick(
            "why_pm", txn_id,
            ["Amount mismatch", "FX rounding difference", "Bank fee deducted", "Partial settlement"],
            [0.42, 0.18, 0.22, 0.18],
        )
        return {
            "reconciliation_id": f"R{txn_id[1:]}",
            "transaction_id": txn_id,
            "voucher_id": voucher,
            "vendor_id": vendor_id,
            "transaction_date": txn_date,
            "ledger_amount": amount,
            "bank_amount": bank,
            "variance": round(bank - amount, 2),
            "reconciliation_status": "Partially Matched",
            "bank_statement_ref": ref,
            "matched_date": "",
            "days_outstanding": age,
            "exception_reason": reason,
        }

    if kind == "Disputed":
        reason = pick(
            "why_d", txn_id,
            ["Vendor disputes invoice amount", "Duplicate payment suspected",
             "Goods receipt not confirmed", "Contract terms under review"],
            [0.34, 0.24, 0.24, 0.18],
        )
        return {
            "reconciliation_id": f"R{txn_id[1:]}",
            "transaction_id": txn_id,
            "voucher_id": voucher,
            "vendor_id": vendor_id,
            "transaction_date": txn_date,
            "ledger_amount": amount,
            "bank_amount": "",
            "variance": "",
            "reconciliation_status": "Disputed",
            "bank_statement_ref": "",
            "matched_date": "",
            "days_outstanding": age,
            "exception_reason": reason,
        }

    reason = pick(
        "why_u", txn_id,
        ["No matching bank record", "Timing difference - in transit",
         "Payment not yet cleared", "Missing remittance advice"],
        [0.3, 0.3, 0.26, 0.14],
    )
    return {
        "reconciliation_id": f"R{txn_id[1:]}",
        "transaction_id": txn_id,
        "voucher_id": voucher,
        "vendor_id": vendor_id,
        "transaction_date": txn_date,
        "ledger_amount": amount,
        "bank_amount": "",
        "variance": "",
        "reconciliation_status": "Unreconciled",
        "bank_statement_ref": "",
        "matched_date": "",
        "days_outstanding": age,
        "exception_reason": reason,
    }


# ---------------------------------------------------------------- pass 2: facts

TXN_FIELDS = [
    "transaction_id", "voucher_id", "transaction_date", "fiscal_year",
    "vendor_id", "vendor_name", "account_code", "account_name", "category_name",
    "department_code", "department_name", "fund_code", "fund_name",
    "program_code", "program_name", "purchase_order", "contract_number", "contract_title",
    "amount_paid", "amount_pending", "amount_retainage", "amount_total", "payment_status",
]

PAYOUT_FIELDS = [
    "payout_id", "vendor_id", "vendor_name", "payout_date", "fiscal_year",
    "transaction_count", "department_count", "gross_amount", "paid_amount",
    "pending_amount", "retainage_amount", "payment_method", "payout_status",
]


def build_facts(dims: dict):
    vendor_ids = dims["vendor_ids"]
    programs = dims["programs"]
    as_of = datetime.strptime(dims["max_date"], "%Y-%m-%d").date()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payouts: dict[tuple[str, str], dict] = {}
    status_counts: dict[str, int] = defaultdict(int)
    recon_counts: dict[str, int] = defaultdict(int)
    n_txn = 0

    tf = open(OUT_DIR / "transactions.csv", "w", encoding="utf-8", newline="")
    rf = open(OUT_DIR / "reconciliation.csv", "w", encoding="utf-8", newline="")
    tw = csv.DictWriter(tf, fieldnames=TXN_FIELDS)
    rw = csv.DictWriter(rf, fieldnames=RECON_FIELDS)
    tw.writeheader()
    rw.writeheader()

    for i, r in enumerate(stream_rows(), start=1):
        name = (r.get("vendor") or "").strip()
        if not name:
            continue
        vid = vendor_ids[name]
        txn_id = f"T{i:07d}"
        d = iso_date(r.get("data_as_of", ""))

        paid = money(r.get("vouchers_paid", ""))
        pending = money(r.get("vouchers_pending", ""))
        retain = money(r.get("vouchers_pending_retainage", ""))
        total = round(paid + pending + retain, 2)

        if pending > 0:
            status = "Pending"
        elif retain > 0:
            status = "Retainage Held"
        elif paid < 0:
            status = "Reversed"
        else:
            status = "Paid"
        status_counts[status] += 1

        pcode = (r.get("program_code") or "").strip()
        tw.writerow({
            "transaction_id": txn_id,
            "voucher_id": (r.get("voucher") or "").strip(),
            "transaction_date": d,
            "fiscal_year": (r.get("fiscal_year") or "").strip(),
            "vendor_id": vid,
            "vendor_name": name,
            "account_code": (r.get("sub_object_code") or "").strip(),
            "account_name": (r.get("sub_object") or "").strip(),
            "category_name": (r.get("character") or "").strip(),
            "department_code": (r.get("department_code") or "").strip(),
            "department_name": (r.get("department") or "").strip(),
            "fund_code": (r.get("fund_code") or "").strip(),
            "fund_name": (r.get("fund") or "").strip(),
            "program_code": pcode,
            "program_name": programs.get(pcode, ""),
            "purchase_order": (r.get("purchase_order") or "").strip(),
            "contract_number": (r.get("contract_number") or "").strip(),
            "contract_title": (r.get("contract_title") or "").strip(),
            "amount_paid": paid,
            "amount_pending": pending,
            "amount_retainage": retain,
            "amount_total": total,
            "payment_status": status,
        })
        n_txn += 1

        rec = reconcile(txn_id, (r.get("voucher") or "").strip(), vid, d, total, as_of)
        rw.writerow(rec)
        recon_counts[rec["reconciliation_status"]] += 1

        key = (vid, d)
        p = payouts.get(key)
        if p is None:
            p = payouts[key] = {
                "vendor_id": vid, "vendor_name": name, "payout_date": d,
                "fiscal_year": (r.get("fiscal_year") or "").strip(),
                "transaction_count": 0, "_depts": set(),
                "gross_amount": 0.0, "paid_amount": 0.0,
                "pending_amount": 0.0, "retainage_amount": 0.0,
            }
        p["transaction_count"] += 1
        p["_depts"].add((r.get("department_code") or "").strip())
        p["gross_amount"] += total
        p["paid_amount"] += paid
        p["pending_amount"] += pending
        p["retainage_amount"] += retain

    tf.close()
    rf.close()

    def payout_rows():
        for n, (key, p) in enumerate(sorted(payouts.items()), start=1):
            pid = f"P{n:07d}"
            if p["pending_amount"] > 0:
                st = "Pending"
            elif p["retainage_amount"] > 0:
                st = "Retainage Held"
            elif p["paid_amount"] < 0:
                st = "Reversed"
            else:
                st = "Paid"
            yield {
                "payout_id": pid,
                "vendor_id": p["vendor_id"],
                "vendor_name": p["vendor_name"],
                "payout_date": p["payout_date"],
                "fiscal_year": p["fiscal_year"],
                "transaction_count": p["transaction_count"],
                "department_count": len(p["_depts"]),
                "gross_amount": round(p["gross_amount"], 2),
                "paid_amount": round(p["paid_amount"], 2),
                "pending_amount": round(p["pending_amount"], 2),
                "retainage_amount": round(p["retainage_amount"], 2),
                "payment_method": pick(
                    "method", pid, ["ACH", "Check", "Wire", "Virtual Card"],
                    [0.62, 0.22, 0.12, 0.04],
                ),
                "payout_status": st,
            }

    n_payouts = write_csv("vendor_payouts.csv", PAYOUT_FIELDS, payout_rows())
    return n_txn, n_payouts, status_counts, recon_counts, as_of


# ---------------------------------------------------------------- dimension output


def write_dimensions(dims: dict):
    v = write_csv(
        "vendors.csv",
        ["vendor_id", "vendor_name", "is_nonprofit", "primary_category", "payment_terms",
         "first_payment_date", "last_payment_date", "transaction_count", "total_amount"],
        (dims["vendors"][n] for n in sorted(dims["vendors"])),
    )
    a = write_csv(
        "chart_of_accounts.csv",
        ["account_code", "account_name", "object_code", "object_name",
         "category_code", "category_name", "account_type"],
        (dims["accounts"][k] for k in sorted(dims["accounts"])),
    )
    d = write_csv(
        "departments.csv",
        ["department_code", "department_name", "org_group_code", "org_group_name"],
        (dims["departments"][k] for k in sorted(dims["departments"])),
    )
    f = write_csv(
        "funds.csv",
        ["fund_code", "fund_name", "fund_type_code", "fund_type_name",
         "fund_category_code", "fund_category_name"],
        (dims["funds"][k] for k in sorted(dims["funds"])),
    )
    return v, a, d, f


# ---------------------------------------------------------------- sqlite


SCHEMA = """
DROP TABLE IF EXISTS transactions;
DROP TABLE IF EXISTS reconciliation;
DROP TABLE IF EXISTS vendor_payouts;
DROP TABLE IF EXISTS vendors;
DROP TABLE IF EXISTS chart_of_accounts;
DROP TABLE IF EXISTS departments;
DROP TABLE IF EXISTS funds;

CREATE TABLE vendors (
    vendor_id TEXT PRIMARY KEY, vendor_name TEXT NOT NULL, is_nonprofit TEXT,
    primary_category TEXT, payment_terms TEXT, first_payment_date TEXT,
    last_payment_date TEXT, transaction_count INTEGER, total_amount REAL
);
CREATE TABLE chart_of_accounts (
    account_code TEXT PRIMARY KEY, account_name TEXT, object_code TEXT, object_name TEXT,
    category_code TEXT, category_name TEXT, account_type TEXT
);
CREATE TABLE departments (
    department_code TEXT PRIMARY KEY, department_name TEXT,
    org_group_code TEXT, org_group_name TEXT
);
CREATE TABLE funds (
    fund_code TEXT PRIMARY KEY, fund_name TEXT, fund_type_code TEXT,
    fund_type_name TEXT, fund_category_code TEXT, fund_category_name TEXT
);
CREATE TABLE transactions (
    transaction_id TEXT PRIMARY KEY, voucher_id TEXT, transaction_date TEXT,
    fiscal_year TEXT, vendor_id TEXT, vendor_name TEXT, account_code TEXT,
    account_name TEXT, category_name TEXT, department_code TEXT, department_name TEXT,
    fund_code TEXT, fund_name TEXT, program_code TEXT, program_name TEXT,
    purchase_order TEXT, contract_number TEXT, contract_title TEXT,
    amount_paid REAL, amount_pending REAL, amount_retainage REAL,
    amount_total REAL, payment_status TEXT
);
CREATE TABLE reconciliation (
    reconciliation_id TEXT PRIMARY KEY, transaction_id TEXT, voucher_id TEXT,
    vendor_id TEXT, transaction_date TEXT, ledger_amount REAL, bank_amount REAL,
    variance REAL, reconciliation_status TEXT, bank_statement_ref TEXT,
    matched_date TEXT, days_outstanding INTEGER, exception_reason TEXT
);
CREATE TABLE vendor_payouts (
    payout_id TEXT PRIMARY KEY, vendor_id TEXT, vendor_name TEXT, payout_date TEXT,
    fiscal_year TEXT, transaction_count INTEGER, department_count INTEGER,
    gross_amount REAL, paid_amount REAL, pending_amount REAL, retainage_amount REAL,
    payment_method TEXT, payout_status TEXT
);
"""

INDEXES = """
CREATE INDEX idx_txn_date       ON transactions(transaction_date);
CREATE INDEX idx_txn_vendor     ON transactions(vendor_id);
CREATE INDEX idx_txn_vendorname ON transactions(vendor_name);
CREATE INDEX idx_txn_dept       ON transactions(department_code);
CREATE INDEX idx_txn_account    ON transactions(account_code);
CREATE INDEX idx_txn_status     ON transactions(payment_status);
CREATE INDEX idx_txn_cat        ON transactions(category_name);
CREATE INDEX idx_rec_status     ON reconciliation(reconciliation_status);
CREATE INDEX idx_rec_txn        ON reconciliation(transaction_id);
CREATE INDEX idx_rec_date       ON reconciliation(transaction_date);
CREATE INDEX idx_pay_vendor     ON vendor_payouts(vendor_id);
CREATE INDEX idx_pay_date       ON vendor_payouts(payout_date);
CREATE INDEX idx_ven_name       ON vendors(vendor_name);

CREATE VIEW v_unreconciled AS
SELECT t.transaction_id, t.voucher_id, t.transaction_date, t.vendor_name,
       t.department_name, t.account_name, t.amount_total,
       r.reconciliation_status, r.days_outstanding, r.exception_reason, r.variance
FROM transactions t JOIN reconciliation r USING (transaction_id)
WHERE r.reconciliation_status <> 'Reconciled';

CREATE VIEW v_monthly_spend AS
SELECT substr(transaction_date,1,7) AS month, vendor_id, vendor_name,
       category_name, department_name,
       COUNT(*) AS txn_count, ROUND(SUM(amount_total),2) AS total_amount
FROM transactions GROUP BY 1,2,3,4,5;
"""

TABLE_FILES = {
    "vendors": "vendors.csv",
    "chart_of_accounts": "chart_of_accounts.csv",
    "departments": "departments.csv",
    "funds": "funds.csv",
    "transactions": "transactions.csv",
    "reconciliation": "reconciliation.csv",
    "vendor_payouts": "vendor_payouts.csv",
}

NUMERIC = {
    "transaction_count", "department_count", "days_outstanding", "total_amount",
    "amount_paid", "amount_pending", "amount_retainage", "amount_total",
    "ledger_amount", "bank_amount", "variance", "gross_amount", "paid_amount",
    "pending_amount", "retainage_amount",
}


def build_sqlite():
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)

    for table, fname in TABLE_FILES.items():
        path = OUT_DIR / fname
        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            cols = reader.fieldnames or []
            sql = (f"INSERT INTO {table} ({','.join(cols)}) "
                   f"VALUES ({','.join('?' * len(cols))})")

            def rows():
                for r in reader:
                    out = []
                    for c in cols:
                        val = r[c]
                        if c in NUMERIC:
                            out.append(float(val) if val not in ("", None) else None)
                        else:
                            out.append(val)
                    yield out

            con.executemany(sql, rows())
        con.commit()

    con.executescript(INDEXES)
    con.commit()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLE_FILES}
    con.execute("ANALYZE")
    con.commit()
    con.close()
    return counts


# ---------------------------------------------------------------- main


def main() -> int:
    print("Pass 1/3  building dimensions ...", flush=True)
    dims = build_dimensions()
    nv, na, nd, nf = write_dimensions(dims)
    print(f"  vendors={nv:,}  accounts={na:,}  departments={nd:,}  funds={nf:,}", flush=True)

    print("Pass 2/3  building transactions, payouts, reconciliation ...", flush=True)
    n_txn, n_pay, status_counts, recon_counts, as_of = build_facts(dims)
    print(f"  transactions={n_txn:,}  payouts={n_pay:,}  as_of={as_of}", flush=True)

    print("Pass 3/3  loading SQLite ...", flush=True)
    counts = build_sqlite()

    print("\n--- payment_status ---")
    for k, v in sorted(status_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<16} {v:>9,}  {v / n_txn:6.2%}")
    print("--- reconciliation_status ---")
    for k, v in sorted(recon_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<18} {v:>9,}  {v / n_txn:6.2%}")
    print("--- sqlite row counts ---")
    for k, v in counts.items():
        print(f"  {k:<20} {v:>9,}")
    print(f"\ndb: {DB_PATH}  ({DB_PATH.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
