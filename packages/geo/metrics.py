import logging
from functools import lru_cache

import pyproj
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

logger = logging.getLogger(__name__)

SRID_WGS84 = 4326
SRID_LAMBERT93 = 2154


class MetricsError(Exception):
    """Exception raised when a geometric metric cannot be computed."""


@lru_cache(maxsize=None)
def _get_transformer(from_srid: int, to_srid: int) -> pyproj.Transformer:
    """Cached pyproj.Transformer for an SRID pair — building one costs ~38x a transform()
    call (pyproj 3.8.0), too expensive to redo per zone/feature in a per-cell loop."""
    return pyproj.Transformer.from_crs(f"EPSG:{from_srid}", f"EPSG:{to_srid}", always_xy=True)


def _reproject(
    geom: BaseGeometry, from_srid: int = SRID_WGS84, to_srid: int = SRID_LAMBERT93
) -> BaseGeometry:
    """Reproject a geometry between two SRIDs (e.g. WGS84 degrees -> Lambert-93 meters)."""
    try:
        transformer = _get_transformer(from_srid, to_srid)
        return transform(transformer.transform, geom)
    except Exception as e:
        logger.error(
            "error reprojecting geometry",
            extra={
                "event": "geo_metrics.reproject_error",
                "context": {"from_srid": from_srid, "to_srid": to_srid, "error": str(e)},
            },
        )
        raise MetricsError(f"Error reprojecting geometry from {from_srid} to {to_srid}: {e}") from e


def area_m2(geom: BaseGeometry, srid: int = SRID_WGS84) -> float:
    """Real-world area in m2, reprojecting to Lambert-93 if the input isn't already metric."""
    if srid == SRID_LAMBERT93:
        return geom.area
    return _reproject(geom, from_srid=srid, to_srid=SRID_LAMBERT93).area


def length_m(geom: BaseGeometry, srid: int = SRID_WGS84) -> float:
    """Real-world length in m, reprojecting to Lambert-93 if the input isn't already metric."""
    if srid == SRID_LAMBERT93:
        return geom.length
    return _reproject(geom, from_srid=srid, to_srid=SRID_LAMBERT93).length
