"""Extract ETL logic units and translation risks from Perl source.

There is no usable Perl AST from Python - only Perl can parse Perl, famously -
so this is a regex-and-heuristics extractor. That is fine for the job it has:
it does not need to *understand* the script, it needs to find the constructs
that an LLM translates incorrectly, and hand them to the prompt as explicit
warnings.

Every risk flag here corresponds to a real defect class observed in
Perl -> Python migrations:

    string_sort         `sort` without a comparator is a STRING sort
    split_no_limit      trailing empty fields are silently discarded
    undef_arithmetic    undef numifies to 0 rather than raising
    ambiguous_date      DD/MM vs MM/DD cannot be inferred from the code
    numeric_string_cmp  == on strings is 0 == 0, i.e. always true
    int_truncation      int() truncates; round() does not
    implicit_global     a variable used across subs without `my`
    inplace_edit        s/// on $_ inside a loop mutates the alias

The extractor is deliberately noisy. A false positive costs a sentence in the
prompt; a false negative costs a production incident.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List

logger = logging.getLogger(__name__)


@dataclass
class RiskFlag:
    kind: str
    line: int
    snippet: str
    explanation: str

    def as_prompt_line(self) -> str:
        return "- line %d [%s] %s\n    %s" % (self.line, self.kind,
                                              self.snippet.strip(), self.explanation)


@dataclass
class PerlUnit:
    """Structural summary of one Perl script."""

    name: str
    line_count: int = 0
    subs: List[str] = field(default_factory=list)
    file_reads: List[str] = field(default_factory=list)
    file_writes: List[str] = field(default_factory=list)
    printf_formats: List[str] = field(default_factory=list)
    hash_accumulators: List[str] = field(default_factory=list)
    constants: Dict[str, str] = field(default_factory=dict)
    external_commands: List[str] = field(default_factory=list)
    risks: List[RiskFlag] = field(default_factory=list)

    def risk_kinds(self) -> List[str]:
        return sorted({r.kind for r in self.risks})

    def prompt_block(self) -> str:
        """The risk section injected into the translation prompt."""
        if not self.risks:
            return "None detected."
        return "\n".join(r.as_prompt_line() for r in self.risks)


_STRIP_COMMENT = re.compile(r"(?<!\$)#.*$")

_PATTERNS = [
    ("string_sort",
     re.compile(r"\bsort\s+(?:keys|values|@)\w*"),
     "Perl's default sort is a STRING comparison. sorted() on numbers in "
     "Python is numeric and will reorder the output. Sort the values as "
     "strings, or confirm with the owner that numeric order is acceptable."),

    ("split_no_limit",
     re.compile(r"\bsplit\s*\((?![^)]*,\s*-?\d)[^)]*\)"),
     "split without a LIMIT argument discards trailing empty fields. "
     "str.split() keeps them, so field counts and any denominator derived "
     "from them will differ. Use perl_semantics.perl_split."),

    ("undef_arithmetic",
     re.compile(r"no\s+warnings\s+['\"]uninitialized['\"]|\$\w+\{\$?\w+\}\s*[*+/-]"),
     "An undefined hash lookup numifies to 0 in arithmetic context rather "
     "than raising or producing NaN. pandas will give NaN and poison every "
     "downstream aggregate. Use perl_semantics.perl_num."),

    ("ambiguous_date",
     re.compile(r"\$\w*(?:date|dt|dd|mm)\w*\s*(?:=~|,)|split\s*\(\s*/\\?\//"),
     "Date component order (DD/MM vs MM/DD) is not recoverable from the "
     "code. Read the field order from the surrounding assignment and state "
     "the format explicitly - do not let pandas infer it."),

    ("numeric_string_cmp",
     re.compile(r"\$\w+\s*==\s*['\"]"),
     "== is numeric comparison in Perl. Comparing a string with == numifies "
     "both sides to 0, so the test is always true. The intent was almost "
     "certainly `eq`."),

    ("int_truncation",
     re.compile(r"\bint\s*\("),
     "Perl's int() truncates toward zero. Python's round() rounds half to "
     "even. Translate int() to int(), never to round()."),

    ("inplace_edit",
     re.compile(r"for\s*\(\s*\$\w+\s*,|foreach\s+\(\s*\$"),
     "Aliasing loop: s/// inside `for ($a, $b)` mutates the originals. The "
     "Python equivalent must reassign, not just transform a copy."),

    ("sprintf_rounding",
     re.compile(r"(?:sprintf|printf)\s*\(\s*[\"'][^\"']*%[.\d]*f"),
     "sprintf('%.2f') defers to the platform double-to-decimal conversion, "
     "which Python's format spec matches exactly. Python's round() does NOT "
     "- it rounds half to even on the decimal value. Use string formatting."),
]


def _strip_comment(line: str) -> str:
    return _STRIP_COMMENT.sub("", line)


def extract(source: str, name: str = "<anonymous>") -> PerlUnit:
    """Summarise a Perl script and flag its translation risks."""
    unit = PerlUnit(name=name)
    lines = source.splitlines()
    unit.line_count = len(lines)

    for number, raw in enumerate(lines, start=1):
        line = _strip_comment(raw)
        if not line.strip():
            continue

        match = re.search(r"\bsub\s+(\w+)", line)
        if match:
            unit.subs.append(match.group(1))

        match = re.search(r"open\s*\(\s*my\s*\$\w+\s*,\s*'([<>+]+)'\s*,\s*([^)]+)\)", line)
        if match:
            target = match.group(2).strip()
            (unit.file_reads if "<" in match.group(1) else unit.file_writes).append(target)

        for fmt in re.findall(r"(?:sprintf|printf)\s*\(\s*\"([^\"]+)\"", line):
            unit.printf_formats.append(fmt)

        match = re.search(r"(\$\w+\{[^}]+\})\s*\+=", line)
        if match:
            unit.hash_accumulators.append(match.group(1))

        match = re.search(r"use\s+constant\s+(\w+)\s*=>\s*'?([^;',]+)'?", line)
        if match:
            unit.constants[match.group(1)] = match.group(2).strip()

        if re.search(r"(?:`|\bsystem\s*\(|\bexec\s*\()", line):
            unit.external_commands.append(line.strip())

        for kind, pattern, explanation in _PATTERNS:
            if pattern.search(line):
                unit.risks.append(RiskFlag(kind, number, raw, explanation))

    # Dedupe risks of the same kind on the same line.
    seen = set()
    deduped = []
    for risk in unit.risks:
        key = (risk.kind, risk.line)
        if key not in seen:
            seen.add(key)
            deduped.append(risk)
    unit.risks = deduped

    logger.debug("[%s] %d lines, %d subs, %d risks (%s)",
                 name, unit.line_count, len(unit.subs), len(unit.risks),
                 ", ".join(unit.risk_kinds()) or "none")
    return unit


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("script", nargs="+", help="path to a .pl file")
    args = ap.parse_args()

    for path_str in args.script:
        path = Path(path_str)
        result = extract(path.read_text(encoding="utf-8"), path.name)
        print("\n== %s (%d lines) ==" % (result.name, result.line_count))
        print("subs        : %s" % (", ".join(result.subs) or "-"))
        print("reads       : %s" % (", ".join(result.file_reads) or "-"))
        print("constants   : %s" % (result.constants or "-"))
        print("accumulators: %s" % (", ".join(result.hash_accumulators) or "-"))
        print("risks       :")
        print(result.prompt_block())
