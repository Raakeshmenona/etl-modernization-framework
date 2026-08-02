"""Reference for perl/fx_revenue_calc.pl."""
from __future__ import annotations

from typing import Dict

from equivalence.perl_semantics import perl_num, perl_sort, perl_split, read_delimited

COMMISSION_BPS = 0.015


def run(inputs: Dict[str, str]) -> str:
    rate: Dict[str, str] = {}
    for f in read_delimited(inputs["fx_rates"])[1:]:
        rate[f[0]] = f[1]

    usd: Dict[str, float] = {}
    commission: Dict[str, float] = {}
    count: Dict[str, int] = {}

    for f in read_delimited(inputs["transactions"])[1:]:
        amount, currency, channel = f[3], f[4], f[5]

        # An unrated currency is undef here, which numifies to 0 - the row
        # contributes nothing rather than blowing up or producing NaN.
        converted = perl_num(amount) * perl_num(rate.get(currency))

        key = "|".join([channel, currency])
        usd[key] = usd.get(key, 0.0) + converted
        commission[key] = commission.get(key, 0.0) + converted * COMMISSION_BPS
        count[key] = count.get(key, 0) + 1

    out = ["channel,currency,txn_count,total_usd,total_commission"]
    for key in perl_sort(usd.keys()):
        channel, currency = perl_split(r"\|", key, -1)[:2]
        out.append("%s,%s,%d,%.2f,%.2f"
                   % (channel, currency, count[key], usd[key], commission[key]))
    return "\n".join(out) + "\n"
