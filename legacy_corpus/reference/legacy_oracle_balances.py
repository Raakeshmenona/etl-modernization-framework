"""Reference for sql/legacy_oracle_balances.sql (Oracle 11g semantics)."""
from __future__ import annotations

from typing import Dict

import pandas as pd

PRODUCT_DECODE = {"CHK": "CHECKING", "SAV": "SAVINGS", "MMK": "MONEY_MARKET"}


def run(inputs: Dict[str, str]) -> str:
    accounts = pd.read_csv(inputs["accounts"], dtype=str)
    customers = pd.read_csv(inputs["customers"], dtype=str)

    # a.customer_id = c.customer_id(+)  ->  accounts LEFT JOIN customers
    merged = accounts.merge(
        customers[["customer_id", "last_name"]],
        on="customer_id",
        how="left",
    )

    merged["balance"] = merged["balance"].astype(float)
    merged = merged[merged["balance"] > 0]                       # AND a.balance > 0

    out = pd.DataFrame({
        "account_id": merged["account_id"],
        # NVL(c.last_name, 'UNKNOWN')
        "customer_last_name": merged["last_name"].fillna("UNKNOWN"),
        # DECODE(...) with a default arm
        "product_name": merged["product_code"].map(PRODUCT_DECODE).fillna("OTHER"),
        "balance": merged["balance"].map(lambda v: "%.2f" % v),
        # TO_CHAR(TO_DATE(x,'YYYY-MM-DD'), 'YYYY-MM')
        "activity_month": merged["last_activity_date"].str.slice(0, 7),
    })

    out = out.sort_values("account_id", kind="mergesort")         # ORDER BY a.account_id
    return out.to_csv(index=False)
