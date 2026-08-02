"""Capture legacy output snapshots into data/golden/.

Run this once, on a machine that can reach the system of record. Everything
downstream - local runs, CI, the equivalence gate - diffs against the captured
snapshots rather than re-executing the legacy estate, which is the only thing
that works when the build agent has no Perl and no Oracle.

Provenance is recorded per snapshot in data/golden/_manifest.json:

    perl-subprocess            captured from the real interpreter, authoritative
    perl-semantics-reference   captured from legacy_corpus/reference/, which
                               mirrors the Perl statement-for-statement using
                               equivalence.perl_semantics

A snapshot's provenance travels with it into the Snowflake audit log, so a
reviewer can always tell which evidence a sign-off rested on.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import GOLDEN_DIR, load_manifest                      # noqa: E402
from equivalence.dual_executor import (                            # noqa: E402
    PerlSubprocessRunner, ReferenceRunner, perl_available,
)

logger = logging.getLogger(__name__)


def capture(prefer_subprocess: bool = True, only: str = None) -> dict:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    use_perl = prefer_subprocess and perl_available()
    if not use_perl:
        logger.warning("perl not found on PATH - capturing from the "
                       "perl-semantics reference implementations")

    provenance = {}
    for unit in load_manifest():
        if only and unit.name != only:
            continue

        # SQL units always go through the reference implementation: the legacy
        # Oracle/Teradata instances are not reachable from a laptop either.
        runner = (PerlSubprocessRunner()
                  if use_perl and unit.language == "perl"
                  else ReferenceRunner())
        result = runner.run(unit)

        if not result.ok:
            logger.error("[%s] capture FAILED via %s: %s",
                         unit.name, runner.name, result.error)
            provenance[unit.name] = {"status": "failed",
                                     "runner": runner.name,
                                     "error": result.error}
            continue

        # to_csv() uses os.linesep, so on Windows it emits CRLF. Snapshots are
        # committed and byte-compared against Perl's stdout, so normalise to LF
        # and keep the sha256 stable across platforms.
        csv_text = result.frame.to_csv(index=False).replace("\r\n", "\n")
        with open(str(unit.golden_path), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(csv_text)

        provenance[unit.name] = {
            "status": "captured",
            "runner": runner.name,
            "authoritative": runner.name == "perl-subprocess",
            "rows": int(len(result.frame)),
            "columns": list(result.frame.columns),
            "sha256": hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round(result.duration_ms, 2),
        }
        logger.info("[%-24s] %5d rows via %-24s -> %s",
                    unit.name, len(result.frame), runner.name, unit.golden_path.name)

    manifest_path = GOLDEN_DIR / "_manifest.json"
    with open(str(manifest_path), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(provenance, fh, indent=2, sort_keys=True)
        fh.write("\n")
    logger.info("provenance written to %s", manifest_path)
    return provenance


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", help="capture a single unit by name")
    ap.add_argument("--no-perl", action="store_true",
                    help="force the reference implementations even if perl is installed")
    args = ap.parse_args()

    provenance = capture(prefer_subprocess=not args.no_perl, only=args.only)
    failures = [n for n, p in provenance.items() if p.get("status") == "failed"]
    if failures:
        logger.error("capture failed for: %s", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
