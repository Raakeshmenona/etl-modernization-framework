"""Row- and cell-level diff between legacy and translated output.

Both frames arrive all-string (see dual_executor.normalise). Per column the
differ decides how to compare:

  * if every non-empty value on BOTH sides parses as a number, compare
    numerically within the unit's tolerance
  * otherwise compare as exact strings

Rows are aligned on the unit's key columns. Positional alignment is the
fallback, and it is flagged in the result because it is much weaker evidence:
a translation that emits correct rows in the wrong order would pass on cells
while breaking every downstream consumer that depends on ordering.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

MAX_EXAMPLES = 10


@dataclass
class ColumnDiff:
    column: str
    comparison: str                 # "numeric" | "string"
    compared: int = 0
    mismatched: int = 0
    max_abs_delta: float = 0.0
    examples: List[Dict[str, str]] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        return 1.0 if not self.compared else 1.0 - self.mismatched / self.compared


@dataclass
class DiffResult:
    aligned_on: List[str]
    alignment: str                  # "key" | "positional"
    legacy_rows: int = 0
    translated_rows: int = 0
    aligned_rows: int = 0
    missing_in_translated: List[str] = field(default_factory=list)
    unexpected_in_translated: List[str] = field(default_factory=list)
    legacy_only_columns: List[str] = field(default_factory=list)
    translated_only_columns: List[str] = field(default_factory=list)
    column_order_matches: bool = True
    ordering_matches: bool = True
    columns: Dict[str, ColumnDiff] = field(default_factory=dict)
    mismatched_keys: List[str] = field(default_factory=list)

    @property
    def cells_compared(self) -> int:
        return sum(c.compared for c in self.columns.values())

    @property
    def cells_mismatched(self) -> int:
        return sum(c.mismatched for c in self.columns.values())

    @property
    def rows_with_any_mismatch(self) -> int:
        return len(self.mismatched_keys)

    def failing_columns(self) -> List[ColumnDiff]:
        return sorted((c for c in self.columns.values() if c.mismatched),
                      key=lambda c: -c.mismatched)


def _as_number(value: str) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _is_numeric_column(left: pd.Series, right: pd.Series) -> bool:
    values = [v for v in list(left) + list(right) if str(v).strip() != ""]
    if not values:
        return False
    return all(_as_number(v) is not None for v in values)


def _key_series(frame: pd.DataFrame, keys: List[str]) -> pd.Series:
    return frame[keys].astype(str).agg("|".join, axis=1)


def diff(legacy: pd.DataFrame,
         translated: pd.DataFrame,
         key_columns: Optional[List[str]] = None,
         tolerance: float = 0.0) -> DiffResult:
    """Compare two normalised frames."""
    key_columns = key_columns or []
    usable_keys = [k for k in key_columns
                   if k in legacy.columns and k in translated.columns]

    result = DiffResult(
        aligned_on=usable_keys,
        alignment="key" if usable_keys else "positional",
        legacy_rows=len(legacy),
        translated_rows=len(translated),
    )

    legacy_cols, translated_cols = list(legacy.columns), list(translated.columns)
    result.legacy_only_columns = [c for c in legacy_cols if c not in translated_cols]
    result.translated_only_columns = [c for c in translated_cols if c not in legacy_cols]
    result.column_order_matches = legacy_cols == translated_cols

    shared = [c for c in legacy_cols if c in translated_cols]
    if not shared:
        logger.warning("no shared columns between legacy and translated output")
        return result

    left, right, keys = _align(legacy, translated, usable_keys, result)
    result.aligned_rows = len(left)

    if usable_keys and len(left):
        # Same rows, same order? Compare the key sequences as emitted.
        legacy_order = list(_key_series(legacy, usable_keys))
        translated_order = list(_key_series(translated, usable_keys))
        result.ordering_matches = legacy_order == translated_order

    mismatched_keys = set()

    for column in shared:
        numeric = _is_numeric_column(left[column], right[column])
        cdiff = ColumnDiff(column=column, comparison="numeric" if numeric else "string")

        for position in range(len(left)):
            lv, rv = left[column].iloc[position], right[column].iloc[position]
            cdiff.compared += 1

            if numeric:
                ln, rn = _as_number(lv), _as_number(rv)
                if ln is None and rn is None:
                    continue
                if ln is None or rn is None:
                    equal = False
                    delta = float("inf")
                else:
                    delta = abs(ln - rn)
                    equal = delta <= tolerance
                    cdiff.max_abs_delta = max(cdiff.max_abs_delta, delta)
            else:
                equal = str(lv).strip() == str(rv).strip()

            if not equal:
                cdiff.mismatched += 1
                mismatched_keys.add(keys[position])
                if len(cdiff.examples) < MAX_EXAMPLES:
                    cdiff.examples.append({
                        "key": keys[position],
                        "legacy": str(lv),
                        "translated": str(rv),
                    })

        result.columns[column] = cdiff

    result.mismatched_keys = sorted(mismatched_keys)
    return result


def _align(legacy: pd.DataFrame,
           translated: pd.DataFrame,
           keys: List[str],
           result: DiffResult) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    """Return (legacy_rows, translated_rows, row_labels) in matched order."""
    if not keys:
        n = min(len(legacy), len(translated))
        if len(legacy) != len(translated):
            logger.warning("positional alignment over differing row counts (%d vs %d)",
                           len(legacy), len(translated))
        return (legacy.head(n).reset_index(drop=True),
                translated.head(n).reset_index(drop=True),
                ["row[%d]" % i for i in range(n)])


    left = legacy.reset_index(drop=True)
    right = translated.reset_index(drop=True)
    left_keys = list(_key_series(left, keys))
    right_keys = list(_key_series(right, keys))

    # First occurrence wins on the right. Duplicate keys are themselves a
    # finding - a translation that fans a row out is not equivalent - and the
    # row-count component of the score catches it.
    right_position = {}
    for index, key in enumerate(right_keys):
        right_position.setdefault(key, index)

    left_seen = set(left_keys)
    result.missing_in_translated = [k for k in left_keys
                                    if k not in right_position][:MAX_EXAMPLES * 5]
    result.unexpected_in_translated = [k for k in right_keys
                                       if k not in left_seen][:MAX_EXAMPLES * 5]

    matched = [i for i, k in enumerate(left_keys) if k in right_position]
    left_matched = left.iloc[matched].reset_index(drop=True)
    right_matched = right.iloc[[right_position[left_keys[i]] for i in matched]] \
                         .reset_index(drop=True)

    return left_matched, right_matched, [left_keys[i] for i in matched]


def summarise(result: DiffResult) -> str:
    """One-screen human summary, for CI logs and review packets."""
    lines = [
        "rows      legacy=%d translated=%d aligned=%d (%s)"
        % (result.legacy_rows, result.translated_rows, result.aligned_rows, result.alignment),
        "cells     compared=%d mismatched=%d"
        % (result.cells_compared, result.cells_mismatched),
    ]
    if result.missing_in_translated:
        lines.append("missing   %s" % ", ".join(result.missing_in_translated[:5]))
    if result.unexpected_in_translated:
        lines.append("extra     %s" % ", ".join(result.unexpected_in_translated[:5]))
    if result.legacy_only_columns or result.translated_only_columns:
        lines.append("schema    legacy_only=%s translated_only=%s"
                     % (result.legacy_only_columns, result.translated_only_columns))
    if not result.ordering_matches:
        lines.append("ordering  row order differs between legacy and translated output")

    for cdiff in result.failing_columns():
        lines.append("  %-22s %s  %d/%d mismatched (max delta %.6g)"
                     % (cdiff.column, cdiff.comparison, cdiff.mismatched,
                        cdiff.compared, cdiff.max_abs_delta))
        for example in cdiff.examples[:3]:
            lines.append("      %-26s legacy=%-14s translated=%s"
                         % (example["key"], example["legacy"], example["translated"]))
    return "\n".join(lines)
