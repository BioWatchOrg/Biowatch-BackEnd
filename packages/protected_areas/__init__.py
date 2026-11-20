"""Aires protégées par zone H3 : fonctions pures, sans I/O (ADR-009).

Datation des sites, couverture par zone et report des fermetures d'une édition
à la suivante. L'orchestration (téléchargement INPN, écriture en base, job_run)
vit dans apps/jobs/pipelines/protected_areas.py, conformément au CLAUDE.md.
"""

from .coverage import DatedSite, ZoneCoverage, compute_zone_coverage
from .dates import ZNIEFF_DEFAULT_VALID_FROM, site_valid_from
from .history import (
    DEFAULT_MAX_CLOSED_SHARE,
    PreviousRow,
    TooManyClosuresError,
    carry_over_closed_rows,
)
from .rows import to_area_rows
from .versions import (
    PIPELINE_VERSION,
    PROTECTED_AREA_TYPES,
    ProtectedAreaType,
    build_source_version,
    edition_of,
)

__all__ = [
    "PROTECTED_AREA_TYPES",
    "ProtectedAreaType",
    "PIPELINE_VERSION",
    "build_source_version",
    "edition_of",
    "ZNIEFF_DEFAULT_VALID_FROM",
    "site_valid_from",
    "DatedSite",
    "ZoneCoverage",
    "compute_zone_coverage",
    "DEFAULT_MAX_CLOSED_SHARE",
    "PreviousRow",
    "TooManyClosuresError",
    "carry_over_closed_rows",
    "to_area_rows",
]
