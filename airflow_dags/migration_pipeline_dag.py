"""Airflow DAG: discover -> translate -> validate -> score -> gate -> promote.

The DAG is deliberately thin. All logic lives in `migration_pipeline.py`, so a
local run and a scheduled run execute the same code; the DAG contributes task
boundaries for retries and observability, and a per-unit TaskGroup so one
blocked translation does not hide the other six.

    discover_estate
        |
        +-- (per unit, in parallel) translate -> validate -> dual_execute_and_score
        |
    aggregate_scores
        |
        +-- equivalence_gate ---(all clear)---> promote_to_snowflake
        |                    \\--(blocked)----> raise_for_review
        |
    publish_audit_log

Only `promote_to_snowflake` writes anywhere durable. Everything before it is
side-effect free by design: a failed migration run should leave no trace beyond
the audit record that says it failed.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# The DAGs folder is not on PYTHONPATH inside a scheduler container.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from airflow import DAG                                              # noqa: E402
from airflow.exceptions import AirflowFailException                  # noqa: E402
from airflow.operators.python import PythonOperator                  # noqa: E402
from airflow.utils.task_group import TaskGroup                       # noqa: E402

from config import EQUIVALENCE_THRESHOLD, load_manifest, unit_by_name  # noqa: E402
from migration_pipeline import process_unit                          # noqa: E402
from parsers import dependency_mapper                                # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_ARGS = {
    "owner": "data-modernization",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
    "email_on_failure": True,
    "depends_on_past": False,
}


def discover_estate(**context) -> dict:
    """Inventory the corpus and publish the dependency-ordered migration plan."""
    units = load_manifest()
    graph = dependency_mapper.build(units)
    plan = {
        "units": [u.name for u in units],
        "waves": dependency_mapper.migration_waves(graph),
        "undocumented_dependencies": [list(e) for e in sorted(graph.undocumented_edges)],
    }
    if plan["undocumented_dependencies"]:
        # Not fatal, but a reviewer needs to see it before a phased cutover.
        logger.warning("undocumented coupling detected: %s",
                       plan["undocumented_dependencies"])
    logger.info("discovered %d units across %d waves",
                len(plan["units"]), len(plan["waves"]))
    return plan


def translate_and_score(unit_name: str, **context) -> dict:
    """Translate, statically validate, dual-execute and score one unit."""
    unit = unit_by_name(unit_name)
    result = process_unit(unit, threshold=EQUIVALENCE_THRESHOLD)
    logger.info("[%s] score=%.3f passed=%s", unit_name, result.score.score, result.passed)
    # XCom carries the summary only. The full diff stays on the worker's disk
    # and in the report artefact; XCom is not a place for a 500-row frame.
    return {
        "unit": unit_name,
        "owner": result.owner,
        "criticality": result.criticality,
        "score": result.score.score,
        "passed": result.passed,
        "findings": result.score.findings[:10],
        "components": result.score.components,
        "regression_cases": result.regression_cases,
        "legacy_runner": result.legacy_runner,
        "translation_source": result.translation_source,
    }


def aggregate_scores(**context) -> dict:
    task_instance = context["ti"]
    plan = task_instance.xcom_pull(task_ids="discover_estate")
    results = [task_instance.xcom_pull(task_ids="units.score_%s" % name)
               for name in plan["units"]]
    results = [r for r in results if r]

    passed = [r for r in results if r["passed"]]
    summary = {
        "run_id": context["run_id"],
        "logical_date": context["logical_date"].isoformat(),
        "threshold": EQUIVALENCE_THRESHOLD,
        "unit_count": len(results),
        "passed": len(passed),
        "blocked": len(results) - len(passed),
        "auto_translation_rate": round(100.0 * len(passed) / len(results), 2) if results else 0.0,
        "mean_score": round(sum(r["score"] for r in results) / len(results), 4) if results else 0.0,
        "results": results,
    }
    logger.info("aggregate: %s", json.dumps(
        {k: v for k, v in summary.items() if k != "results"}, indent=2))
    return summary


def equivalence_gate(**context) -> dict:
    """Fail the DAG run when any unit falls below the threshold."""
    summary = context["ti"].xcom_pull(task_ids="aggregate_scores")
    blocked = [r for r in summary["results"] if not r["passed"]]

    if blocked:
        detail = "; ".join("%s=%.2f%% (%s)"
                           % (r["unit"], r["score"],
                              r["findings"][0] if r["findings"] else "below threshold")
                           for r in blocked)
        # AirflowFailException, not a retry: a non-equivalent translation will
        # be non-equivalent again in five minutes. It needs a human.
        raise AirflowFailException(
            "equivalence gate blocked %d/%d unit(s) below %.1f%%: %s"
            % (len(blocked), summary["unit_count"], EQUIVALENCE_THRESHOLD, detail))

    logger.info("gate cleared: all %d units at or above %.1f%%",
                summary["unit_count"], EQUIVALENCE_THRESHOLD)
    return summary


def promote_to_snowflake(**context) -> dict:
    """Publish translations that cleared the gate, then record the promotion."""
    from reporting.snowflake_migration_log import promote, write_audit_records

    summary = context["ti"].xcom_pull(task_ids="aggregate_scores")
    promoted = promote([r for r in summary["results"] if r["passed"]],
                       run_id=summary["run_id"])
    write_audit_records(summary, promoted=promoted)
    logger.info("promoted %d unit(s)", len(promoted))
    return {"promoted": promoted}


def publish_audit_log(**context) -> None:
    """Always record the run, cleared or blocked. This is the trail regulators read."""
    from reporting.snowflake_migration_log import write_audit_records

    summary = context["ti"].xcom_pull(task_ids="aggregate_scores")
    write_audit_records(summary, promoted=[])


with DAG(
    dag_id="etl_migration_pipeline",
    description="LLM-assisted legacy ETL migration with a dual-execution equivalence gate",
    default_args=DEFAULT_ARGS,
    schedule="0 2 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["migration", "equivalence", "genai"],
) as dag:

    discover = PythonOperator(
        task_id="discover_estate",
        python_callable=discover_estate,
    )

    # Fan out per unit so one blocked translation does not mask the others,
    # and so a flaky unit retries alone rather than re-running the estate.
    with TaskGroup(group_id="units") as unit_group:
        for manifest_unit in load_manifest():
            PythonOperator(
                task_id="score_%s" % manifest_unit.name,
                python_callable=translate_and_score,
                op_kwargs={"unit_name": manifest_unit.name},
            )

    aggregate = PythonOperator(
        task_id="aggregate_scores",
        python_callable=aggregate_scores,
    )

    gate = PythonOperator(
        task_id="equivalence_gate",
        python_callable=equivalence_gate,
    )

    promote = PythonOperator(
        task_id="promote_to_snowflake",
        python_callable=promote_to_snowflake,
    )

    audit = PythonOperator(
        task_id="publish_audit_log",
        python_callable=publish_audit_log,
        trigger_rule="all_done",          # record blocked runs too
    )

    discover >> unit_group >> aggregate >> gate >> promote
    aggregate >> audit
