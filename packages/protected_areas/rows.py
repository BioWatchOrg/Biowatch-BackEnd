"""Couvertures et lignes fermées -> lignes de `protected_areas_by_zone`."""

import datetime
from collections.abc import Mapping, Sequence

from geo import H3Cell
from protected_areas.coverage import ZoneCoverage
from protected_areas.history import PreviousRow
from protected_areas.versions import ProtectedAreaType


def to_area_rows(
    current: Mapping[H3Cell, ZoneCoverage],
    closed: Sequence[PreviousRow],
    *,
    area_type: ProtectedAreaType,
    source_version: str,
    computed_at: datetime.datetime,
) -> list[dict[str, object]]:
    """Lignes prêtes pour `upsert` : une ligne ouverte par zone couverte, puis les lignes fermées.

    `closed` vient de `carry_over_closed_rows`, qui garantit qu'aucune ligne
    fermée ne partage la clé primaire d'une ligne ouverte.
    """
    common = {
        "protected_area_type": area_type,
        "source_version": source_version,
        "computed_at": computed_at,
    }
    rows: list[dict[str, object]] = [
        {
            "zone_id": zone_id,
            "valid_from": zone.valid_from,
            "valid_to": None,
            "coverage_ratio": zone.coverage_ratio,
            **common,
        }
        for zone_id, zone in current.items()
    ]
    rows.extend(
        {
            "zone_id": row.zone_id,
            "valid_from": row.valid_from,
            "valid_to": row.valid_to,
            "coverage_ratio": row.coverage_ratio,
            **common,
        }
        for row in closed
    )
    return rows
