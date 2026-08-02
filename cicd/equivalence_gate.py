"""Azure DevOps quality gate. Exit 1 when a translation is not equivalent.

This is the control that makes the rest of the framework worth having. A
migration programme without it ships translations on the strength of a code
review; with it, nothing reaches production until it has been executed against
the legacy behaviour and scored.

    python -m cicd.equivalence_gate                      # score, then gate
    python -m cicd.equivalence_gate --report reports/equivalence_report.json
    python -m cicd.equivalence_gate --threshold 99.5 --junit junit/equivalence.xml

Exit codes:
    0   every unit at or above the threshold
    1   at least one unit blocked - do not promote
    2   the gate could not run (missing corpus, unreadable report)

Promotion is per unit, not per build: a blocked unit stays on the legacy stack
while its siblings move. Anything else turns one bad translation into a stalled
programme, which is how equivalence gates get switched off.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import EQUIVALENCE_THRESHOLD, load_manifest                  # noqa: E402
from migration_pipeline import format_table, run_pipeline                # noqa: E402

logger = logging.getLogger(__name__)

# A blocked unit at this criticality fails the build even under --soft.
BLOCKING_CRITICALITY = {"critical"}


def _units_from_report(payload: Dict) -> List[Dict]:
    return payload.get("units", [])


def load_report(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_junit(units: List[Dict], threshold: float, path: Path) -> Path:
    """Emit JUnit XML so Azure DevOps renders per-unit results natively."""
    failures = [u for u in units if not u["passed"]]
    cases = []
    for unit in units:
        name = escape(unit["unit"])
        classname = "equivalence.%s" % escape(unit.get("owner", "unassigned"))
        score = unit["score"]["score"]
        if unit["passed"]:
            cases.append('    <testcase classname="%s" name="%s" time="%.3f"/>'
                         % (classname, name, unit.get("legacy_ms", 0) / 1000.0))
        else:
            detail = escape("\n".join(unit["score"].get("findings", []))
                            or (unit["score"].get("error") or "below threshold"))
            cases.append(
                '    <testcase classname="%s" name="%s" time="%.3f">\n'
                '      <failure message="equivalence %.3f%% &lt; %.1f%%">%s</failure>\n'
                '    </testcase>' % (classname, name, unit.get("legacy_ms", 0) / 1000.0,
                                     score, threshold, detail))

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<testsuites>\n'
           '  <testsuite name="equivalence-gate" tests="%d" failures="%d">\n'
           '%s\n'
           '  </testsuite>\n'
           '</testsuites>\n'
           % (len(units), len(failures), "\n".join(cases)))

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(xml)
    return path


def gate(units: List[Dict], threshold: float, soft: bool = False) -> int:
    passed = [u for u in units if u["passed"]]
    blocked = [u for u in units if not u["passed"]]

    print("\n" + "=" * 78)
    print("EQUIVALENCE GATE   threshold %.1f%%   %d/%d units cleared"
          % (threshold, len(passed), len(units)))
    print("=" * 78)

    if not blocked:
        print("\nAll units cleared the gate. Safe to promote.")
        return 0

    for unit in blocked:
        criticality = unit.get("criticality", "medium")
        print("\nBLOCKED  %s  (%.3f%%, owner=%s, criticality=%s)"
              % (unit["unit"], unit["score"]["score"],
                 unit.get("owner", "?"), criticality))
        for finding in unit["score"].get("findings", []):
            print("    - %s" % finding)
        if unit.get("validation_errors"):
            for error in unit["validation_errors"]:
                print("    - static: %s" % error)
        print("    -> stays on the legacy stack; re-run the translator with the "
              "diff attached and re-score before promoting")

    print("\n%d unit(s) blocked. %d unit(s) may still be promoted independently: %s"
          % (len(blocked), len(passed),
             ", ".join(u["unit"] for u in passed) or "none"))

    if soft:
        critical = [u for u in blocked
                    if u.get("criticality") in BLOCKING_CRITICALITY]
        if not critical:
            print("\n--soft: no blocked unit is business-critical, build continues.")
            return 0
        print("\n--soft ignored: %s is business-critical."
              % ", ".join(u["unit"] for u in critical))

    return 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", type=Path,
                    help="score an existing report instead of re-running the pipeline")
    ap.add_argument("--threshold", type=float, default=EQUIVALENCE_THRESHOLD)
    ap.add_argument("--junit", type=Path, help="write JUnit XML here")
    ap.add_argument("--soft", action="store_true",
                    help="pass the build unless a business-critical unit is blocked")
    ap.add_argument("--no-perl", action="store_true",
                    help="force golden replay even if perl is installed")
    args = ap.parse_args()

    if args.report:
        if not args.report.exists():
            logger.error("no report at %s", args.report)
            return 2
        payload = load_report(args.report)
        units = _units_from_report(payload)
        threshold = args.threshold or payload.get("threshold", EQUIVALENCE_THRESHOLD)
    else:
        try:
            manifest = load_manifest()
        except Exception as exc:                    # noqa: BLE001
            logger.error("cannot load the legacy corpus manifest: %s", exc)
            return 2
        report = run_pipeline(manifest,
                              threshold=args.threshold,
                              prefer_subprocess=not args.no_perl)
        print(format_table(report))
        units = report.to_dict()["units"]
        threshold = args.threshold

    if args.junit:
        path = write_junit(units, threshold, args.junit)
        logger.info("JUnit results written to %s", path)

    if not units:
        logger.error("no units were evaluated - refusing to pass an empty gate")
        return 2

    return gate(units, threshold, soft=args.soft)


if __name__ == "__main__":
    raise SystemExit(main())
