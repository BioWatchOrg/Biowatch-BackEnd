"""Couverture de chaque zone H3 par les sites d'un type d'aire protégée."""

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import shapely
from shapely.errors import GEOSException
from shapely.geometry.base import BaseGeometry

from geo import SRID_LAMBERT93, H3Cell, IntersectionError, coverage_ratio

# Précision de `coverage_ratio`. Une couverture qui s'arrondit à 0 (moins d'un
# millionième de la zone : un site qui ne fait que longer l'hexagone) n'est pas
# une protection de la zone et ne donne pas de ligne.
COVERAGE_DECIMALS = 6


@dataclass(frozen=True)
class DatedSite:
    """Un site protégé, géométrie en Lambert-93, avec sa date de début de protection."""

    site_id: str
    geom: BaseGeometry
    valid_from: datetime.date


@dataclass(frozen=True)
class ZoneCoverage:
    """Ce qu'un type d'aire protégée représente pour une zone."""

    coverage_ratio: float
    valid_from: datetime.date


def compute_zone_coverage(
    zones: Mapping[H3Cell, BaseGeometry], sites: Sequence[DatedSite]
) -> tuple[dict[H3Cell, ZoneCoverage], dict[H3Cell, str]]:
    """Couverture par zone, et erreurs par zone.

    `zones` et `sites` sont en Lambert-93 (surfaces en mètres). Pour chaque zone,
    les sites qui la recouvrent sont fusionnés : `coverage_ratio` est la part de
    la zone couverte par cette fusion (deux sites superposés ne comptent qu'une
    fois), `valid_from` la date du plus ancien de ces sites. Une zone qu'aucun
    site ne recouvre est absente du résultat.

    Une zone dont le calcul échoue (géométrie que GEOS refuse) va dans le second
    dictionnaire avec le message d'erreur ; les autres zones sont calculées.
    """
    coverage: dict[H3Cell, ZoneCoverage] = {}
    errors: dict[H3Cell, str] = {}
    if not sites:
        return coverage, errors

    geoms = [shapely.make_valid(site.geom) for site in sites]
    tree = shapely.STRtree(geoms)
    for zone_id, hexagon in zones.items():
        try:
            overlaps: list[BaseGeometry] = []
            dates: list[datetime.date] = []
            for index in tree.query(hexagon, predicate="intersects"):
                overlap = hexagon.intersection(geoms[index])
                if overlap.area > 0:
                    overlaps.append(overlap)
                    dates.append(sites[index].valid_from)
            if not overlaps:
                continue
            ratio = coverage_ratio(hexagon, shapely.union_all(overlaps), srid=SRID_LAMBERT93)
        except (GEOSException, IntersectionError) as e:
            errors[zone_id] = str(e)
            continue
        ratio = round(min(ratio, 1.0), COVERAGE_DECIMALS)
        if ratio > 0:
            coverage[zone_id] = ZoneCoverage(coverage_ratio=ratio, valid_from=min(dates))
    return coverage, errors
