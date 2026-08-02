"""Reference for perl/account_status_derive.pl."""
from __future__ import annotations

from typing import Dict

from equivalence.perl_semantics import days_between, perl_num, read_delimited

REF_DATE = "2026-07-01"
DORMANT_DAYS = 90


def run(inputs: Dict[str, str]) -> str:
    out = ["account_id,status,tier,days_since_activity"]

    for f in read_delimited(inputs["accounts"])[1:]:
        account_id, balance, last_activity, overdraft = f[0], f[2], f[3], f[5]

        days = days_between(last_activity, REF_DATE)
        bal = perl_num(balance)

        if bal < 0:
            # `eq`, not `==`: 'Y' == 0 is true in Perl and would flag every row.
            status = "OVERDRAWN_AUTHORISED" if overdraft == "Y" else "OVERDRAWN_UNAUTHORISED"
            tier = "NONE"
        elif bal == 0:
            # unless ($days <= 90) { DORMANT } else { ACTIVE_ZERO }
            status = "DORMANT" if not (days <= DORMANT_DAYS) else "ACTIVE_ZERO"
            tier = "NONE"
        else:
            status = "DORMANT" if days > 365 else "ACTIVE"
            if bal < 1000:
                tier = "STANDARD"
            elif bal < 10000:
                tier = "PREMIUM"
            else:
                tier = "PRIVATE"

        out.append("%s,%s,%s,%d" % (account_id, status, tier, days))

    return "\n".join(out) + "\n"
