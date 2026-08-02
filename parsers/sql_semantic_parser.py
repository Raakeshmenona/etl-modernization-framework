"""Parse, normalise and transpile legacy SQL dialects.

Uses sqlglot when it is installed; falls back to a regex feature-detector when
it is not, so the pipeline still produces risk flags for the prompt on a bare
build agent. The fallback does not transpile - it says so rather than emitting
SQL it cannot vouch for.

The output that matters is not the transpiled string. It is the list of
dialect features that changed meaning, because those are what the translation
prompt has to be warned about and what a reviewer has to sign off.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

try:                                                # pragma: no cover
    import sqlglot
    from sqlglot import exp
    SQLGLOT_AVAILABLE = True
except ImportError:                                 # pragma: no cover
    sqlglot = None
    exp = None
    SQLGLOT_AVAILABLE = False


# Dialect constructs whose meaning is not preserved by a naive lift-and-shift.
DIALECT_FEATURES = [
    ("oracle_outer_join", re.compile(r"\(\+\)"),
     "Oracle (+) outer join. The marker sits on the OPTIONAL side, so the "
     "other table is preserved. Getting the direction backwards silently "
     "drops rows and is the most common defect in an Oracle migration."),

    ("nvl", re.compile(r"\bNVL\s*\(", re.I),
     "NVL(x, y) -> COALESCE(x, y). NVL always evaluates both arguments; "
     "COALESCE short-circuits. Only matters if the second argument has side "
     "effects or can error."),

    ("decode", re.compile(r"\bDECODE\s*\(", re.I),
     "DECODE(expr, k1, v1, ..., default) -> CASE. DECODE treats NULL as "
     "matchable; CASE WHEN x = NULL never matches. If any key is NULL the "
     "CASE needs an explicit IS NULL arm."),

    ("rownum", re.compile(r"\bROWNUM\b", re.I),
     "ROWNUM is assigned BEFORE ORDER BY. `WHERE ROWNUM <= 10 ORDER BY x` "
     "returns an arbitrary 10 rows then sorts them - it is not top-10. "
     "A LIMIT after ORDER BY changes the result set."),

    ("to_date", re.compile(r"\bTO_DATE\s*\(", re.I),
     "TO_DATE with an explicit format model. Carry the format through; do "
     "not let the target infer it."),

    ("teradata_sel", re.compile(r"^\s*SEL\s+", re.I | re.M),
     "Teradata SEL abbreviation -> SELECT."),

    ("qualify", re.compile(r"\bQUALIFY\b", re.I),
     "QUALIFY filters after the window function. Snowflake supports it; "
     "Spark SQL and Postgres do not and need a windowed sub-select."),

    ("rank_vs_row_number", re.compile(r"\bRANK\s*\(\s*\)", re.I),
     "RANK() lets ties share an ordinal and skips the ones that follow. "
     "Substituting ROW_NUMBER() changes the ROW COUNT whenever there is a "
     "tie, not just the rank values."),

    ("implicit_cross_join", re.compile(r"\bFROM\b[^;]*?,\s*\w+\s+\w+", re.I | re.S),
     "Comma-separated FROM list (implicit join). Convert to explicit ANSI "
     "JOIN and verify each ON clause against the WHERE predicates."),

    ("sysdate", re.compile(r"\bSYSDATE\b|\bCURRENT_DATE\b", re.I),
     "Non-deterministic date function. An equivalence harness cannot compare "
     "output that depends on wall-clock time - parameterise it."),
]


@dataclass
class SqlUnit:
    name: str
    dialect: str
    target_dialect: str
    statements: int = 0
    tables: List[str] = field(default_factory=list)
    output_columns: List[str] = field(default_factory=list)
    features: List[Dict[str, str]] = field(default_factory=list)
    transpiled: Optional[str] = None
    normalised: Optional[str] = None
    parse_error: Optional[str] = None
    parser: str = "regex-fallback"

    def feature_kinds(self) -> List[str]:
        return sorted({f["kind"] for f in self.features})

    def prompt_block(self) -> str:
        if not self.features:
            return "None detected."
        return "\n".join("- [%s] %s\n    %s" % (f["kind"], f["match"], f["explanation"])
                         for f in self.features)


def _detect_features(sql: str) -> List[Dict[str, str]]:
    found = []
    for kind, pattern, explanation in DIALECT_FEATURES:
        match = pattern.search(sql)
        if match:
            found.append({
                "kind": kind,
                "match": match.group(0).strip()[:60],
                "explanation": explanation,
            })
    return found


def parse(sql: str,
          name: str = "<anonymous>",
          dialect: str = "oracle",
          target_dialect: str = "snowflake") -> SqlUnit:
    """Analyse one legacy SQL script."""
    unit = SqlUnit(name=name, dialect=dialect, target_dialect=target_dialect)
    unit.features = _detect_features(sql)

    if not SQLGLOT_AVAILABLE:
        logger.info("[%s] sqlglot not installed - feature detection only, no transpile",
                    name)
        unit.tables = _regex_tables(sql)
        unit.output_columns = _regex_output_columns(sql)
        unit.statements = len([s for s in sql.split(";") if s.strip()])
        return unit

    unit.parser = "sqlglot"
    try:                                            # pragma: no cover
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
        unit.statements = len(trees)
        tables, columns = set(), []
        for tree in trees:
            for table in tree.find_all(exp.Table):
                tables.add(table.name)
            select = tree.find(exp.Select)
            if select is not None:
                columns = [(e.alias_or_name or "") for e in select.expressions]
        unit.tables = sorted(t for t in tables if t)
        unit.output_columns = [c for c in columns if c]
        unit.normalised = "\n".join(t.sql(dialect=dialect, pretty=True) for t in trees)
        unit.transpiled = "\n".join(sqlglot.transpile(sql, read=dialect,
                                                      write=target_dialect, pretty=True))
    except Exception as exc:                        # noqa: BLE001  # pragma: no cover
        unit.parse_error = "%s: %s" % (type(exc).__name__, exc)
        logger.warning("[%s] sqlglot could not parse: %s", name, unit.parse_error)
        unit.tables = _regex_tables(sql)
        unit.output_columns = _regex_output_columns(sql)

    return unit


def _regex_tables(sql: str) -> List[str]:
    names = set()
    for match in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w$.]*)", sql, re.I):
        names.add(match.group(1).split(".")[-1].lower())
    # Comma-separated FROM lists: `FROM accounts a, customers c`
    from_clause = re.search(r"\bFROM\b(.*?)(?:\bWHERE\b|\bGROUP\b|\bORDER\b|;|$)",
                            sql, re.I | re.S)
    if from_clause:
        for chunk in from_clause.group(1).split(","):
            token = chunk.strip().split()
            if token:
                names.add(token[0].split(".")[-1].lower())
    return sorted(n for n in names if n and not n.isdigit())


def _regex_output_columns(sql: str) -> List[str]:
    """Pull `AS alias` names in order - good enough to check the contract."""
    return [m.group(1).lower()
            for m in re.finditer(r"\bAS\s+([A-Za-z_]\w*)", sql, re.I)]


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("script", nargs="+")
    ap.add_argument("--dialect", default="oracle")
    ap.add_argument("--target", default="snowflake")
    args = ap.parse_args()

    for path_str in args.script:
        path = Path(path_str)
        result = parse(path.read_text(encoding="utf-8"), path.name,
                       args.dialect, args.target)
        print("\n== %s (%s -> %s, parser=%s) ==" %
              (result.name, result.dialect, result.target_dialect, result.parser))
        print("tables  : %s" % ", ".join(result.tables))
        print("columns : %s" % ", ".join(result.output_columns))
        print("features:")
        print(result.prompt_block())
        if result.transpiled:
            print("\n-- transpiled to %s --\n%s" % (result.target_dialect, result.transpiled))
