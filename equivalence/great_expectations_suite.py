"""Derive a Great Expectations suite from legacy output, run it on both sides.

The equivalence score answers "did the translation reproduce this run?". The
expectation suite answers a different and longer-lived question: "does the
translation still behave like the legacy job on data it has never seen?"

The suite is inferred from the legacy output - column presence and order,
observed value ranges, categorical domains, null rates, uniqueness of the key -
and then applied to BOTH sides. A translation that passes the diff but violates
an expectation the legacy job always satisfied is flagged: it means the
translation agreed on this sample by luck rather than by construction.

Great Expectations is optional. Without it the same expectations run through a
small built-in evaluator, so CI on a bare agent still gets the checks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

try:                                                # pragma: no cover
    import great_expectations as gx
    # `from_pandas` is the 0.x convenience entry point. Great Expectations 1.x
    # replaced it with a context/datasource/batch-definition flow that needs
    # project state on disk, which a stateless equivalence check should not be
    # creating. Detect the API we can actually use rather than the package.
    GE_AVAILABLE = hasattr(gx, "from_pandas")
    if not GE_AVAILABLE:
        logger.debug("great_expectations %s does not expose from_pandas; "
                     "using the built-in evaluator",
                     getattr(gx, "__version__", "?"))
except ImportError:
    gx = None
    GE_AVAILABLE = False

# A column with at most this many distinct values is treated as categorical
# and gets a value-set expectation rather than a range.
CATEGORICAL_MAX_CARDINALITY = 25


@dataclass
class Expectation:
    type: str
    column: Optional[str]
    kwargs: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        target = self.column or "<table>"
        return "%s(%s, %s)" % (self.type, target, self.kwargs)


@dataclass
class SuiteResult:
    unit: str
    side: str
    evaluated: int = 0
    failed: List[str] = field(default_factory=list)
    engine: str = "builtin"

    @property
    def passed(self) -> bool:
        return not self.failed

    @property
    def success_rate(self) -> float:
        return 1.0 if not self.evaluated else 1 - len(self.failed) / self.evaluated


def _numeric(series: pd.Series) -> Optional[pd.Series]:
    converted = pd.to_numeric(series, errors="coerce")
    if converted.notna().sum() == 0:
        return None
    if converted.isna().sum() > (series.astype(str).str.strip() == "").sum():
        return None                                 # some values are genuinely non-numeric
    return converted


def build_suite(legacy: pd.DataFrame,
                key_columns: Optional[Sequence[str]] = None) -> List[Expectation]:
    """Infer expectations from the legacy job's observed behaviour."""
    expectations: List[Expectation] = [
        Expectation("expect_table_columns_to_match_ordered_list", None,
                    {"column_list": list(legacy.columns)}),
        Expectation("expect_table_row_count_to_be_between", None,
                    # +/-10%: the shape of the output should not move much on
                    # comparable input, but the sample is not the population.
                    {"min_value": int(len(legacy) * 0.9),
                     "max_value": int(len(legacy) * 1.1) + 1}),
    ]

    for column in legacy.columns:
        series = legacy[column].astype(str)
        blank_rate = float((series.str.strip() == "").mean())

        if blank_rate == 0.0:
            expectations.append(
                Expectation("expect_column_values_to_not_be_null", column, {}))
        else:
            expectations.append(
                Expectation("expect_column_null_rate_to_be_close_to", column,
                            {"null_rate": round(blank_rate, 6), "atol": 0.02}))

        numeric = _numeric(series)
        if numeric is not None:
            expectations.append(
                Expectation("expect_column_values_to_be_between", column,
                            {"min_value": float(numeric.min()),
                             "max_value": float(numeric.max())}))
        elif series.nunique() <= CATEGORICAL_MAX_CARDINALITY:
            expectations.append(
                Expectation("expect_column_values_to_be_in_set", column,
                            {"value_set": sorted(series.unique().tolist())}))

    for column in key_columns or []:
        if column in legacy.columns:
            expectations.append(
                Expectation("expect_column_values_to_be_unique", column, {}))

    if key_columns and len(key_columns) > 1:
        expectations.append(
            Expectation("expect_compound_columns_to_be_unique", None,
                        {"column_list": list(key_columns)}))

    logger.debug("inferred %d expectations from legacy output", len(expectations))
    return expectations


def evaluate(frame: pd.DataFrame,
             expectations: Sequence[Expectation],
             unit: str,
             side: str) -> SuiteResult:
    """Run the suite. Uses Great Expectations when installed."""
    if GE_AVAILABLE:                                # pragma: no cover
        return _evaluate_with_ge(frame, expectations, unit, side)
    return _evaluate_builtin(frame, expectations, unit, side)


def _evaluate_builtin(frame: pd.DataFrame,
                      expectations: Sequence[Expectation],
                      unit: str,
                      side: str) -> SuiteResult:
    result = SuiteResult(unit=unit, side=side, engine="builtin")

    for expectation in expectations:
        result.evaluated += 1
        kind, column, kwargs = expectation.type, expectation.column, expectation.kwargs

        if column is not None and column not in frame.columns:
            result.failed.append("%s: column absent" % expectation.describe())
            continue

        series = frame[column].astype(str) if column else None

        if kind == "expect_table_columns_to_match_ordered_list":
            if list(frame.columns) != list(kwargs["column_list"]):
                result.failed.append("column list/order differs: %s" % list(frame.columns))

        elif kind == "expect_table_row_count_to_be_between":
            if not kwargs["min_value"] <= len(frame) <= kwargs["max_value"]:
                result.failed.append("row count %d outside [%d, %d]"
                                     % (len(frame), kwargs["min_value"], kwargs["max_value"]))

        elif kind == "expect_column_values_to_not_be_null":
            blanks = int((series.str.strip() == "").sum())
            if blanks:
                result.failed.append("%s: %d blank value(s) where the legacy job "
                                     "had none" % (column, blanks))

        elif kind == "expect_column_null_rate_to_be_close_to":
            observed = float((series.str.strip() == "").mean())
            if abs(observed - kwargs["null_rate"]) > kwargs["atol"]:
                result.failed.append("%s: null rate %.4f, legacy %.4f"
                                     % (column, observed, kwargs["null_rate"]))

        elif kind == "expect_column_values_to_be_between":
            numeric = pd.to_numeric(series, errors="coerce").dropna()
            if len(numeric) and (numeric.min() < kwargs["min_value"]
                                 or numeric.max() > kwargs["max_value"]):
                result.failed.append("%s: range [%g, %g] escapes legacy [%g, %g]"
                                     % (column, numeric.min(), numeric.max(),
                                        kwargs["min_value"], kwargs["max_value"]))

        elif kind == "expect_column_values_to_be_in_set":
            unseen = sorted(set(series.unique()) - set(kwargs["value_set"]))
            if unseen:
                result.failed.append("%s: value(s) the legacy job never produced: %s"
                                     % (column, unseen[:5]))

        elif kind == "expect_column_values_to_be_unique":
            duplicates = int(series.duplicated().sum())
            if duplicates:
                result.failed.append("%s: %d duplicate key value(s)" % (column, duplicates))

        elif kind == "expect_compound_columns_to_be_unique":
            columns = [c for c in kwargs["column_list"] if c in frame.columns]
            duplicates = int(frame[columns].duplicated().sum())
            if duplicates:
                result.failed.append("compound key %s: %d duplicate(s)"
                                     % (columns, duplicates))

        else:                                       # pragma: no cover
            result.evaluated -= 1
            logger.debug("unhandled expectation type %s", kind)

    return result


def _evaluate_with_ge(frame: pd.DataFrame,
                      expectations: Sequence[Expectation],
                      unit: str,
                      side: str) -> SuiteResult:   # pragma: no cover
    """Delegate the expectations GE implements natively; keep the rest local."""
    native = {
        "expect_table_columns_to_match_ordered_list",
        "expect_table_row_count_to_be_between",
        "expect_column_values_to_not_be_null",
        "expect_column_values_to_be_between",
        "expect_column_values_to_be_in_set",
        "expect_column_values_to_be_unique",
        "expect_compound_columns_to_be_unique",
    }
    result = SuiteResult(unit=unit, side=side, engine="great_expectations")
    validator = gx.from_pandas(frame)

    for expectation in expectations:
        if expectation.type not in native:
            continue
        result.evaluated += 1
        method = getattr(validator, expectation.type)
        kwargs = dict(expectation.kwargs)
        if expectation.column is not None:
            kwargs["column"] = expectation.column
        outcome = method(**kwargs)
        if not outcome.success:
            result.failed.append(expectation.describe())

    local = [e for e in expectations if e.type not in native]
    if local:
        fallback = _evaluate_builtin(frame, local, unit, side)
        result.evaluated += fallback.evaluated
        result.failed.extend(fallback.failed)

    return result


def compare(unit: str,
            legacy: pd.DataFrame,
            translated: pd.DataFrame,
            key_columns: Optional[Sequence[str]] = None) -> Dict[str, SuiteResult]:
    """Infer a suite from the legacy output and run it against both sides."""
    suite = build_suite(legacy, key_columns)
    return {
        "legacy": evaluate(legacy, suite, unit, "legacy"),
        "translated": evaluate(translated, suite, unit, "translated"),
    }
