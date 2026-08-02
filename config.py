"""Project paths and the legacy-corpus manifest."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent

CORPUS_DIR = PROJECT_ROOT / "legacy_corpus"
MANIFEST_PATH = CORPUS_DIR / "manifest.yaml"
REFERENCE_PKG = "legacy_corpus.reference"

SAMPLE_DIR = PROJECT_ROOT / "data" / "sample"
GOLDEN_DIR = PROJECT_ROOT / "data" / "golden"

TRANSLATION_CACHE_DIR = PROJECT_ROOT / "translator" / "cache"
PROMPT_DIR = PROJECT_ROOT / "translator" / "prompt_templates"
GENERATED_DIR = PROJECT_ROOT / "generated"
REGRESSION_DIR = PROJECT_ROOT / "generated" / "regression"
REPORT_DIR = PROJECT_ROOT / "reports"

# The CI gate blocks promotion below this score. Raising it is cheap; lowering
# it requires a signed-off exception recorded in the Snowflake audit log.
EQUIVALENCE_THRESHOLD = 99.0

# Claude model used for translation. Pinned so a cached translation can be
# attributed to an exact model version in the audit trail.
TRANSLATION_MODEL = "claude-opus-4-8"


@dataclass
class Unit:
    """One legacy script under migration."""

    name: str
    path: str
    language: str
    owner: str = "unassigned"
    criticality: str = "medium"
    inputs: Dict[str, str] = field(default_factory=dict)
    output_columns: List[str] = field(default_factory=list)
    key_columns: List[str] = field(default_factory=list)
    tolerance: float = 0.0
    depends_on: List[str] = field(default_factory=list)
    dialect: Optional[str] = None
    target_dialect: Optional[str] = None

    @property
    def source_path(self) -> Path:
        return CORPUS_DIR / self.path

    @property
    def golden_path(self) -> Path:
        return GOLDEN_DIR / ("%s.csv" % self.name)

    @property
    def translation_path(self) -> Path:
        return TRANSLATION_CACHE_DIR / ("%s.py" % self.name)

    def input_paths(self) -> Dict[str, str]:
        """Logical dataset name -> absolute path under data/sample."""
        return {name: str(SAMPLE_DIR / filename) for name, filename in self.inputs.items()}

    def read_source(self) -> str:
        return self.source_path.read_text(encoding="utf-8")


def load_manifest(path: Path = MANIFEST_PATH) -> List[Unit]:
    """Parse manifest.yaml into Unit objects, preserving declaration order."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    units = [Unit(**entry) for entry in raw["units"]]
    logger.debug("loaded %d units from %s", len(units), path)
    return units


def unit_by_name(name: str, units: Optional[List[Unit]] = None) -> Unit:
    for unit in units if units is not None else load_manifest():
        if unit.name == name:
            return unit
    raise KeyError("no unit named %r in the manifest" % name)
