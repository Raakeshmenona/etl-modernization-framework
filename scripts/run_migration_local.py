"""End-to-end migration run on the sample corpus. No cloud services required.

    python scripts/run_migration_local.py              # full run + report
    python scripts/run_migration_local.py --unit fx_revenue_calc --verbose
    python scripts/run_migration_local.py --validate   # exit 1 if KPIs regress

`--validate` is what the Azure DevOps pipeline calls: it re-checks the KPI
targets the framework claims, so the build fails if a change quietly degrades
translation accuracy rather than only when a unit outright breaks.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import EQUIVALENCE_THRESHOLD, REPORT_DIR, load_manifest       # noqa: E402
from migration_pipeline import format_table, run_pipeline                 # noqa: E402
from parsers import dependency_mapper                                     # noqa: E402

logger = logging.getLogger(__name__)

# KPI floors asserted by --validate. These are the numbers the README claims.
MIN_AUTO_TRANSLATION_RATE = 85.0
MIN_MEAN_SCORE = 95.0
# The planted defect must stay caught: if this unit starts passing, either the
# corpus or the differ has been weakened.
MUST_BE_BLOCKED = {"null_handling_report"}


def write_markdown(report, path: Path) -> Path:
    lines = [
        "# Migration equivalence report",
        "",
        "| field | value |",
        "|---|---|",
        "| run id | `%s` |" % report.run_id,
        "| started | %s |" % report.started_at,
        "| translation model | `%s` |" % report.model,
        "| legacy runner | `%s` |" % report.legacy_runner,
        "| gate threshold | %.1f%% |" % report.threshold,
        "| units | %d |" % len(report.results),
        "| passed | %d |" % len(report.passed),
        "| blocked | %d |" % len(report.blocked),
        "| auto-translation rate | %.1f%% |" % report.auto_translation_rate,
        "| mean equivalence score | %.2f%% |" % report.mean_score,
        "",
        "## Per-unit results",
        "",
        "| unit | owner | criticality | score | gate | risk flags | regression cases |",
        "|---|---|---|---:|---|---|---:|",
    ]
    for result in report.results:
        lines.append("| `%s` | %s | %s | %.3f%% | %s | %s | %d |"
                     % (result.unit, result.owner, result.criticality,
                        result.score.score,
                        "PASS" if result.passed else "**BLOCKED**",
                        ", ".join(result.risk_flags) or "-",
                        result.regression_cases))

    for result in report.blocked:
        lines.extend(["", "## Blocked: `%s`" % result.unit, ""])
        for finding in result.score.findings:
            lines.append("- %s" % finding)
        if result.diff_summary:
            lines.extend(["", "```", result.diff_summary, "```"])

    lines.extend(["", "## Migration waves", ""])
    for index, wave in enumerate(report.migration_waves):
        lines.append("%d. %s" % (index, ", ".join("`%s`" % n for n in wave)))

    if report.undocumented_dependencies:
        lines.extend(["", "## Undocumented coupling", "",
                      "Shared inputs the manifest does not declare as dependencies:",
                      ""])
        for source, target in report.undocumented_dependencies:
            lines.append("- `%s` -> `%s`" % (source, target))

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unit", help="run a single unit by name")
    ap.add_argument("--threshold", type=float, default=EQUIVALENCE_THRESHOLD)
    ap.add_argument("--no-regression", action="store_true",
                    help="skip regression-suite generation")
    ap.add_argument("--no-perl", action="store_true",
                    help="force golden replay even if perl is installed")
    ap.add_argument("--validate", action="store_true",
                    help="assert the KPI floors; exit 1 if they regress")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")

    units = load_manifest()
    if args.unit:
        units = [u for u in units if u.name == args.unit]
        if not units:
            logger.error("no unit named %r in the manifest", args.unit)
            return 2

    graph = dependency_mapper.build(load_manifest())
    logger.info("estate: %d units, %d dependency edges, %d migration waves",
                len(graph.nodes), len(graph.edges),
                len(dependency_mapper.migration_waves(graph)))

    report = run_pipeline(units,
                          threshold=args.threshold,
                          prefer_subprocess=not args.no_perl,
                          write_regression=not args.no_regression)

    print(format_table(report))

    markdown = write_markdown(report, REPORT_DIR / "equivalence_report.md")
    logger.info("\nreports: %s", markdown.parent)

    for result in report.blocked:
        print("\nBLOCKED  %s" % result.unit)
        for finding in result.score.findings:
            print("    - %s" % finding)

    if args.validate:
        return _validate(report)
    return 0


def _validate(report) -> int:
    failures = []

    if report.auto_translation_rate < MIN_AUTO_TRANSLATION_RATE:
        failures.append("auto-translation rate %.1f%% is below the %.1f%% floor"
                        % (report.auto_translation_rate, MIN_AUTO_TRANSLATION_RATE))
    if report.mean_score < MIN_MEAN_SCORE:
        failures.append("mean equivalence score %.2f%% is below the %.1f%% floor"
                        % (report.mean_score, MIN_MEAN_SCORE))

    blocked = {r.unit for r in report.blocked}
    evaluated = {r.unit for r in report.results}
    for expected in MUST_BE_BLOCKED & evaluated:
        if expected not in blocked:
            failures.append("%s was expected to be caught by the gate but passed - "
                            "the differ or the corpus has been weakened" % expected)

    for result in report.results:
        if result.score.error and "not evaluated" not in result.score.error:
            failures.append("%s failed to execute: %s" % (result.unit, result.score.error))

    if failures:
        print("\nKPI VALIDATION FAILED")
        for failure in failures:
            print("  - %s" % failure)
        return 1

    print("\nKPI validation passed: auto-translation %.1f%% (floor %.1f%%), "
          "mean score %.2f%% (floor %.1f%%), %d planted defect(s) still caught"
          % (report.auto_translation_rate, MIN_AUTO_TRANSLATION_RATE,
             report.mean_score, MIN_MEAN_SCORE, len(MUST_BE_BLOCKED & evaluated)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
