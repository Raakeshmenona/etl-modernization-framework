# Power BI — Migration Programme Dashboard

Connects to `MIGRATION_AUDIT.PUBLIC` in Snowflake (see
[`reporting/migration_audit.sql`](../reporting/migration_audit.sql)).

**Connection:** Snowflake connector, **Import** mode with an 8-hourly refresh.
Not DirectQuery — the audit tables are append-only and small (one row per unit
per run), so import costs nothing and the report stays responsive when the
warehouse is suspended. DirectQuery here would wake a warehouse every time
someone opens a page.

**Role:** `MIGRATION_READER` (SELECT on views only). The report must not be
able to write to the audit trail, even accidentally.

## Data model

| Table / view | Role | Grain |
|---|---|---|
| `V_UNIT_CURRENT_STATE` | Fact (primary) | one row per unit |
| `V_MIGRATION_KPI` | Header cards | one row |
| `V_UNIT_ATTEMPT_HISTORY` | Effort analysis | one row per unit |
| `V_FAILURE_MODE_BREAKDOWN` | Root-cause drill-through | one row per finding |
| `V_MIGRATION_BURNDOWN` | Trend | one row per day |
| `EQUIVALENCE_RUN` | Detail / drill-through | one row per unit per run |

Relationships: `V_UNIT_CURRENT_STATE[UNIT_NAME]` 1:* to
`EQUIVALENCE_RUN[UNIT_NAME]`, `V_UNIT_ATTEMPT_HISTORY[UNIT_NAME]` and
`V_FAILURE_MODE_BREAKDOWN[UNIT_NAME]`. Single-direction filtering — bidirectional
cross-filtering on the findings table double-counts units with several findings.

Mark a `DimDate` table as the date table and relate it to
`EQUIVALENCE_RUN[CAPTURED_AT]`; the burndown view is pre-aggregated and does
not need it.

## Pages

### 1. Programme status (default landing page)

Cards: `Units Migrated`, `Units Blocked`, `Auto-Translation Rate %`,
`Engineer Hours Saved`, `Mean Equivalence Score`.

Main visual: a table over `V_UNIT_CURRENT_STATE` — unit, owner, criticality,
score, state — with conditional formatting on `MIGRATION_STATE`
(`MIGRATED` green, `READY_TO_PROMOTE` blue, `BLOCKED` red, `ROLLED_BACK` amber)
and a data bar on `EQUIVALENCE_SCORE` **scaled 90–100, not 0–100**. Every real
score sits above 95, so a 0-based bar renders seven near-identical full bars
and hides the thing the page exists to show.

Right rail: `Units On Replayed Evidence` card. When this is non-zero, some
scores rest on captured snapshots rather than a live legacy run — a reviewer
needs to know that before signing off, so it belongs on the landing page and
not buried in a tooltip.

### 2. Blocked units (root cause)

Stacked bar of `V_FAILURE_MODE_BREAKDOWN` component scores per blocked unit,
so you can see at a glance whether the estate is failing on `cell_value`
(logic defects — fix the prompt) or on `ordering` / `row_set` (structural
defects — usually one systematic mistake across many units).

Below it, the findings table with drill-through to page 4.

### 3. Effort and throughput

`V_UNIT_ATTEMPT_HISTORY`: attempts per unit, score improvement per attempt,
hours in flight. A unit with many attempts and flat `SCORE_IMPROVEMENT` is not
converging — it needs a human, not another generation cycle. That is the
signal this page exists to surface.

Line chart from `V_MIGRATION_BURNDOWN` with `UNITS_CLEARED` against
`UNITS_SCORED` over time.

### 4. Unit detail (drill-through)

Drill-through filter on `UNIT_NAME`. Score history line chart from
`EQUIVALENCE_RUN`, the component breakdown for the latest run, the findings
list, and the provenance fields (`TRANSLATION_MODEL`, `LEGACY_RUNNER`,
`SCORER_VERSION`, `REVIEWER`) as a card row. That provenance block is what an
auditor asks for; putting it on the drill-through page means it is one click
from any unit rather than a query someone has to write.

## DAX measures

```dax
Units Migrated =
CALCULATE(COUNTROWS('V_UNIT_CURRENT_STATE'),
          'V_UNIT_CURRENT_STATE'[MIGRATION_STATE] = "MIGRATED")

Units Blocked =
CALCULATE(COUNTROWS('V_UNIT_CURRENT_STATE'),
          'V_UNIT_CURRENT_STATE'[MIGRATION_STATE] = "BLOCKED")

Auto-Translation Rate % =
DIVIDE(
    CALCULATE(COUNTROWS('V_UNIT_CURRENT_STATE'), 'V_UNIT_CURRENT_STATE'[PASSED] = TRUE()),
    COUNTROWS('V_UNIT_CURRENT_STATE'),
    0
) * 100

-- 40 engineer-hours to migrate a script by hand; 4 hours to review one the
-- gate has already cleared. Only counts units that actually cleared.
Engineer Hours Saved =
CALCULATE(
    COUNTROWS('V_UNIT_CURRENT_STATE') * 36,
    'V_UNIT_CURRENT_STATE'[PASSED] = TRUE()
)

Mean Equivalence Score =
AVERAGE('V_UNIT_CURRENT_STATE'[EQUIVALENCE_SCORE])

-- Guards the headline number: if any unit's evidence is a replayed snapshot
-- rather than a live legacy execution, say so on the page.
Evidence Caveat =
VAR Replayed =
    CALCULATE(COUNTROWS('V_UNIT_CURRENT_STATE'),
              'V_UNIT_CURRENT_STATE'[LEGACY_AUTHORITATIVE] = FALSE())
RETURN
    IF(Replayed = 0,
       "All scores from live legacy execution",
       FORMAT(Replayed, "0") & " unit(s) scored against replayed snapshots")

-- Distance to the gate, signed. Negative means blocked, and by how much -
-- which is the number that tells you whether it is one bad row or a rewrite.
Score Headroom =
AVERAGE('V_UNIT_CURRENT_STATE'[EQUIVALENCE_SCORE])
  - AVERAGE('V_UNIT_CURRENT_STATE'[THRESHOLD])

Units Not Converging =
CALCULATE(
    COUNTROWS('V_UNIT_ATTEMPT_HISTORY'),
    'V_UNIT_ATTEMPT_HISTORY'[ATTEMPTS] >= 3,
    'V_UNIT_ATTEMPT_HISTORY'[SCORE_IMPROVEMENT] < 1
)
```

## Refresh and alerting

Gateway refresh at 03:00 and 11:00 (the Airflow DAG runs at 02:00, so the
morning refresh always sees the overnight scores).

Data alerts on the Programme Status page:

| Alert | Condition | Goes to |
|---|---|---|
| Gate regression | `Auto-Translation Rate %` drops below 85 | migration leads |
| Critical unit blocked | `Units Blocked` > 0 **and** criticality = critical | on-call |
| Stalled translation | `Units Not Converging` > 0 | migration leads |

Do not alert on `Mean Equivalence Score` — it is dominated by the units that
already pass and barely moves when a single unit breaks, so it looks calm
during exactly the incident you want to catch.
