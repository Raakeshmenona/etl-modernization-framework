"""Reference for perl/null_handling_report.pl."""
from __future__ import annotations

from typing import Dict

from equivalence.perl_semantics import perl_split


def run(inputs: Dict[str, str]) -> str:
    out = ["record_id,field_count,populated_fields,completeness_pct"]

    with open(inputs["raw_feed"], "r", encoding="utf-8") as fh:
        fh.readline()                                  # my $header = <$fh>;
        for line in fh:
            line = line.rstrip("\n").rstrip("\r")
            if not line:
                continue

            # LIMIT omitted: trailing empty fields are discarded. This is the
            # behaviour the completeness denominator depends on.
            f = perl_split(r"\|", line)

            field_count = len(f)
            if not field_count:
                continue

            populated = sum(1 for v in f if v is not None and len(v))
            out.append("%s,%d,%d,%.1f"
                       % (f[0], field_count, populated, 100 * populated / field_count))

    return "\n".join(out) + "\n"
