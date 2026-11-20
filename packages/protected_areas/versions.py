"""Types d'aires protégées et `source_version` des lignes écrites."""

import datetime
import re
from typing import Literal, get_args

ProtectedAreaType = Literal["natura2000", "reserve", "znieff_type1"]

# Valeurs de `protected_areas_by_zone.protected_area_type`, dans l'ordre de traitement.
PROTECTED_AREA_TYPES: tuple[ProtectedAreaType, ...] = get_args(ProtectedAreaType)

# Version du calcul (règles de date, fusion, couverture) — PAS une date. À
# incrémenter à la main quand l'une de ces règles change : les lignes sont alors
# recalculées sous une nouvelle `source_version`, sans écraser les anciennes.
PIPELINE_VERSION = 1

_SOURCE_VERSION_RE = re.compile(r"^inpn_[a-z0-9_]+_(\d{4}-\d{2}-\d{2})_v\d+$")


def build_source_version(area_type: ProtectedAreaType, edition: datetime.date) -> str:
    """`inpn_<type>_<date de l'archive>_v<version du calcul>`.

    L'édition est la date `Last-Modified` de l'archive INPN : c'est le seul
    repère commun aux trois sources, et une archive inchangée redonne la même
    version, donc la même clé d'idempotence.
    """
    return f"inpn_{area_type}_{edition.isoformat()}_v{PIPELINE_VERSION}"


def edition_of(source_version: str) -> datetime.date | None:
    """Date de l'archive portée par une `source_version`, ou `None` si elle n'en a pas la forme."""
    match = _SOURCE_VERSION_RE.match(source_version)
    if match is None:
        return None
    try:
        return datetime.date.fromisoformat(match.group(1))
    except ValueError:
        return None
