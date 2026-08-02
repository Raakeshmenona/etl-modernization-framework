"""Perl 5 semantics that Python does not share.

These are the behaviours a translation gets wrong. Each one is a documented
divergence between the two languages, not a stylistic preference:

    perl_split      trailing empty fields are discarded unless LIMIT is negative
    perl_num        a non-numeric scalar numifies to 0 instead of raising
    perl_sort       the default sort is a string comparison, not numeric
    perl_int        int() truncates toward zero; Python's round() does not
    perl_true       "", "0" and 0 are false; "0.0" and "00" are true

The module has two jobs. It backs the reference implementations used to
capture golden output when a Perl interpreter is not available (see
`dual_executor.GoldenReplayRunner`), and it is importable by generated code
so a translation can express legacy semantics explicitly rather than by
accident.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional, Sequence

# Leading-numeric prefix, as Perl's string-to-number conversion sees it.
_NUMERIC_PREFIX = re.compile(r"""
    ^\s*                    # Perl skips leading whitespace
    ([+-]?                  # optional sign
     (?:\d+\.?\d*|\.\d+)    # 12 | 12. | 12.5 | .5
     (?:[eE][+-]?\d+)?      # optional exponent
    )
""", re.VERBOSE)


def perl_split(pattern: str, value: Optional[str], limit: int = 0) -> List[str]:
    """Perl's `split /pattern/, $value, LIMIT`.

    The difference that matters: with LIMIT omitted or zero, Perl discards
    trailing empty fields. ``"a|b||".split("|")`` gives four fields in Python
    and two in Perl.

    >>> perl_split(r"\\|", "a|b||")
    ['a', 'b']
    >>> perl_split(r"\\|", "a|b||", -1)
    ['a', 'b', '', '']
    """
    if value is None:
        return []

    if limit > 0:
        fields = re.split(pattern, value, maxsplit=limit - 1)
        return fields

    fields = re.split(pattern, value)

    if limit == 0:
        while fields and fields[-1] == "":
            fields.pop()

    return fields


def perl_num(value: Any) -> float:
    """Perl's numeric context.

    undef is 0, a leading numeric prefix wins, and anything else is 0. Perl
    warns but never raises, which is why bad feed rows have been silently
    scoring as zero in production for years.

    >>> perl_num(None), perl_num(""), perl_num("12abc"), perl_num("abc")
    (0.0, 0.0, 12.0, 0.0)
    """
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)

    match = _NUMERIC_PREFIX.match(str(value))
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:                      # pragma: no cover - regex guarantees this
        return 0.0


def perl_int(value: Any) -> int:
    """Perl's `int()`: truncate toward zero, never round.

    >>> perl_int(3.9), perl_int(-3.9)
    (3, -3)
    """
    return int(perl_num(value))


def perl_true(value: Any) -> bool:
    """Perl truthiness. Note that "0.0" and "00" are TRUE.

    >>> [perl_true(v) for v in (None, "", "0", "0.0", "00", 0, 0.0, "a")]
    [False, False, False, True, True, False, False, True]
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value not in ("", "0")
    return bool(value)


def perl_sort(values: Iterable[Any]) -> List[str]:
    """Perl's default `sort`: a string comparison.

    Numeric-looking keys come out 1, 10, 100, 2, 20 - which is what every
    downstream reconciliation report has been keyed on since the job was
    written.

    >>> perl_sort([1, 2, 10, 20, 100])
    ['1', '10', '100', '2', '20']
    """
    return sorted((str(v) for v in values))


def perl_sprintf_f(value: Any, places: int = 2) -> str:
    """Perl's `sprintf("%.Nf", ...)`.

    Both languages defer to the platform's double-to-decimal conversion here,
    so this is a genuine 1:1 mapping - unlike Python's ``round()``, which
    rounds half to even on the decimal value and does diverge.
    """
    return "%.*f" % (places, perl_num(value))


def epoch_days(iso_date: str) -> int:
    """Days since 1970-01-01 for a YYYY-MM-DD string.

    Mirrors the integer-only civil-date arithmetic the legacy scripts inline
    rather than depending on Date::Calc.
    """
    y_s, m_s, d_s = perl_split(r"-", iso_date, -1)[:3]
    y, m, d = int(y_s), int(m_s), int(d_s)
    y -= 1 if m <= 2 else 0
    era = y // 400 if y >= 0 else -((-y + 399) // 400)
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def days_between(from_iso: str, to_iso: str) -> int:
    """Signed day count between two YYYY-MM-DD strings."""
    return epoch_days(to_iso) - epoch_days(from_iso)


def read_delimited(path: str, delimiter: str = ",") -> Sequence[List[str]]:
    """Read a delimited file the way the legacy scripts do.

    Line-at-a-time, no quote handling, trailing empties preserved (LIMIT -1).
    Returns rows *including* the header so callers mirror the Perl control
    flow exactly.
    """
    pattern = re.escape(delimiter)
    rows: List[List[str]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            rows.append(perl_split(pattern, line, -1))
    return rows
