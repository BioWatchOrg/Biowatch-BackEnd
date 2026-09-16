import logging

from shapely.errors import ShapelyError
from shapely.geometry.base import BaseGeometry

from .metrics import SRID_WGS84, area_m2

logger = logging.getLogger(__name__)


class IntersectionError(Exception):
    """Exception raised when a geometric intersection cannot be computed."""


def _intersects(geom_a: BaseGeometry, geom_b: BaseGeometry) -> bool:
    """Whether two geometries touch or overlap."""
    try:
        return bool(geom_a.intersects(geom_b))
    except ShapelyError as e:
        logger.error(
            "error testing geometry intersection",
            extra={"event": "geo_intersections.intersects_error", "context": {"error": str(e)}},
        )
        raise IntersectionError(f"Error testing geometry intersection: {e}")


def _intersection(geom_a: BaseGeometry, geom_b: BaseGeometry) -> BaseGeometry | None:
    """The overlapping geometry between two geometries, or None if they don't overlap."""
    try:
        result = geom_a.intersection(geom_b)
        return None if result.is_empty else result
    except ShapelyError as e:
        logger.error(
            "error computing geometry intersection",
            extra={"event": "geo_intersections.intersection_error", "context": {"error": str(e)}},
        )
        raise IntersectionError(f"Error computing geometry intersection: {e}")


def filter_intersecting(
    geoms: list[BaseGeometry], mask: BaseGeometry
) -> list[BaseGeometry]:
    """Keep only the geometries that intersect `mask` (e.g. features touching a zone)."""
    return [geom for geom in geoms if _intersects(geom, mask)]


def coverage_ratio(geom: BaseGeometry, mask: BaseGeometry, srid: int = SRID_WGS84) -> float:
    """
    Fraction of `geom`'s area covered by `mask`, in [0.0, 1.0].

    Generic building block for a coverage ratio like protected_areas_by_zone's
    (a zone hexagon vs. a protected-area polygon) — no business logic attached here.
    """
    overlap = _intersection(geom, mask)
    if overlap is None:
        return 0.0
    geom_area = area_m2(geom, srid=srid)
    if geom_area == 0:
        return 0.0
    return area_m2(overlap, srid=srid) / geom_area
