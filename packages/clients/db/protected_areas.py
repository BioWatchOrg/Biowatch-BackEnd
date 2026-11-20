"""Lectures de `zones_hex` et `protected_areas_by_zone` pour le job des aires protégées."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import ProtectedAreasByZone, ZonesHex


def zone_ids_for_aoi(session: Session, aoi_label: str, resolution: int) -> list[str]:
    """Zones H3 d'une AOI à une résolution, telles que présentes dans `zones_hex` (triées)."""
    return list(
        session.scalars(
            select(ZonesHex.zone_id)
            .where(ZonesHex.aoi_id == aoi_label, ZonesHex.resolution == resolution)
            .order_by(ZonesHex.zone_id)
        ).all()
    )


def previous_protected_area_rows(
    session: Session,
    area_type: str,
    aoi_label: str,
    resolution: int,
    current_source_version: str,
) -> tuple[str | None, list[ProtectedAreasByZone]]:
    """Dernière édition écrite avant `current_source_version` pour ce type et cette AOI.

    Renvoie `(source_version, lignes)` de la `source_version` calculée le plus
    récemment (`computed_at`) parmi les autres que la courante, ou `(None, [])`
    au premier import. Les lignes sont limitées aux zones de l'AOI à la
    résolution donnée (la table n'a pas de colonne AOI : jointure sur `zones_hex`).
    """
    in_scope = (
        (ProtectedAreasByZone.protected_area_type == area_type)
        & (ProtectedAreasByZone.source_version != current_source_version)
        & (ZonesHex.aoi_id == aoi_label)
        & (ZonesHex.resolution == resolution)
    )
    previous_version = session.scalar(
        select(ProtectedAreasByZone.source_version)
        .join(ZonesHex, ZonesHex.zone_id == ProtectedAreasByZone.zone_id)
        .where(in_scope)
        .group_by(ProtectedAreasByZone.source_version)
        .order_by(
            func.max(ProtectedAreasByZone.computed_at).desc(),
            ProtectedAreasByZone.source_version.desc(),
        )
        .limit(1)
    )
    if previous_version is None:
        return None, []
    rows = session.scalars(
        select(ProtectedAreasByZone)
        .join(ZonesHex, ZonesHex.zone_id == ProtectedAreasByZone.zone_id)
        .where(in_scope, ProtectedAreasByZone.source_version == previous_version)
        .order_by(ProtectedAreasByZone.zone_id, ProtectedAreasByZone.valid_from)
    ).all()
    return previous_version, list(rows)
