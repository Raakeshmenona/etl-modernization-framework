"""Reference for sql/legacy_teradata_ranking.sql (Teradata semantics)."""
from __future__ import annotations

from typing import Dict

import pandas as pd


def run(inputs: Dict[str, str]) -> str:
    txn = pd.read_csv(inputs["transactions"], dtype=str)
    txn["amount"] = txn["amount"].astype(float)
    txn["customer_id"] = txn["customer_id"].astype(int)

    grouped = (txn.groupby(["customer_id", "currency"], as_index=False)
                  .agg(txn_count=("amount", "size"), total_amount=("amount", "sum")))

    # RANK() OVER (PARTITION BY currency ORDER BY SUM(amount) DESC).
    # method="min" is RANK, not DENSE_RANK: ties share an ordinal and consume
    # the ones that follow.
    grouped["currency_rank"] = (grouped.groupby("currency")["total_amount"]
                                       .rank(method="min", ascending=False)
                                       .astype(int))

    grouped = grouped[grouped["currency_rank"] <= 5]              # QUALIFY
    grouped = grouped.sort_values(["currency", "currency_rank", "customer_id"],
                                  kind="mergesort")

    out = pd.DataFrame({
        "customer_id": grouped["customer_id"],
        "currency": grouped["currency"],
        "txn_count": grouped["txn_count"],
        "total_amount": grouped["total_amount"].map(lambda v: "%.2f" % v),
        "currency_rank": grouped["currency_rank"],
    })
    return out.to_csv(index=False)
