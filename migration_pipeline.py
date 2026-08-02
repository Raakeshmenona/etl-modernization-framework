"""The migration pipeline, in one place.

Every entry point drives this module rather than reimplementing the sequence:
the local runner (`scripts/run_migration_local.py`), the CI quality gate
(`cicd/equivalence_gate.py`) and the Airflow DAG
(`airflow_dags/migration_pipeline_dag.py`) all call `run_pipeline`. The DAG
splits it across tasks for retry granularity and observability; the logic is
the same code, so a green local run means the same thing as a green build.

Per unit:

    parse       static analysis of the legacy source -> risk flags
    translate   cache-backed Claude translation (regenerated on source change)
    validate    static gate on the generated module, before it is imported
    execute     legacy and translated over identical inputs
    diff        key-aligned, cell-level comparison
    score       weighted equivalence score against the promotion threshold
    expect      inferred Great Expectations suite run on both sides
    regress     pytest regression suite pinned to the legacy behaviour
"""
from __future__ import annotations

import json
import logging
import platform
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from config import (EQUIVALENCE_THRESHOLD, REPORT_DIR, TRANSLATION_MODEL, Unit,
                    load_manifest)
from equivalence import (dual_executor, equivalence_scorer, great_expectations_suite,
                         output_differ, regression_generator)
from equivalence.equivalence_scorer import EquivalenceScore
from parsers import dependency_mapper, perl_ast_extractor, sql_semantic_parser
from translator import llm_code_generator
from translator.translation_validator import validate

logger = logging.getLogger(__name__)


@dataclass
class UnitResult:
    unit: str
    owner: str
    criticality: str
    language: str
    score: EquivalenceScore
    risk_flags: List[str] = field(default_factory=list)
    translation_source: str = "cache"
    validation_errors: List[str] = field(default_factory=list)
    validation_warnings: List[str] = field(default_factory=list)
    legacy_runner: str = ""
    legacy_ms: float = 0.0
    translated_ms: float = 0.0
    expectations_failed: List[str] = field(default_factory=list)
    expectation_engine: str = ""
    regression_cases: int = 0
    regression_path: Optional[str] = None
    diff_summary: str = ""

    @property
    def passed(self) -> bool:
        return self.score.passed and not self.validation_errors

    def to_dict(self) -> Dict:
        payload = asdict(self)
        payload["score"] = self.score.to_dict()
        payload["passed"] = self.passed
        return payload


@dataclass
class PipelineReport:
    run_id: str
    started_at: str
    threshold: float
    model: str
    legacy_runner: str
    python_version: str
    results: List[UnitResult] = field(default_factory=list)
    migration_waves: List[List[str]] = field(default_factory=list)
    undocumented_dependencies: List[List[str]] = field(default_factory=list)

    @property
    def passed(self) -> List[UnitResult]:
        return [r for r in self.results if r.passed]

    @property
    def blocked(self) -> List[UnitResult]:
        return [r for r in self.results if not r.passed]

    @property
    def auto_translation_rate(self) -> float:
        if not self.results:
            return 0.0
        return 100.0 * len(self.passed) / len(self.results)

    @property
    def mean_score(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.score.score for r in self.results) / len(self.results)

    def to_dict(self) -> Dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "threshold": self.threshold,
            "model": self.model,
            "legacy_runner": self.legacy_runner,
            "python_version": self.python_version,
            "unit_count": len(self.results),
            "passed": len(self.passed),
            "blocked": len(self.blocked),
            "auto_translation_rate": round(self.auto_translation_rate, 2),
            "mean_score": round(self.mean_score, 4),
            "migration_waves": self.migration_waves,
            "undocumented_dependencies": self.undocumented_dependencies,
            "units": [r.to_dict() for r in self.results],
        }


def _risk_flags(unit: Unit) -> List[str]:
    source = unit.read_source()
    if unit.language == "perl":
        return perl_ast_extractor.extract(source, unit.name).risk_kinds()
    return sql_semantic_parser.parse(source, unit.name,
                                     unit.dialect or "oracle",
                                     unit.target_dialect or "snowflake").feature_kinds()


def process_unit(unit: Unit,
                 threshold: float = EQUIVALENCE_THRESHOLD,
                 prefer_subprocess: bool = True,
                 write_regression: bool = True) -> UnitResult:
    """Run the full per-unit pipeline."""
    result = UnitResult(
        unit=unit.name,
        owner=unit.owner,
        criticality=unit.criticality,
        language=unit.language,
        score=equivalence_scorer.failed_score(unit.name, threshold, "not evaluated"),
        risk_flags=_risk_flags(unit),
    )

    generation = llm_code_generator.generate(unit)
    result.translation_source = generation.source
    if generation.code is None:
        result.score = equivalence_scorer.failed_score(
            unit.name, threshold,
            "no translation available: %s" % "; ".join(generation.errors))
        return result

    static = validate(generation.code, unit.name, unit.output_columns)
    result.validation_errors = list(static.errors)
    result.validation_warnings = list(static.warnings)
    if not static.ok:
        result.score = equivalence_scorer.failed_score(
            unit.name, threshold,
            "static validation rejected the module: %s" % "; ".join(static.errors))
        return result

    run = dual_executor.execute(unit, prefer_subprocess=prefer_subprocess)
    result.legacy_runner = run.legacy.runner
    result.legacy_ms = round(run.legacy.duration_ms, 2)
    result.translated_ms = round(run.translated.duration_ms, 2)

    if not run.both_ran:
        result.score = equivalence_scorer.failed_score(
            unit.name, threshold, run.legacy.error or run.translated.error)
        return result

    diff_result = output_differ.diff(run.legacy.frame, run.translated.frame,
                                     unit.key_columns, unit.tolerance)
    result.diff_summary = output_differ.summarise(diff_result)
    result.score = equivalence_scorer.score(unit.name, run.legacy.frame,
                                            run.translated.frame, diff_result, threshold)

    suites = great_expectations_suite.compare(unit.name, run.legacy.frame,
                                              run.translated.frame, unit.key_columns)
    result.expectation_engine = suites["translated"].engine
    result.expectations_failed = list(suites["translated"].failed)
    if suites["legacy"].failed:
        # The suite is inferred from this frame, so it should always hold on
        # the legacy side. If it does not, the inference is wrong, not the code.
        logger.warning("[%s] inferred suite does not hold on its own source frame: %s",
                       unit.name, suites["legacy"].failed[:2])

    if write_regression:
        suite = regression_generator.build(unit, run.legacy.frame, diff_result)
        path = regression_generator.write(suite, unit)
        result.regression_cases = len(suite.cases)
        result.regression_path = str(path.relative_to(Path.cwd())
                                     if str(path).startswith(str(Path.cwd())) else path)

    return result


def run_pipeline(units: Optional[List[Unit]] = None,
                 threshold: float = EQUIVALENCE_THRESHOLD,
                 prefer_subprocess: bool = True,
                 write_regression: bool = True,
                 write_report: bool = True) -> PipelineReport:
    units = units if units is not None else load_manifest()
    started = datetime.now(timezone.utc)

    graph = dependency_mapper.build(units)
    report = PipelineReport(
        run_id=started.strftime("%Y%m%dT%H%M%SZ"),
        started_at=started.isoformat(),
        threshold=threshold,
        model=TRANSLATION_MODEL,
        legacy_runner=dual_executor.legacy_runner(prefer_subprocess).name,
        python_version=platform.python_version(),
        migration_waves=dependency_mapper.migration_waves(graph),
        undocumented_dependencies=[list(e) for e in sorted(graph.undocumented_edges)],
    )

    # Migrate in dependency order: a unit whose upstream is still on the legacy
    # stack cannot be promoted, however well it scores.
    order = {name: i for i, name in enumerate(dependency_mapper.topological_order(graph))}
    for unit in sorted(units, key=lambda u: order.get(u.name, 0)):
        report.results.append(
            process_unit(unit, threshold, prefer_subprocess, write_regression))

    if write_report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / "equivalence_report.json"
        with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report.to_dict(), fh, indent=2)
            fh.write("\n")
        logger.info("report written to %s", path)

    return report


def format_table(report: PipelineReport) -> str:
    """Fixed-width summary for CI logs."""
    lines = [
        "",
        "%-24s %-12s %8s  %-6s %-9s %s"
        % ("unit", "owner", "score", "gate", "runner", "notes"),
        "-" * 100,
    ]
    for result in report.results:
        note = ""
        if result.validation_errors:
            note = "static: %s" % result.validation_errors[0][:40]
        elif result.score.findings:
            note = result.score.findings[0][:52]
        elif result.validation_warnings:
            note = "warn: %s" % result.validation_warnings[0][:46]
        lines.append("%-24s %-12s %8.3f  %-6s %-9s %s"
                     % (result.unit, result.owner[:12], result.score.score,
                        "PASS" if result.passed else "BLOCK",
                        result.translation_source, note))
    lines.extend([
        "-" * 100,
        "%d/%d units at or above %.1f%%   auto-translation rate %.1f%%   mean score %.2f%%"
        % (len(report.passed), len(report.results), report.threshold,
           report.auto_translation_rate, report.mean_score),
    ])
    return "\n".join(lines)
