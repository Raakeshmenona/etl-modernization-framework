"""Run the legacy job and its translation over the same inputs.

Two legacy runners, chosen automatically:

    PerlSubprocessRunner   `perl script.pl input.csv` in a sandboxed subprocess.
                           Authoritative. Used whenever `perl` is on PATH.

    GoldenReplayRunner     Replays a previously captured stdout snapshot from
                           data/golden/. This is how the pattern works in a real
                           migration - you capture legacy output once from the
                           system of record, then diff against it forever,
                           because CI agents do not get an Oracle instance or a
                           1998 Perl runtime. `capture_golden.py` produces the
                           snapshots, from real Perl if available and from the
                           documented reference implementations otherwise.

The translated side is always executed in-process: generated code exposes
`run(inputs: dict[str, str]) -> pandas.DataFrame | str`, so there is no shell
between the harness and the code under test.
"""
from __future__ import annotations

import importlib
import importlib.util
import io
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from config import PROJECT_ROOT, REFERENCE_PKG, Unit

logger = logging.getLogger(__name__)

LEGACY_TIMEOUT_SECONDS = 120


@dataclass
class ExecutionResult:
    """Output of one side of the dual run."""

    side: str                       # "legacy" | "translated"
    runner: str                     # which runner produced it
    frame: Optional[pd.DataFrame]
    duration_ms: float
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.frame is not None


def parse_csv_text(text: str) -> pd.DataFrame:
    """Parse legacy stdout into a frame, keeping every value as a string.

    String-typing both sides up front means the differ - not pandas' type
    inference - decides what counts as a numeric comparison. Otherwise a
    column that happens to be all-integers on one side and mixed on the other
    would diverge for reasons that have nothing to do with the translation.
    """
    if not text.strip():
        return pd.DataFrame()
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


def normalise(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce any frame to the all-string form the differ expects."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(frame.columns) if frame is not None else [])
    out = frame.copy()
    for column in out.columns:
        out[column] = out[column].map(_as_text)
    return out.reset_index(drop=True)


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        # 3.0 and "3" must not read as different values.
        return str(int(value))
    return str(value)


# --------------------------------------------------------------------------- #
# legacy runners
# --------------------------------------------------------------------------- #

def perl_available() -> bool:
    return shutil.which("perl") is not None


class PerlSubprocessRunner:
    """Authoritative path: execute the original Perl."""

    name = "perl-subprocess"

    def run(self, unit: Unit) -> ExecutionResult:
        argv = ["perl", str(unit.source_path)] + list(unit.input_paths().values())
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=LEGACY_TIMEOUT_SECONDS,
                cwd=str(PROJECT_ROOT),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ExecutionResult("legacy", self.name, None,
                                   (time.perf_counter() - started) * 1000, str(exc))

        duration_ms = (time.perf_counter() - started) * 1000
        if completed.returncode != 0:
            return ExecutionResult("legacy", self.name, None, duration_ms,
                                   "exit %d: %s" % (completed.returncode,
                                                    completed.stderr.strip()[:500]))
        if completed.stderr.strip():
            logger.warning("[%s] perl wrote to stderr: %s",
                           unit.name, completed.stderr.strip()[:200])
        return ExecutionResult("legacy", self.name,
                               parse_csv_text(completed.stdout), duration_ms)


class GoldenReplayRunner:
    """Replay a captured legacy snapshot."""

    name = "golden-replay"

    def run(self, unit: Unit) -> ExecutionResult:
        started = time.perf_counter()
        if not unit.golden_path.exists():
            return ExecutionResult(
                "legacy", self.name, None, 0.0,
                "no golden capture at %s - run data/capture_golden.py" % unit.golden_path)
        text = unit.golden_path.read_text(encoding="utf-8")
        return ExecutionResult("legacy", self.name, parse_csv_text(text),
                               (time.perf_counter() - started) * 1000)


class ReferenceRunner:
    """Execute the Perl-semantics reference implementation directly.

    Used by capture_golden.py to mint snapshots when Perl is unavailable.
    Never used as the legacy side of a scored comparison - that would be
    grading the translation against another Python program with no provenance.
    """

    name = "perl-semantics-reference"

    def run(self, unit: Unit) -> ExecutionResult:
        started = time.perf_counter()
        try:
            module = importlib.import_module("%s.%s" % (REFERENCE_PKG, unit.name))
            output = module.run(unit.input_paths())
        except Exception as exc:                              # noqa: BLE001
            return ExecutionResult("legacy", self.name, None,
                                   (time.perf_counter() - started) * 1000,
                                   "%s: %s" % (type(exc).__name__, exc))
        duration_ms = (time.perf_counter() - started) * 1000
        frame = parse_csv_text(output) if isinstance(output, str) else normalise(output)
        return ExecutionResult("legacy", self.name, frame, duration_ms)


def legacy_runner(prefer_subprocess: bool = True, unit: Optional[Unit] = None):
    """Pick the legacy runner for this unit on this machine.

    Only Perl units can be re-executed locally. SQL units always replay their
    captured snapshot - the legacy Oracle and Teradata instances are not
    reachable from a laptop or a build agent, which is the whole reason the
    capture-once-replay-forever pattern exists.
    """
    if unit is not None and unit.language != "perl":
        return GoldenReplayRunner()
    if prefer_subprocess and perl_available():
        return PerlSubprocessRunner()
    return GoldenReplayRunner()


# --------------------------------------------------------------------------- #
# translated side
# --------------------------------------------------------------------------- #

def load_generated_module(path: Path, module_name: str):
    """Import a generated module from an arbitrary path."""
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load generated module from %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run_translated(unit: Unit, translation_path: Optional[Path] = None) -> ExecutionResult:
    path = translation_path or unit.translation_path
    started = time.perf_counter()

    if not path.exists():
        return ExecutionResult("translated", "in-process", None, 0.0,
                               "no translation at %s" % path)
    try:
        module = load_generated_module(path, "generated_%s" % unit.name)
        output = module.run(unit.input_paths())
    except Exception as exc:                                  # noqa: BLE001
        return ExecutionResult("translated", "in-process", None,
                               (time.perf_counter() - started) * 1000,
                               "%s: %s" % (type(exc).__name__, exc))

    duration_ms = (time.perf_counter() - started) * 1000
    frame = parse_csv_text(output) if isinstance(output, str) else normalise(output)
    return ExecutionResult("translated", "in-process", frame, duration_ms)


@dataclass
class DualRun:
    unit: Unit
    legacy: ExecutionResult
    translated: ExecutionResult

    @property
    def both_ran(self) -> bool:
        return self.legacy.ok and self.translated.ok


def execute(unit: Unit,
            translation_path: Optional[Path] = None,
            prefer_subprocess: bool = True) -> DualRun:
    """Run both sides of `unit` over the same sample inputs."""
    runner = legacy_runner(prefer_subprocess, unit)
    legacy = runner.run(unit)
    translated = run_translated(unit, translation_path)

    logger.info("[%-24s] legacy=%-22s %6.1fms  translated %6.1fms",
                unit.name, runner.name, legacy.duration_ms, translated.duration_ms)
    if not legacy.ok:
        logger.error("[%s] legacy side failed: %s", unit.name, legacy.error)
    if not translated.ok:
        logger.error("[%s] translated side failed: %s", unit.name, translated.error)

    return DualRun(unit, legacy, translated)
