"""Deterministic synthetic inputs for the legacy ETL corpus.

Everything here is seeded so the equivalence scores in the README are
reproducible. The generator deliberately plants the edge cases that break
naive Perl -> Python translation:

  * customer ids that sort differently as strings vs integers
  * DD/MM/YYYY dates where day > 12 for only *some* rows
  * a currency with no FX rate (undef -> 0 in Perl's numeric context)
  * pipe-delimited records with trailing empty fields (Perl's split drops them)
  * zero and negative balances that hit the `unless`/`eq` branches
"""
from __future__ import annotations

import csv
import logging
import random
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SEED = 42
SAMPLE_DIR = Path(__file__).resolve().parent / "sample"

CURRENCIES = ["USD", "EUR", "GBP", "INR", "JPY", "CHF"]
# CHF is deliberately absent from fx_rates.csv -> exercises the undef-rate path.
RATED_CURRENCIES = ["USD", "EUR", "GBP", "INR", "JPY"]
RATES = {"USD": 1.0, "EUR": 1.0850, "GBP": 1.2710, "INR": 0.0120, "JPY": 0.0064}
CHANNELS = ["BRANCH", "ATM", "ONLINE", "MOBILE"]
PRODUCTS = ["CHK", "SAV", "MMK", "LOC"]

FIRST_NAMES = ["ravi", "Priya", "JOHN", "aisha", "Mikael", "sofia", "Chen", "Amara",
               "Dmitri", "Lucia", "Kwame", "Yuki", "Tomas", "Nadia", "Omar"]
LAST_NAMES = ["sharma", "OKAFOR", "Lindqvist", "  Rossi ", "Zhang", "de Souza",
              "Mbeki", "Tanaka", "Novak", "Haddad", "Petrov", "Nakamura"]

N_CUSTOMERS = 300
N_TRANSACTIONS = 800
N_FEED_RECORDS = 500
# Share of raw-feed records that end in one or more empty fields. This is the
# population the `split` trailing-empty-field defect shows up on.
TRAILING_EMPTY_RATE = 0.12


def _write(name: str, header: list, rows: list, delimiter: str = ",") -> Path:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLE_DIR / name
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter=delimiter, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    logger.info("wrote %-24s %4d rows", name, len(rows))
    return path


def gen_customers(rng: random.Random) -> list:
    rows = []
    for cid in range(1, N_CUSTOMERS + 1):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        dob = date(1955, 1, 1) + timedelta(days=rng.randrange(0, 365 * 50))
        rows.append([
            cid,                                   # bare integer: "10" < "9" as a string
            first,
            last,
            dob.isoformat(),
            "%s.%s@example.com" % (first.strip().lower(), last.strip().lower().replace(" ", "")),
            "+1-555-%04d" % rng.randrange(0, 10000),
            rng.choice(["ACTIVE", "ACTIVE", "ACTIVE", "CLOSED"]),
        ])

    # Plant exact duplicates (same normalised name + dob, different id and
    # different casing/whitespace) so the dedupe job has something to collapse.
    for src in rng.sample(rows, 25):
        cid = max(int(r[0]) for r in rows) + 1
        rows.append([
            cid,
            "  " + str(src[1]).upper() + " ",
            str(src[2]).lower() + "  ",
            src[3],
            src[4],
            src[5],
            src[6],
        ])
    return rows


def gen_transactions(rng: random.Random) -> list:
    rows = []
    for i in range(1, N_TRANSACTIONS + 1):
        d = date(2026, 1, 1) + timedelta(days=rng.randrange(0, 180))
        rows.append([
            "TXN%06d" % i,
            rng.randrange(1, N_CUSTOMERS + 1),
            d.strftime("%d/%m/%Y"),          # DD/MM/YYYY - the classic trap
            "%.2f" % round(rng.uniform(5.0, 9500.0), 2),
            rng.choice(CURRENCIES),
            rng.choice(CHANNELS),
        ])
    return rows


def gen_accounts(rng: random.Random) -> list:
    rows = []
    ref = date(2026, 7, 1)
    for i in range(1, N_CUSTOMERS + 1):
        roll = rng.random()
        if roll < 0.10:
            balance = -round(rng.uniform(10, 4000), 2)     # overdrawn
        elif roll < 0.20:
            balance = 0.00                                  # hits the `unless` branch
        else:
            balance = round(rng.uniform(1, 250000), 2)
        last_activity = ref - timedelta(days=rng.randrange(0, 400))
        rows.append([
            "ACC%05d" % i,
            i,
            "%.2f" % balance,
            last_activity.isoformat(),
            rng.choice(PRODUCTS),
            rng.choice(["Y", "N", "N", "N"]),
        ])
    return rows


def gen_fx_rates() -> list:
    return [[cur, "%.4f" % RATES[cur]] for cur in RATED_CURRENCIES]


def gen_raw_feed(rng: random.Random) -> list:
    """Pipe-delimited nightly feed with ragged, partly-empty records."""
    rows = []
    for i in range(1, N_FEED_RECORDS + 1):
        record = [
            "REC%05d" % i,
            rng.choice(["RETAIL", "CORP", "WEALTH"]),
            "%.2f" % round(rng.uniform(0, 50000), 2),
            rng.choice(["GBP", "USD", "EUR"]),
            rng.choice(["OK", "PENDING", ""]),
            rng.choice(["N", "Y"]),
            "2026-06-%02d" % rng.randrange(1, 29),
        ]
        # Blank out interior fields sometimes (both languages agree on these).
        for idx in rng.sample(range(1, 6), rng.randrange(0, 3)):
            record[idx] = ""
        # Blank out a *trailing* run sometimes - Perl's split drops these,
        # str.split() does not. This is the planted defect.
        if rng.random() < TRAILING_EMPTY_RATE:
            for idx in range(len(record) - rng.randrange(1, 4), len(record)):
                record[idx] = ""
        rows.append(record)
    return rows


def main() -> None:
    rng = random.Random(SEED)
    _write("customers.csv",
           ["customer_id", "first_name", "last_name", "dob", "email", "phone", "status"],
           gen_customers(rng))
    _write("transactions.csv",
           ["txn_id", "customer_id", "txn_date", "amount", "currency", "channel"],
           gen_transactions(rng))
    _write("accounts.csv",
           ["account_id", "customer_id", "balance", "last_activity_date",
            "product_code", "overdraft_flag"],
           gen_accounts(rng))
    _write("fx_rates.csv", ["currency", "rate_to_usd"], gen_fx_rates())
    _write("raw_feed.psv",
           ["record_id", "segment", "amount", "currency", "status", "flag", "as_of_date"],
           gen_raw_feed(rng), delimiter="|")
    logger.info("sample data written to %s", SAMPLE_DIR)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
