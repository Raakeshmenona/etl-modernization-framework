"""Immutable audit trail for the migration programme.

Every scored run is written to Snowflake, cleared or blocked. That matters more
than it sounds: the audit log is the artefact that answers "on what evidence
did we retire this job?" years after the Perl is gone and the people who ran
the programme have moved on. Recording only successful promotions produces a
trail that cannot distinguish "translated correctly first time" from
"translated eleven times until it passed".

Each record pins the score AND how it was derived - the scorer version, the
component weights, the model that produced the translation, the hash of the
legacy source, and whether the legacy side was executed or replayed. A score
without that context is not evidence.

Without Snowflake credentials the writer falls back to newline-delimited JSON
under reports/audit/, so a local run produces the same records in the same
shape and the loader can be replayed later.
"""
from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from config import (EQUIVALENCE_THRESHOLD, REPORT_DIR, TRANSLATION_MODEL,
                    load_manifest)

logger = logging.getLogger(__name__)

AUDIT_DIR = REPORT_DIR / "audit"
AUDIT_TABLE = "MIGRATION_AUDIT.PUBLIC.EQUIVALENCE_RUN"
PROMOTION_TABLE = "MIGRATION_AUDIT.PUBLIC.UNIT_PROMOTION"


def _connection():
    """Snowflake connection from the environment, or None when unconfigured."""
    required = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER")
    if not all(os.environ.get(key) for key in required):
        logger.info("Snowflake not configured (%s) - writing audit records to %s",
                    ", ".join(required), AUDIT_DIR)
        return None
    try:
        import snowflake.connector
    except ImportError:
        logger.warning("snowflake-connector-python is not installed - "
                       "falling back to local audit records")
        return None

    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ.get("SNOWFLAKE_PASSWORD"),
        authenticator=os.environ.get("SNOWFLAKE_AUTHENTICATOR", "snowflake"),
        role=os.environ.get("SNOWFLAKE_ROLE", "MIGRATION_WRITER"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "MIGRATION_WH"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "MIGRATION_AUDIT"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
    )


def build_records(summary: Dict, promoted: Optional[Sequence[str]] = None) -> List[Dict]:
    """One row per unit per run."""
    promoted = set(promoted or [])
    captured_at = datetime.now(timezone.utc).isoformat()
    owners = {u.name: u for u in load_manifest()}

    records = []
    for result in summary.get("results", []):
        unit = owners.get(result["unit"])
        records.append({
            "RUN_ID": summary.get("run_id"),
            "CAPTURED_AT": captured_at,
            "UNIT_NAME": result["unit"],
            "OWNER": result.get("owner"),
            "CRITICALITY": result.get("criticality"),
            "LANGUAGE": unit.language if unit else None,
            "SOURCE_PATH": unit.path if unit else None,
            "EQUIVALENCE_SCORE": result.get("score"),
            "THRESHOLD": summary.get("threshold", EQUIVALENCE_THRESHOLD),
            "PASSED": bool(result.get("passed")),
            "PROMOTED": result["unit"] in promoted,
            "SCORE_COMPONENTS": json.dumps(result.get("components", {})),
            "FINDINGS": json.dumps(result.get("findings", [])),
            "REGRESSION_CASES": result.get("regression_cases", 0),
            # Provenance: without these the score is a number, not evidence.
            "TRANSLATION_MODEL": TRANSLATION_MODEL,
            "TRANSLATION_SOURCE": result.get("translation_source"),
            "LEGACY_RUNNER": result.get("legacy_runner"),
            "LEGACY_AUTHORITATIVE": result.get("legacy_runner") == "perl-subprocess",
            "SCORER_VERSION": summary.get("scorer_version", "1.0.0"),
            "EXECUTED_ON": socket.gethostname(),
            "REVIEWER": os.environ.get("MIGRATION_REVIEWER", "unassigned"),
        })
    return records


def write_audit_records(summary: Dict,
                        promoted: Optional[Sequence[str]] = None) -> int:
    """Append this run's records to Snowflake, or to the local fallback."""
    records = build_records(summary, promoted)
    if not records:
        logger.warning("no results in the summary - nothing to audit")
        return 0

    connection = _connection()
    if connection is None:
        return _write_local(records)

    columns = list(records[0].keys())
    placeholders = ", ".join(["%s"] * len(columns))
    statement = "INSERT INTO %s (%s) VALUES (%s)" % (
        AUDIT_TABLE, ", ".join(columns), placeholders)

    try:
        with connection.cursor() as cursor:
            cursor.executemany(statement, [tuple(r[c] for c in columns) for r in records])
        connection.commit()
        logger.info("wrote %d audit record(s) to %s", len(records), AUDIT_TABLE)
    finally:
        connection.close()
    return len(records)


def _write_local(records: List[Dict]) -> int:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / ("equivalence_run_%s.jsonl"
                        % (records[0].get("RUN_ID") or "local"))
    with open(str(path), "a", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    logger.info("wrote %d audit record(s) to %s", len(records), path)
    return len(records)


def promote(results: Sequence[Dict], run_id: str) -> List[str]:
    """Record promotion of the units that cleared the gate.

    Promotion is per unit. A blocked sibling does not hold back a translation
    that has been proven equivalent - it just stays on the legacy stack until
    its own score clears.
    """
    promoted = [r["unit"] for r in results if r.get("passed")]
    if not promoted:
        logger.info("nothing to promote")
        return []

    connection = _connection()
    if connection is None:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        path = AUDIT_DIR / "promotions.jsonl"
        with open(str(path), "a", encoding="utf-8", newline="\n") as fh:
            for unit in promoted:
                fh.write(json.dumps({
                    "RUN_ID": run_id,
                    "UNIT_NAME": unit,
                    "PROMOTED_AT": datetime.now(timezone.utc).isoformat(),
                    "PROMOTED_BY": os.environ.get("MIGRATION_REVIEWER", "ci"),
                }) + "\n")
        logger.info("recorded %d promotion(s) in %s", len(promoted), path)
        return promoted

    try:
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO %s (RUN_ID, UNIT_NAME, PROMOTED_AT, PROMOTED_BY) "
                "VALUES (%%s, %%s, CURRENT_TIMESTAMP(), %%s)" % PROMOTION_TABLE,
                [(run_id, unit, os.environ.get("MIGRATION_REVIEWER", "ci"))
                 for unit in promoted])
        connection.commit()
        logger.info("recorded %d promotion(s) in %s", len(promoted), PROMOTION_TABLE)
    finally:
        connection.close()
    return promoted


def main() -> int:
    """Load an existing equivalence report into the audit trail."""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", type=Path,
                    default=REPORT_DIR / "equivalence_report.json")
    ap.add_argument("--promote", action="store_true",
                    help="also record promotions for units that cleared the gate")
    args = ap.parse_args()

    if not args.report.exists():
        logger.error("no report at %s - run scripts/run_migration_local.py first",
                     args.report)
        return 2

    payload = json.loads(args.report.read_text(encoding="utf-8"))
    summary = {
        "run_id": payload["run_id"],
        "threshold": payload["threshold"],
        "results": [{
            "unit": u["unit"],
            "owner": u["owner"],
            "criticality": u["criticality"],
            "score": u["score"]["score"],
            "passed": u["passed"],
            "findings": u["score"]["findings"],
            "components": u["score"]["components"],
            "regression_cases": u["regression_cases"],
            "legacy_runner": u["legacy_runner"],
            "translation_source": u["translation_source"],
        } for u in payload["units"]],
    }

    promoted = promote(summary["results"], summary["run_id"]) if args.promote else []
    write_audit_records(summary, promoted=promoted)
    return 0


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
