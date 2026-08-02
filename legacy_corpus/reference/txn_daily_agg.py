"""Reference for perl/txn_daily_agg.pl."""
from __future__ import annotations

from typing import Dict

from equivalence.perl_semantics import perl_num, perl_sort, perl_split, read_delimited


def run(inputs: Dict[str, str]) -> str:
    rows = read_delimited(inputs["transactions"])
    body = rows[1:]

    amount: Dict[str, float] = {}
    count: Dict[str, int] = {}

    for f in body:
        txn_date, amt, currency = f[2], f[3], f[4]

        # DD/MM/YYYY, not MM/DD/YYYY.
        dd, mm, yyyy = perl_split(r"/", txn_date, -1)[:3]
        iso = "%04d-%02d-%02d" % (int(yyyy), int(mm), int(dd))

        key = "|".join([iso, currency])
        # Accumulated in file order so the floating-point rounding path is
        # identical to the Perl run.
        amount[key] = amount.get(key, 0.0) + perl_num(amt)
        count[key] = count.get(key, 0) + 1

    out = ["settlement_date,currency,txn_count,total_amount"]
    for key in perl_sort(amount.keys()):
        iso, currency = perl_split(r"\|", key, -1)[:2]
        out.append("%s,%s,%d,%.2f" % (iso, currency, count[key], amount[key]))
    return "\n".join(out) + "\n"
