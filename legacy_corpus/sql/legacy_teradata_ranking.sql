-- LEGACY: Teradata top-N customers by spend within each currency.
-- Dialect features that do not survive a naive lift-and-shift:
--   * SEL          -> SELECT
--   * QUALIFY      -> supported in Snowflake, but NOT in Spark SQL or Postgres,
--                     where it has to become a windowed sub-select
--   * RANK()       -> ties share a rank and consume the following ordinals;
--                     swapping in ROW_NUMBER() silently changes the row count
--
-- inputs : transactions
-- output : customer_id, currency, txn_count, total_amount, currency_rank

SEL     t.customer_id                                   AS customer_id,
        t.currency                                      AS currency,
        COUNT(*)                                        AS txn_count,
        SUM(t.amount)                                   AS total_amount,
        RANK() OVER (PARTITION BY t.currency
                     ORDER BY SUM(t.amount) DESC)       AS currency_rank
FROM    transactions t
GROUP BY t.customer_id, t.currency
QUALIFY currency_rank <= 5
ORDER BY t.currency, currency_rank, t.customer_id;
