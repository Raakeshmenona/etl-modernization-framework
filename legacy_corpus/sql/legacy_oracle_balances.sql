-- LEGACY: Oracle 11g positive-balance extract feeding the daily balance mart.
-- Dialect features that do not survive a naive lift-and-shift:
--   * NVL             -> COALESCE
--   * DECODE          -> CASE
--   * (+) outer join  -> ANSI LEFT JOIN (and the join side is easy to flip)
--   * TO_DATE/TO_CHAR -> date parsing with an explicit format model
--
-- inputs : accounts, customers
-- output : account_id, customer_last_name, product_name, balance, activity_month

SELECT  a.account_id                                            AS account_id,
        NVL(c.last_name, 'UNKNOWN')                             AS customer_last_name,
        DECODE(a.product_code,
               'CHK', 'CHECKING',
               'SAV', 'SAVINGS',
               'MMK', 'MONEY_MARKET',
                      'OTHER')                                  AS product_name,
        a.balance                                               AS balance,
        TO_CHAR(TO_DATE(a.last_activity_date, 'YYYY-MM-DD'),
                'YYYY-MM')                                      AS activity_month
FROM    accounts  a,
        customers c
WHERE   a.customer_id = c.customer_id(+)
AND     a.balance     > 0
ORDER BY a.account_id;
