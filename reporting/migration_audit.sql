-- Snowflake audit layer for the migration programme.
-- Deploy once per environment:
--   snowsql -f reporting/migration_audit.sql
--
-- Design notes
--   * EQUIVALENCE_RUN is append-only. There is no UPDATE path and no DELETE
--     grant. A re-scored unit produces a NEW row, so the history of a
--     translation that took four attempts is visible rather than overwritten.
--   * Provenance columns (TRANSLATION_MODEL, LEGACY_RUNNER, SCORER_VERSION,
--     SCORE_COMPONENTS) are not optional. A score without them cannot be
--     re-derived, and an audit trail you cannot re-derive is decoration.

CREATE DATABASE IF NOT EXISTS MIGRATION_AUDIT;
CREATE SCHEMA   IF NOT EXISTS MIGRATION_AUDIT.PUBLIC;

USE SCHEMA MIGRATION_AUDIT.PUBLIC;

-- ------------------------------------------------------------------------ --
-- One row per unit per scored run.
-- ------------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS EQUIVALENCE_RUN (
    RUN_ID                VARCHAR(64)    NOT NULL,
    CAPTURED_AT           TIMESTAMP_NTZ  NOT NULL,
    UNIT_NAME             VARCHAR(128)   NOT NULL,
    OWNER                 VARCHAR(128),
    CRITICALITY           VARCHAR(16),
    LANGUAGE              VARCHAR(16),
    SOURCE_PATH           VARCHAR(512),

    EQUIVALENCE_SCORE     NUMBER(7,4)    NOT NULL,
    THRESHOLD             NUMBER(7,4)    NOT NULL,
    PASSED                BOOLEAN        NOT NULL,
    PROMOTED              BOOLEAN        NOT NULL DEFAULT FALSE,
    SCORE_COMPONENTS      VARIANT,
    FINDINGS              VARIANT,
    REGRESSION_CASES      NUMBER(6,0)    DEFAULT 0,

    -- Provenance. Drop these and the score stops being evidence.
    TRANSLATION_MODEL     VARCHAR(64),
    TRANSLATION_SOURCE    VARCHAR(32),   -- cache | generated
    LEGACY_RUNNER         VARCHAR(64),   -- perl-subprocess | golden-replay
    LEGACY_AUTHORITATIVE  BOOLEAN,       -- FALSE means a replayed snapshot
    SCORER_VERSION        VARCHAR(16),
    EXECUTED_ON           VARCHAR(128),
    REVIEWER              VARCHAR(128),

    CONSTRAINT PK_EQUIVALENCE_RUN PRIMARY KEY (RUN_ID, UNIT_NAME)
)
CLUSTER BY (UNIT_NAME, CAPTURED_AT)
COMMENT = 'Append-only equivalence scores. One row per unit per run.';

-- ------------------------------------------------------------------------ --
-- Promotion events. Separate from scoring: a unit can score 100% and still
-- not be promoted because its upstream dependency is blocked.
-- ------------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS UNIT_PROMOTION (
    RUN_ID        VARCHAR(64)   NOT NULL,
    UNIT_NAME     VARCHAR(128)  NOT NULL,
    PROMOTED_AT   TIMESTAMP_NTZ NOT NULL,
    PROMOTED_BY   VARCHAR(128),
    ROLLED_BACK_AT TIMESTAMP_NTZ,
    ROLLBACK_REASON VARCHAR(1024),

    CONSTRAINT PK_UNIT_PROMOTION PRIMARY KEY (RUN_ID, UNIT_NAME)
)
COMMENT = 'Promotion and rollback events per translation unit.';

-- ======================================================================== --
-- Reporting views consumed by Power BI
-- ======================================================================== --

-- Latest score per unit. The dashboard's primary table.
CREATE OR REPLACE VIEW V_UNIT_CURRENT_STATE AS
WITH ranked AS (
    SELECT r.*,
           ROW_NUMBER() OVER (PARTITION BY r.UNIT_NAME
                              ORDER BY r.CAPTURED_AT DESC) AS rn
    FROM EQUIVALENCE_RUN r
)
SELECT  ranked.UNIT_NAME,
        ranked.OWNER,
        ranked.CRITICALITY,
        ranked.LANGUAGE,
        ranked.EQUIVALENCE_SCORE,
        ranked.THRESHOLD,
        ranked.PASSED,
        ranked.CAPTURED_AT                                   AS LAST_SCORED_AT,
        ranked.LEGACY_AUTHORITATIVE,
        ranked.REGRESSION_CASES,
        CASE
            WHEN p.ROLLED_BACK_AT IS NOT NULL THEN 'ROLLED_BACK'
            WHEN p.PROMOTED_AT    IS NOT NULL THEN 'MIGRATED'
            WHEN ranked.PASSED                THEN 'READY_TO_PROMOTE'
            ELSE 'BLOCKED'
        END                                                  AS MIGRATION_STATE
FROM        ranked
LEFT JOIN   UNIT_PROMOTION p
       ON   p.UNIT_NAME = ranked.UNIT_NAME
      AND   p.RUN_ID    = ranked.RUN_ID
WHERE       ranked.rn = 1;

-- Programme-level KPIs. One row; drives the dashboard header cards.
CREATE OR REPLACE VIEW V_MIGRATION_KPI AS
SELECT  COUNT(*)                                                  AS TOTAL_UNITS,
        SUM(IFF(MIGRATION_STATE = 'MIGRATED', 1, 0))              AS MIGRATED_UNITS,
        SUM(IFF(MIGRATION_STATE = 'BLOCKED', 1, 0))               AS BLOCKED_UNITS,
        ROUND(100.0 * SUM(IFF(PASSED, 1, 0)) / NULLIF(COUNT(*), 0), 2)
                                                                  AS AUTO_TRANSLATION_RATE_PCT,
        ROUND(AVG(EQUIVALENCE_SCORE), 3)                          AS MEAN_EQUIVALENCE_SCORE,
        MIN(EQUIVALENCE_SCORE)                                    AS WORST_SCORE,
        -- Effort saved: 40 engineer-hours per script manually, 4 hours of
        -- review when the gate clears it automatically.
        SUM(IFF(PASSED, 36, 0))                                   AS ENGINEER_HOURS_SAVED,
        SUM(IFF(LEGACY_AUTHORITATIVE, 0, 1))                      AS UNITS_ON_REPLAYED_EVIDENCE
FROM    V_UNIT_CURRENT_STATE;

-- Attempt history. How many times each unit had to be re-translated, and
-- whether the score is actually improving.
CREATE OR REPLACE VIEW V_UNIT_ATTEMPT_HISTORY AS
SELECT  UNIT_NAME,
        OWNER,
        COUNT(*)                                              AS ATTEMPTS,
        MIN(CAPTURED_AT)                                      AS FIRST_ATTEMPT_AT,
        MAX(CAPTURED_AT)                                      AS LAST_ATTEMPT_AT,
        MIN(EQUIVALENCE_SCORE)                                AS FIRST_SEEN_WORST,
        MAX(EQUIVALENCE_SCORE)                                AS BEST_SCORE,
        MAX(EQUIVALENCE_SCORE) - MIN(EQUIVALENCE_SCORE)       AS SCORE_IMPROVEMENT,
        DATEDIFF('hour', MIN(CAPTURED_AT), MAX(CAPTURED_AT))  AS HOURS_IN_FLIGHT
FROM    EQUIVALENCE_RUN
GROUP BY UNIT_NAME, OWNER;

-- Which score component is doing the blocking, across the estate. This is the
-- view that tells you whether to fix the prompt or fix the harness.
CREATE OR REPLACE VIEW V_FAILURE_MODE_BREAKDOWN AS
SELECT  UNIT_NAME,
        OWNER,
        CAPTURED_AT,
        EQUIVALENCE_SCORE,
        SCORE_COMPONENTS:schema::FLOAT        AS SCHEMA_COMPONENT,
        SCORE_COMPONENTS:row_set::FLOAT       AS ROW_SET_COMPONENT,
        SCORE_COMPONENTS:cell_value::FLOAT    AS CELL_VALUE_COMPONENT,
        SCORE_COMPONENTS:null_pattern::FLOAT  AS NULL_PATTERN_COMPONENT,
        SCORE_COMPONENTS:ordering::FLOAT      AS ORDERING_COMPONENT,
        f.value::STRING                       AS FINDING
FROM    EQUIVALENCE_RUN,
        LATERAL FLATTEN(INPUT => FINDINGS, OUTER => TRUE) f
WHERE   NOT PASSED;

-- Programme burndown by day, for the trend chart.
CREATE OR REPLACE VIEW V_MIGRATION_BURNDOWN AS
SELECT  TO_DATE(CAPTURED_AT)                                  AS RUN_DATE,
        COUNT(DISTINCT UNIT_NAME)                             AS UNITS_SCORED,
        COUNT(DISTINCT IFF(PASSED, UNIT_NAME, NULL))          AS UNITS_CLEARED,
        ROUND(AVG(EQUIVALENCE_SCORE), 3)                      AS MEAN_SCORE
FROM    EQUIVALENCE_RUN
GROUP BY TO_DATE(CAPTURED_AT)
ORDER BY RUN_DATE;

-- ------------------------------------------------------------------------ --
-- Grants. The writer role cannot UPDATE or DELETE - that is the point of an
-- append-only audit table.
-- ------------------------------------------------------------------------ --
GRANT USAGE  ON DATABASE MIGRATION_AUDIT           TO ROLE MIGRATION_WRITER;
GRANT USAGE  ON SCHEMA   MIGRATION_AUDIT.PUBLIC    TO ROLE MIGRATION_WRITER;
GRANT INSERT ON TABLE    EQUIVALENCE_RUN           TO ROLE MIGRATION_WRITER;
GRANT INSERT ON TABLE    UNIT_PROMOTION            TO ROLE MIGRATION_WRITER;

GRANT USAGE  ON DATABASE MIGRATION_AUDIT           TO ROLE MIGRATION_READER;
GRANT USAGE  ON SCHEMA   MIGRATION_AUDIT.PUBLIC    TO ROLE MIGRATION_READER;
GRANT SELECT ON ALL VIEWS IN SCHEMA MIGRATION_AUDIT.PUBLIC TO ROLE MIGRATION_READER;
