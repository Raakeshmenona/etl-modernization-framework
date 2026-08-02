"""Unit tests for the migration framework.

The tests that matter most here are the ones that check the harness can still
FAIL. A gate that has quietly stopped detecting divergence passes every build
and is worse than no gate at all, so several tests deliberately feed the
scorer broken output and assert it is rejected.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import EQUIVALENCE_THRESHOLD, load_manifest, unit_by_name
from equivalence import dual_executor, equivalence_scorer, output_differ
from equivalence.great_expectations_suite import build_suite, compare, evaluate
from equivalence.perl_semantics import (days_between, perl_int, perl_num,
                                        perl_sort, perl_split, perl_sprintf_f,
                                        perl_true)
from equivalence.regression_generator import build as build_regression
from equivalence.regression_generator import select_cases
from parsers import dependency_mapper, perl_ast_extractor, sql_semantic_parser
from translator.translation_validator import validate

PERL_AVAILABLE = shutil.which("perl") is not None


# --------------------------------------------------------------------------- #
# Perl semantics
# --------------------------------------------------------------------------- #

def test_perl_split_drops_trailing_empty_fields():
    # The defect the framework is built to catch.
    assert perl_split(r"\|", "a|b||") == ["a", "b"]
    assert "a|b||".split("|") == ["a", "b", "", ""]


def test_perl_split_negative_limit_keeps_them():
    assert perl_split(r"\|", "a|b||", -1) == ["a", "b", "", ""]


def test_perl_split_keeps_leading_empty_field():
    assert perl_split(r",", ",a,b") == ["", "a", "b"]


def test_perl_num_never_raises_and_never_nans():
    assert perl_num(None) == 0.0
    assert perl_num("") == 0.0
    assert perl_num("abc") == 0.0
    assert perl_num("12abc") == 12.0
    assert perl_num("  -3.5e2xyz") == -350.0
    assert perl_num(7) == 7.0


def test_perl_int_truncates_toward_zero():
    assert perl_int(3.9) == 3
    assert perl_int(-3.9) == -3
    assert round(-3.9) == -4          # what a naive translation would produce


def test_perl_true_treats_zero_string_as_false_but_not_zero_point_zero():
    assert perl_true("0") is False
    assert perl_true("0.0") is True
    assert perl_true("00") is True
    assert perl_true("") is False
    assert perl_true(0) is False


def test_perl_sort_is_lexicographic():
    assert perl_sort([1, 2, 10, 20, 100]) == ["1", "10", "100", "2", "20"]


def test_perl_sprintf_matches_python_format_not_round():
    assert perl_sprintf_f(2.675, 2) == "2.67"     # both defer to the double
    assert perl_sprintf_f(1.005, 2) == "1.00"


def test_days_between_matches_civil_calendar():
    assert days_between("2026-01-01", "2026-01-01") == 0
    assert days_between("2026-01-01", "2026-12-31") == 364
    assert days_between("2024-02-28", "2024-03-01") == 2     # leap year
    assert days_between("2026-07-01", "2026-01-01") == -181


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #

def test_perl_extractor_flags_split_without_limit():
    unit = unit_by_name("null_handling_report")
    analysis = perl_ast_extractor.extract(unit.read_source(), unit.name)
    assert "split_no_limit" in analysis.risk_kinds()


def test_perl_extractor_does_not_flag_split_with_negative_limit():
    unit = unit_by_name("customer_dedupe")
    analysis = perl_ast_extractor.extract(unit.read_source(), unit.name)
    assert "split_no_limit" not in analysis.risk_kinds()


def test_perl_extractor_finds_constants_and_subs():
    unit = unit_by_name("account_status_derive")
    analysis = perl_ast_extractor.extract(unit.read_source(), unit.name)
    assert "days_between" in analysis.subs
    assert analysis.constants.get("DORMANT_DAYS") == "90"


def test_sql_parser_detects_oracle_outer_join_direction():
    unit = unit_by_name("legacy_oracle_balances")
    analysis = sql_semantic_parser.parse(unit.read_source(), unit.name, "oracle")
    kinds = analysis.feature_kinds()
    assert "oracle_outer_join" in kinds
    assert "nvl" in kinds
    assert "decode" in kinds


def test_sql_parser_detects_teradata_qualify_and_rank():
    unit = unit_by_name("legacy_teradata_ranking")
    analysis = sql_semantic_parser.parse(unit.read_source(), unit.name, "teradata")
    kinds = analysis.feature_kinds()
    assert "qualify" in kinds
    assert "rank_vs_row_number" in kinds


def test_dependency_graph_has_no_cycles_and_orders_upstream_first():
    graph = dependency_mapper.build()
    order = dependency_mapper.topological_order(graph)
    assert len(order) == len(graph.nodes)
    for source, target in graph.edges:
        assert order.index(source) < order.index(target)


def test_dependency_mapper_surfaces_undocumented_coupling():
    # accounts.csv is read by two units but the manifest declares no edge
    # between them; the mapper must not let that pass silently.
    graph = dependency_mapper.build()
    assert graph.undocumented_edges


# --------------------------------------------------------------------------- #
# Translation validator
# --------------------------------------------------------------------------- #

GOOD = '''
import pandas as pd
def run(inputs):
    return pd.DataFrame({"a": [1]})
'''


def test_validator_accepts_a_well_formed_module():
    assert validate(GOOD, "good", ["a"]).ok


def test_validator_rejects_forbidden_import():
    result = validate("import subprocess\ndef run(inputs):\n    return None\n", "bad")
    assert not result.ok
    assert any("subprocess" in e for e in result.errors)


def test_validator_rejects_missing_run_function():
    result = validate("import pandas as pd\nx = 1\n", "bad")
    assert not result.ok


def test_validator_rejects_wrong_signature():
    result = validate("def run(df):\n    return df\n", "bad")
    assert not result.ok


def test_validator_rejects_missing_declared_output_column():
    result = validate(GOOD, "bad", ["a", "missing_column"])
    assert not result.ok
    assert any("missing_column" in e for e in result.errors)


def test_validator_warns_on_date_inference():
    code = GOOD + "\nimport pandas\nx = pandas.to_datetime(['1/2/2026'])\n"
    result = validate(code, "smelly", ["a"])
    assert any("inferred_dates" in w for w in result.warnings)


def test_every_cached_translation_passes_static_validation():
    for unit in load_manifest():
        assert unit.translation_path.exists(), "%s has no cached translation" % unit.name
        result = validate(unit.translation_path.read_text(encoding="utf-8"),
                          unit.name, unit.output_columns)
        assert result.ok, "%s: %s" % (unit.name, result.errors)


# --------------------------------------------------------------------------- #
# Differ and scorer
# --------------------------------------------------------------------------- #

def _frame(rows):
    return pd.DataFrame(rows, dtype=str)


def test_identical_frames_score_100():
    frame = _frame([{"k": "1", "v": "10"}, {"k": "2", "v": "20"}])
    result = output_differ.diff(frame, frame.copy(), ["k"])
    score = equivalence_scorer.score("t", frame, frame.copy(), result,
                                     EQUIVALENCE_THRESHOLD)
    assert score.score == 100.0
    assert score.passed


def test_single_wrong_cell_is_detected_and_blocks():
    legacy = _frame([{"k": "1", "v": "10"}, {"k": "2", "v": "20"}])
    broken = _frame([{"k": "1", "v": "10"}, {"k": "2", "v": "99"}])
    result = output_differ.diff(legacy, broken, ["k"])
    assert result.cells_mismatched == 1
    score = equivalence_scorer.score("t", legacy, broken, result, EQUIVALENCE_THRESHOLD)
    assert not score.passed


def test_dropped_row_is_penalised_on_the_row_set_component():
    legacy = _frame([{"k": "1", "v": "10"}, {"k": "2", "v": "20"}])
    short = _frame([{"k": "1", "v": "10"}])
    result = output_differ.diff(legacy, short, ["k"])
    assert result.missing_in_translated == ["2"]
    score = equivalence_scorer.score("t", legacy, short, result, EQUIVALENCE_THRESHOLD)
    assert score.components["cell_value"] == 1.0      # every aligned cell agrees
    assert score.components["row_set"] < 1.0          # but a row went missing
    assert not score.passed


def test_reordered_rows_are_caught_even_though_every_cell_matches():
    legacy = _frame([{"k": "1", "v": "10"}, {"k": "2", "v": "20"}])
    shuffled = _frame([{"k": "2", "v": "20"}, {"k": "1", "v": "10"}])
    result = output_differ.diff(legacy, shuffled, ["k"])
    assert result.cells_mismatched == 0
    assert not result.ordering_matches
    score = equivalence_scorer.score("t", legacy, shuffled, result, EQUIVALENCE_THRESHOLD)
    assert score.components["ordering"] == 0.0
    assert not score.passed


def test_column_reorder_is_penalised_on_schema():
    legacy = _frame([{"a": "1", "b": "2"}])
    swapped = legacy[["b", "a"]]
    result = output_differ.diff(legacy, swapped, [])
    score = equivalence_scorer.score("t", legacy, swapped, result, EQUIVALENCE_THRESHOLD)
    assert score.components["schema"] < 1.0
    assert not score.passed


def test_tolerance_absorbs_a_rounding_difference_but_not_a_real_one():
    legacy = _frame([{"k": "1", "v": "10.00"}])
    close = _frame([{"k": "1", "v": "10.004"}])
    far = _frame([{"k": "1", "v": "10.20"}])
    assert output_differ.diff(legacy, close, ["k"], tolerance=0.005).cells_mismatched == 0
    assert output_differ.diff(legacy, far, ["k"], tolerance=0.005).cells_mismatched == 1


def test_numeric_and_string_columns_are_compared_differently():
    legacy = _frame([{"n": "1.0", "s": "abc"}])
    other = _frame([{"n": "1", "s": "ABC"}])
    result = output_differ.diff(legacy, other, [])
    assert result.columns["n"].comparison == "numeric"
    assert result.columns["n"].mismatched == 0        # 1.0 == 1 numerically
    assert result.columns["s"].comparison == "string"
    assert result.columns["s"].mismatched == 1        # case matters


def test_failed_execution_scores_zero_not_null():
    score = equivalence_scorer.failed_score("t", EQUIVALENCE_THRESHOLD, "boom")
    assert score.score == 0.0
    assert not score.passed
    assert score.error == "boom"


def test_score_weights_sum_to_one():
    assert abs(sum(equivalence_scorer.WEIGHTS.values()) - 1.0) < 1e-9


# --------------------------------------------------------------------------- #
# Expectation suites
# --------------------------------------------------------------------------- #

def test_inferred_suite_always_holds_on_its_own_source_frame():
    frame = _frame([{"k": "1", "cat": "A", "n": "10"},
                    {"k": "2", "cat": "B", "n": "20"}])
    suite = build_suite(frame, ["k"])
    assert evaluate(frame, suite, "t", "legacy").passed


def test_suite_catches_a_category_the_legacy_job_never_produced():
    legacy = _frame([{"k": "1", "cat": "A"}, {"k": "2", "cat": "B"}])
    translated = _frame([{"k": "1", "cat": "A"}, {"k": "2", "cat": "UNKNOWN"}])
    results = compare("t", legacy, translated, ["k"])
    assert results["legacy"].passed
    assert not results["translated"].passed


def test_suite_catches_a_duplicated_key():
    legacy = _frame([{"k": "1"}, {"k": "2"}])
    translated = _frame([{"k": "1"}, {"k": "1"}])
    results = compare("t", legacy, translated, ["k"])
    assert not results["translated"].passed


# --------------------------------------------------------------------------- #
# Regression generator
# --------------------------------------------------------------------------- #

def test_regression_cases_prioritise_divergent_rows():
    legacy = _frame([{"k": str(i), "v": str(i * 2)} for i in range(30)])
    cases = select_cases(legacy, ["k"], divergent_keys=["7", "9"])
    reasons = [c.reason for c in cases]
    assert reasons[0].startswith("divergent")
    assert {c.key["k"] for c in cases if c.reason == "divergent"} == {"7", "9"}


def test_regression_suite_pins_schema_and_totals():
    unit = unit_by_name("fx_revenue_calc")
    run = dual_executor.execute(unit, prefer_subprocess=False)
    suite = build_regression(unit, run.legacy.frame)
    assert suite.columns == unit.output_columns
    assert suite.row_count == len(run.legacy.frame)
    assert "total_commission" in suite.numeric_totals
    assert suite.cases


def test_regression_case_families_stay_diverse_when_everything_diverges():
    legacy = _frame([{"k": str(i), "cat": "A" if i % 2 else "B", "n": str(i)}
                     for i in range(200)])
    cases = select_cases(legacy, ["k"], divergent_keys=[str(i) for i in range(200)])
    families = {c.reason.split(":")[0] for c in cases}
    assert len(families) > 1, "divergent rows crowded out every other family"


# --------------------------------------------------------------------------- #
# End-to-end behaviour of the corpus
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("unit", load_manifest(), ids=lambda u: u.name)
def test_every_unit_executes_on_both_sides(unit):
    run = dual_executor.execute(unit, prefer_subprocess=False)
    assert run.legacy.ok, "legacy: %s" % run.legacy.error
    assert run.translated.ok, "translated: %s" % run.translated.error
    assert list(run.translated.frame.columns) == unit.output_columns


def test_the_planted_defect_is_still_caught():
    """If this passes, the gate has been weakened - not the translation fixed."""
    unit = unit_by_name("null_handling_report")
    run = dual_executor.execute(unit, prefer_subprocess=False)
    result = output_differ.diff(run.legacy.frame, run.translated.frame,
                                unit.key_columns, unit.tolerance)
    score = equivalence_scorer.score(unit.name, run.legacy.frame,
                                     run.translated.frame, result,
                                     EQUIVALENCE_THRESHOLD)
    assert not score.passed
    assert "field_count" in " ".join(score.findings)


def test_every_other_unit_is_equivalent():
    for unit in load_manifest():
        if unit.name == "null_handling_report":
            continue
        run = dual_executor.execute(unit, prefer_subprocess=False)
        result = output_differ.diff(run.legacy.frame, run.translated.frame,
                                    unit.key_columns, unit.tolerance)
        score = equivalence_scorer.score(unit.name, run.legacy.frame,
                                         run.translated.frame, result,
                                         EQUIVALENCE_THRESHOLD)
        assert score.passed, "%s scored %.3f: %s" % (unit.name, score.score,
                                                     score.findings)


@pytest.mark.skipif(not PERL_AVAILABLE, reason="perl is not installed")
@pytest.mark.parametrize("unit", [u for u in load_manifest() if u.language == "perl"],
                         ids=lambda u: u.name)
def test_reference_implementation_matches_real_perl(unit):
    """The reference impls back offline golden capture - they must be exact.

    Without this, a replayed snapshot is an assertion rather than evidence.
    """
    argv = ["perl", str(unit.source_path)] + list(unit.input_paths().values())
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr

    reference = dual_executor.ReferenceRunner().run(unit)
    assert reference.ok, reference.error

    from_perl = dual_executor.parse_csv_text(completed.stdout)
    pd.testing.assert_frame_equal(from_perl, reference.frame)


def test_golden_snapshots_exist_and_record_their_provenance():
    manifest_path = Path(__file__).resolve().parents[1] / "data" / "golden" / "_manifest.json"
    assert manifest_path.exists(), "run data/capture_golden.py"
    provenance = json.loads(manifest_path.read_text(encoding="utf-8"))
    for unit in load_manifest():
        assert unit.golden_path.exists(), "no golden snapshot for %s" % unit.name
        entry = provenance[unit.name]
        assert entry["status"] == "captured"
        assert entry["sha256"]
        assert "authoritative" in entry
