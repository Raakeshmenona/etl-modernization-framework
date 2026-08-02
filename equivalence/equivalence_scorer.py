"""Turn a diff into a single defensible equivalence score.

The score is a weighted sum of five independent components. The weights are a
governance decision, not a mathematical truth - they are stated here, versioned,
and written to the audit log with every run so a score can be re-derived years
later.

    schema        0.20   same columns, same order
    row_set       0.15   same rows present, no drops, no fan-out
    cell_value    0.50   cell-for-cell agreement on aligned rows
    null_pattern  0.10   the same cells are empty on both sides
    ordering      0.05   rows emitted in the same order

Why cell_value is only half: a translation that agrees on every cell it emits
but silently drops 30% of rows is not equivalent, and a single blended
"percentage of matching rows" hides that. Splitting the components is what lets
the gate explain *why* it blocked.

Nulls get their own component because Perl's undef, an empty CSV field, and
pandas' NaN are three different things that all print as nothing. Collapsing
them into cell equality is how null-handling defects reach production.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from equivalence.output_differ import DiffResult

logger = logging.getLogger(__name__)

WEIGHTS = {
    "schema": 0.20,
    "row_set": 0.15,
    "cell_value": 0.50,
    "null_pattern": 0.10,
    "ordering": 0.05,
}

SCORER_VERSION = "1.0.0"


@dataclass
class EquivalenceScore:
    unit: str
    score: float                            # 0-100
    passed: bool
    threshold: float
    components: Dict[str, float] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=lambda: dict(WEIGHTS))
    scorer_version: str = SCORER_VERSION
    findings: List[str] = field(default_factory=list)
    legacy_rows: int = 0
    translated_rows: int = 0
    cells_compared: int = 0
    cells_mismatched: int = 0
    rows_with_mismatch: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


def _schema_component(result: DiffResult) -> float:
    shared = len(result.columns)
    total = shared + len(result.legacy_only_columns) + len(result.translated_only_columns)
    if total == 0:
        return 0.0
    presence = shared / total
    # Column order is worth a quarter of the schema component: consumers that
    # read positionally break on a reorder even though every value is right.
    return presence * (1.0 if result.column_order_matches else 0.75)


def _row_set_component(result: DiffResult) -> float:
    universe = max(result.legacy_rows, result.translated_rows)
    if universe == 0:
        return 1.0 if result.legacy_rows == result.translated_rows else 0.0
    dropped = len(result.missing_in_translated)
    added = len(result.unexpected_in_translated)
    return max(0.0, 1.0 - (dropped + added) / universe)


def _cell_component(result: DiffResult) -> float:
    if result.cells_compared == 0:
        return 0.0
    return 1.0 - result.cells_mismatched / result.cells_compared


def _null_component(legacy: pd.DataFrame, translated: pd.DataFrame) -> float:
    """Fraction of columns whose empty-cell count agrees on both sides."""
    shared = [c for c in legacy.columns if c in translated.columns]
    if not shared:
        return 0.0
    agreeing = 0
    for column in shared:
        left_nulls = int((legacy[column].astype(str).str.strip() == "").sum())
        right_nulls = int((translated[column].astype(str).str.strip() == "").sum())
        if left_nulls == right_nulls:
            agreeing += 1
    return agreeing / len(shared)


def score(unit_name: str,
          legacy: pd.DataFrame,
          translated: pd.DataFrame,
          result: DiffResult,
          threshold: float) -> EquivalenceScore:
    """Score one translation against its legacy counterpart."""
    components = {
        "schema": _schema_component(result),
        "row_set": _row_set_component(result),
        "cell_value": _cell_component(result),
        "null_pattern": _null_component(legacy, translated),
        "ordering": 1.0 if result.ordering_matches else 0.0,
    }
    total = sum(WEIGHTS[name] * value for name, value in components.items()) * 100.0

    findings = []
    if result.legacy_only_columns:
        findings.append("columns missing from translation: %s"
                        % ", ".join(result.legacy_only_columns))
    if result.translated_only_columns:
        findings.append("columns the legacy job never emitted: %s"
                        % ", ".join(result.translated_only_columns))
    if not result.column_order_matches and not (result.legacy_only_columns
                                                or result.translated_only_columns):
        findings.append("column order differs")
    if result.missing_in_translated:
        findings.append("%d row(s) present in legacy output but not in the translation"
                        % len(result.missing_in_translated))
    if result.unexpected_in_translated:
        findings.append("%d row(s) the translation invented"
                        % len(result.unexpected_in_translated))
    if not result.ordering_matches:
        findings.append("row ordering differs (check for a numeric sort where the "
                        "legacy job used Perl's default string sort)")
    for cdiff in result.failing_columns():
        findings.append("%s: %d/%d cells differ (%s comparison, max delta %.6g)"
                        % (cdiff.column, cdiff.mismatched, cdiff.compared,
                           cdiff.comparison, cdiff.max_abs_delta))

    return EquivalenceScore(
        unit=unit_name,
        score=round(total, 4),
        passed=total >= threshold,
        threshold=threshold,
        components={k: round(v, 6) for k, v in components.items()},
        findings=findings,
        legacy_rows=result.legacy_rows,
        translated_rows=result.translated_rows,
        cells_compared=result.cells_compared,
        cells_mismatched=result.cells_mismatched,
        rows_with_mismatch=result.rows_with_any_mismatch,
    )


def failed_score(unit_name: str, threshold: float, error: str) -> EquivalenceScore:
    """A run that never produced comparable output scores zero, not null.

    A translation that crashes is maximally non-equivalent. Recording it as
    "unknown" is how broken jobs slip past a dashboard.
    """
    return EquivalenceScore(
        unit=unit_name,
        score=0.0,
        passed=False,
        threshold=threshold,
        components={name: 0.0 for name in WEIGHTS},
        findings=["execution failed: %s" % error],
        error=error,
    )
