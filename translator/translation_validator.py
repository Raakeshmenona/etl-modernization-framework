"""Static gate on generated code, before anything executes it.

Every check here is cheap and runs before the translation is allowed near the
sample data. The point is to catch the failure modes that would otherwise cost
a full dual-execution cycle, and to refuse to import code that should not be
imported at all.

Checks, in order of severity:

    syntax        the module parses
    signature     `run` exists and takes the expected parameter
    imports       only allow-listed modules; no os/subprocess/socket/eval
    contract      the module references every declared output column
    heuristics    known translation smells (see SMELLS) - warnings, not errors

The import allow-list is a security control, not a style preference. Generated
code is model output; it is executed in-process by the harness; and a prompt
injected through a legacy source comment is a real path to arbitrary code
execution in CI.
"""
from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

ALLOWED_IMPORTS = {
    "pandas", "numpy", "datetime", "decimal", "math", "re", "collections",
    "itertools", "functools", "typing", "dataclasses", "json", "csv", "io",
    "__future__", "equivalence", "equivalence.perl_semantics",
    "pyspark", "pyspark.sql", "pyspark.sql.functions", "pyspark.sql.types",
}

FORBIDDEN_IMPORTS = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "requests",
    "urllib", "urllib.request", "http", "ftplib", "pickle", "importlib",
    "ctypes", "multiprocessing", "tempfile",
}

FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open", "input"}

SMELLS = [
    ("round_for_money",
     re.compile(r"\bround\s*\([^)]*,\s*[123]\s*\)"),
     "round(x, n) rounds half to even on the decimal value; Perl's sprintf "
     "does not. Use string formatting to reproduce the legacy output."),

    ("naive_split",
     re.compile(r"\.str\.split\(|(?<!perl_)\bsplit\s*\(\s*[\"']"),
     "Bare split keeps trailing empty fields; Perl's split drops them unless "
     "a negative LIMIT was passed. Confirm against the source or use "
     "perl_semantics.perl_split."),

    ("inferred_dates",
     re.compile(r"to_datetime\((?![^)]*format\s*=)"),
     "pd.to_datetime without an explicit format= infers per value and will "
     "read DD/MM as MM/DD wherever both are <= 12."),

    ("ambiguous_us_date",
     re.compile(r"format\s*=\s*[\"']%m/%d/%Y[\"']"),
     "US date format on a feed whose legacy parser read DD/MM. Verify the "
     "field order in the source before accepting this."),

    ("type_inference",
     re.compile(r"read_csv\((?![^)]*dtype)"),
     "read_csv without dtype= lets pandas infer. Leading zeros are lost and "
     "integer keys become floats, which then fail to join."),

    ("silent_dropna",
     re.compile(r"\.dropna\(\)|\.fillna\(0\)"),
     "Dropping or zero-filling nulls silently changes row counts and totals. "
     "Reproduce what the legacy job did, and say so in a comment."),
]


@dataclass
class ValidationResult:
    unit: str
    ok: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.ok = False

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "%s: clean" % self.unit
        parts = ["%s: %s" % (self.unit, "ok" if self.ok else "REJECTED")]
        parts.extend("  error   %s" % e for e in self.errors)
        parts.extend("  warning %s" % w for w in self.warnings)
        return "\n".join(parts)


def _module_root(name: str) -> str:
    return name.split(".")[0]


def validate(code: str,
             unit_name: str = "<generated>",
             output_columns: Optional[Sequence[str]] = None,
             expected_arg: str = "inputs") -> ValidationResult:
    """Static-check one generated module."""
    result = ValidationResult(unit=unit_name)

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        result.add_error("syntax error at line %s: %s" % (exc.lineno, exc.msg))
        return result

    run_function = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([alias.name for alias in node.names]
                     if isinstance(node, ast.Import) else [node.module or ""])
            for name in names:
                if not name:
                    continue
                result.imports.append(name)
                root = _module_root(name)
                if root in FORBIDDEN_IMPORTS:
                    result.add_error("forbidden import %r - generated code runs "
                                     "in-process in CI" % name)
                elif name not in ALLOWED_IMPORTS and root not in ALLOWED_IMPORTS:
                    result.add_error("import %r is not on the allow-list" % name)

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS and node.func.id != "open":
                result.add_error("forbidden call to %s()" % node.func.id)

        if isinstance(node, ast.FunctionDef) and node.name == "run":
            run_function = node

    if run_function is None:
        result.add_error("no top-level `run` function")
    else:
        arg_names = [a.arg for a in run_function.args.args]
        if expected_arg not in arg_names:
            result.add_error("run() must accept %r; got (%s)"
                             % (expected_arg, ", ".join(arg_names) or "no args"))

    for column in output_columns or []:
        if column not in code:
            result.add_error("declared output column %r never appears in the "
                             "generated module" % column)

    for kind, pattern, explanation in SMELLS:
        match = pattern.search(code)
        if match:
            result.warnings.append("[%s] %s -- %s"
                                   % (kind, match.group(0).strip()[:50], explanation))

    logger.debug("validated %s: ok=%s errors=%d warnings=%d",
                 unit_name, result.ok, len(result.errors), len(result.warnings))
    return result


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import load_manifest                                # noqa: E402

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--unit", help="validate a single unit")
    args = ap.parse_args()

    exit_code = 0
    for unit in load_manifest():
        if args.unit and unit.name != args.unit:
            continue
        if not unit.translation_path.exists():
            print("%s: no translation on disk" % unit.name)
            exit_code = 1
            continue
        outcome = validate(unit.translation_path.read_text(encoding="utf-8"),
                           unit.name, unit.output_columns)
        print(outcome.summary())
        if not outcome.ok:
            exit_code = 1
    raise SystemExit(exit_code)
