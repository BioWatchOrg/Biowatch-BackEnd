import logging
from typing import TypeAlias

import shapely.wkt
from shapely.errors import ShapelyError
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

GeoJSON: TypeAlias = dict[str, object]


class GeometryIOError(Exception):
    """Exception raised when a geometry fails to serialize or parse."""


def to_wkt(geom: BaseGeometry) -> str:
    """Shapely geometry -> WKT text (e.g. for PostGIS/GeoAlchemy2)."""
    try:
        # trim=True drops the untrimmed default's redundant trailing zeros (~4x the bytes
        # for the same geometry) without losing precision (round-trip stays exact).
        return shapely.wkt.dumps(geom, trim=True)
    except (ShapelyError, TypeError) as e:
        logger.error(
            "error serializing geometry to WKT",
            extra={"event": "geo_io.to_wkt_error", "context": {"error": str(e)}},
        )
        raise GeometryIOError(f"Error serializing geometry to WKT: {e}") from e


def from_wkt(wkt: str) -> BaseGeometry:
    """WKT text -> Shapely geometry."""
    try:
        return shapely.wkt.loads(wkt)
    except ShapelyError as e:
        # Full WKT omitted from both the log context and the message: a bad MultiPolygon
        # can be several MB, enough to saturate structured-log shipping on one line.
        logger.error(
            "error parsing WKT",
            extra={
                "event": "geo_io.from_wkt_error",
                "context": {"wkt_prefix": wkt[:200], "wkt_len": len(wkt), "error": str(e)},
            },
        )
        raise GeometryIOError(
            f"Error parsing WKT (prefix: {wkt[:200]!r}, len={len(wkt)}): {e}"
        ) from e


def to_geojson(geom: BaseGeometry) -> GeoJSON:
    """Shapely geometry -> GeoJSON dict (e.g. for the API/frontend)."""
    try:
        return dict(mapping(geom))
    except (ShapelyError, AttributeError) as e:
        logger.error(
            "error serializing geometry to GeoJSON",
            extra={"event": "geo_io.to_geojson_error", "context": {"error": str(e)}},
        )
        raise GeometryIOError(f"Error serializing geometry to GeoJSON: {e}") from e


def from_geojson(geojson: GeoJSON) -> BaseGeometry:
    """GeoJSON dict -> Shapely geometry."""
    try:
        return shape(geojson)
    except (ShapelyError, AttributeError, KeyError, ValueError) as e:
        logger.error(
            "error parsing GeoJSON",
            extra={"event": "geo_io.from_geojson_error", "context": {"error": str(e)}},
        )
        raise GeometryIOError(f"Error parsing GeoJSON: {e}") from e
