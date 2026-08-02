"""Generate a pytest regression suite from an observed dual run.

This is the half of the framework that outlives the migration. The equivalence
score proves the translation matched the legacy job on the day it was migrated;
the generated regression suite is what stops someone re-breaking it in 2029,
long after the Perl has been deleted and nobody remembers that the completeness
denominator was supposed to be the observed field count.

Cases are not sampled at random. The selector goes after the rows where a
translation is most likely to drift:

    boundary        numeric min and max per column
    categorical     one row per distinct value of every low-cardinality column
    blank           rows carrying an empty cell
    divergent       rows that actually differed in this run (regression-in-
                    waiting: they are pinned to the LEGACY value, so a future
                    "fix" that reintroduces the defect fails loudly)
    spread          evenly spaced rows for baseline coverage

Alongside the row cases it pins the invariants a diff cannot express: schema
and column order, row count, null counts, and per-column aggregate totals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd

from config import REGRESSION_DIR, Unit
from equivalence.output_differ import DiffResult

logger = logging.getLogger(__name__)

MAX_CASES = 40
# Divergent rows are the most interesting cases, but a badly broken translation
# produces hundreds of them and they would crowd out every other family. Cap
# them so the suite keeps its boundary and categorical coverage.
MAX_DIVERGENT_CASES = 12
CATEGORICAL_MAX_CARDINALITY = 12
SPREAD_CASES = 8


@dataclass
class RegressionCase:
    case_id: str
    reason: str
    key: Dict[str, str]
    expected: Dict[str, str]


@dataclass
class RegressionSuite:
    unit: str
    cases: List[RegressionCase] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    row_count: int = 0
    null_counts: Dict[str, int] = field(default_factory=dict)
    numeric_totals: Dict[str, float] = field(default_factory=dict)
    reasons: Dict[str, int] = field(default_factory=dict)


def _numeric_series(frame: pd.DataFrame, column: str) -> Optional[pd.Series]:
    converted = pd.to_numeric(frame[column].astype(str), errors="coerce")
    return converted if converted.notna().any() else None


def select_cases(legacy: pd.DataFrame,
                 key_columns: Sequence[str],
                 divergent_keys: Sequence[str]) -> List[RegressionCase]:
    """Pick the rows worth pinning, most interesting first."""
    if legacy.empty:
        return []

    keys = [k for k in key_columns if k in legacy.columns]
    picked: Dict[int, str] = {}                     # row index -> reason

    def take(index: int, reason: str) -> None:
        if index in picked or index < 0 or index >= len(legacy):
            return
        picked[index] = reason

    # 1. Rows that diverged in this run.
    if keys and divergent_keys:
        key_series = legacy[keys].astype(str).agg("|".join, axis=1)
        lookup = {value: position for position, value in enumerate(key_series)}
        for divergent in list(divergent_keys)[:MAX_DIVERGENT_CASES]:
            if divergent in lookup:
                take(lookup[divergent], "divergent")

    # 2. Numeric boundaries.
    for column in legacy.columns:
        numeric = _numeric_series(legacy, column)
        if numeric is None or numeric.dropna().empty:
            continue
        take(int(numeric.idxmin()), "boundary:%s:min" % column)
        take(int(numeric.idxmax()), "boundary:%s:max" % column)

    # 3. One row per distinct value of each categorical column.
    for column in legacy.columns:
        values = legacy[column].astype(str)
        if 1 < values.nunique() <= CATEGORICAL_MAX_CARDINALITY:
            for value in sorted(values.unique()):
                match = values[values == value]
                take(int(match.index[0]), "categorical:%s=%s" % (column, value))

    # 4. Rows with a blank cell anywhere.
    blank_mask = (legacy.astype(str).apply(lambda s: s.str.strip() == "")).any(axis=1)
    for index in list(legacy.index[blank_mask])[:5]:
        take(int(index), "blank")

    # 5. Evenly spaced baseline coverage.
    if len(legacy) > 1:
        step = max(1, len(legacy) // SPREAD_CASES)
        for index in range(0, len(legacy), step):
            take(index, "spread")

    priority = {"divergent": 0, "boundary": 1, "categorical": 2, "blank": 3, "spread": 4}
    ordered = sorted(picked.items(),
                     key=lambda item: (priority.get(item[1].split(":")[0], 9), item[0]))

    cases = []
    for position, (index, reason) in enumerate(ordered[:MAX_CASES]):
        row = legacy.iloc[index]
        cases.append(RegressionCase(
            case_id="%s_%03d" % (reason.split(":")[0], position),
            reason=reason,
            key={k: str(row[k]) for k in keys} or {"__row": str(index)},
            expected={c: str(row[c]) for c in legacy.columns},
        ))
    return cases


def build(unit: Unit,
          legacy: pd.DataFrame,
          diff_result: Optional[DiffResult] = None) -> RegressionSuite:
    divergent = list(diff_result.mismatched_keys) if diff_result else []
    suite = RegressionSuite(
        unit=unit.name,
        cases=select_cases(legacy, unit.key_columns, divergent),
        columns=list(legacy.columns),
        row_count=len(legacy),
    )

    for column in legacy.columns:
        blanks = int((legacy[column].astype(str).str.strip() == "").sum())
        suite.null_counts[column] = blanks
        numeric = _numeric_series(legacy, column)
        if numeric is not None and numeric.notna().all():
            suite.numeric_totals[column] = round(float(numeric.sum()), 6)

    for case in suite.cases:
        family = case.reason.split(":")[0]
        suite.reasons[family] = suite.reasons.get(family, 0) + 1

    return suite


def render(suite: RegressionSuite, unit: Unit) -> str:
    """Emit a standalone pytest module."""
    header = '''"""AUTO-GENERATED regression suite for `{unit}` - do not hand-edit.

Generated by equivalence/regression_generator.py from a dual run against the
captured legacy output on {generated_at}.

Every expectation here is the LEGACY job's observed behaviour, including any
behaviour that looks like a defect. If one of these tests fails, the
translation has drifted from the system it replaced - that is a finding, not a
test to update. Change an expectation only alongside a signed-off behaviour
change, and record it in the migration audit log.

Case selection: {reasons}

Regenerate:  python -m equivalence.regression_generator --unit {unit}
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import unit_by_name                                    # noqa: E402
from equivalence.dual_executor import run_translated               # noqa: E402

UNIT = unit_by_name("{unit}")
KEY_COLUMNS = {key_columns!r}
EXPECTED_COLUMNS = {columns!r}
EXPECTED_ROW_COUNT = {row_count}
EXPECTED_NULL_COUNTS = {null_counts!r}
EXPECTED_NUMERIC_TOTALS = {numeric_totals!r}
TOLERANCE = {tolerance!r}

CASES = {cases!r}


@pytest.fixture(scope="module")
def actual():
    result = run_translated(UNIT)
    assert result.ok, "translation failed to execute: %s" % result.error
    return result.frame


def _row_for(frame, key):
    if "__row" in key:
        return frame.iloc[int(key["__row"])]
    mask = None
    for column, value in key.items():
        this = frame[column].astype(str) == value
        mask = this if mask is None else (mask & this)
    matched = frame[mask]
    assert len(matched) == 1, (
        "expected exactly one row for key %s, found %d" % (key, len(matched)))
    return matched.iloc[0]


def _equal(expected, actual_value, tolerance):
    if str(expected).strip() == str(actual_value).strip():
        return True
    try:
        return abs(float(expected) - float(actual_value)) <= tolerance
    except (TypeError, ValueError):
        return False


def test_schema_and_column_order(actual):
    assert list(actual.columns) == EXPECTED_COLUMNS


def test_row_count(actual):
    assert len(actual) == EXPECTED_ROW_COUNT


@pytest.mark.parametrize("column,expected", sorted(EXPECTED_NULL_COUNTS.items()))
def test_null_counts(actual, column, expected):
    blanks = int((actual[column].astype(str).str.strip() == "").sum())
    assert blanks == expected


@pytest.mark.parametrize("column,expected", sorted(EXPECTED_NUMERIC_TOTALS.items()))
def test_numeric_totals(actual, column, expected):
    import pandas as pd
    total = float(pd.to_numeric(actual[column].astype(str), errors="coerce").sum())
    assert abs(total - expected) <= max(TOLERANCE * len(actual), 1e-6)


@pytest.mark.parametrize("case", CASES, ids=[c["case_id"] for c in CASES])
def test_row_matches_legacy(actual, case):
    row = _row_for(actual, case["key"])
    mismatched = [
        "%s: legacy=%r translated=%r" % (column, expected, row[column])
        for column, expected in case["expected"].items()
        if not _equal(expected, row[column], TOLERANCE)
    ]
    assert not mismatched, "%s (%s)\\n  %s" % (
        case["case_id"], case["reason"], "\\n  ".join(mismatched))
'''
    return header.format(
        unit=suite.unit,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        reasons=", ".join("%s=%d" % kv for kv in sorted(suite.reasons.items())) or "none",
        key_columns=list(unit.key_columns),
        columns=suite.columns,
        row_count=suite.row_count,
        null_counts=suite.null_counts,
        numeric_totals=suite.numeric_totals,
        tolerance=unit.tolerance,
        cases=[{"case_id": c.case_id, "reason": c.reason,
                "key": c.key, "expected": c.expected} for c in suite.cases],
    )


def write(suite: RegressionSuite, unit: Unit, directory: Path = REGRESSION_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("test_%s_regression.py" % unit.name)
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render(suite, unit))
    logger.info("[%-24s] %2d regression cases -> %s",
                unit.name, len(suite.cases), path.name)
    return path


def main() -> int:
    import argparse
    import logging as _logging

    from config import load_manifest
    from equivalence import dual_executor, output_differ

    _logging.basicConfig(level=_logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--unit", help="generate for a single unit")
    args = ap.parse_args()

    for unit in load_manifest():
        if args.unit and unit.name != args.unit:
            continue
        run = dual_executor.execute(unit, prefer_subprocess=False)
        if not run.legacy.ok:
            logger.error("[%s] no legacy output to pin: %s", unit.name, run.legacy.error)
            continue
        result = None
        if run.translated.ok:
            result = output_differ.diff(run.legacy.frame, run.translated.frame,
                                        unit.key_columns, unit.tolerance)
        write(build(unit, run.legacy.frame, result), unit)
    return 0


if __name__ == "__main__":
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
