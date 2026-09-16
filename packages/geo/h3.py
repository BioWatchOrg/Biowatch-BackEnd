import logging
from typing import TypeAlias

import h3
from shapely.geometry import Point, Polygon, box
from shapely.geometry.base import BaseGeometry

from .io import GeoJSON, to_geojson, to_wkt

logger = logging.getLogger(__name__)

H3Cell: TypeAlias = str  # H3 cell id (hex string)


class H3ConversionError(Exception):
    """Exception raised when a conversion between H3 and geometry fails."""


def cell_to_polygon(cell: H3Cell) -> Polygon:
    """H3 cell -> shapely polygon (the hexagon's boundary)."""
    try:
        boundary = h3.cell_to_boundary(cell)
        # h3 returns (lat, lng) ; shapely wants (x=lng, y=lat).
        return Polygon([(lng, lat) for lat, lng in boundary])
    except Exception as e:
        logger.error(
            "error converting H3 cell to polygon",
            extra={
                "event": "geo_h3.cell_to_polygon_error",
                "context": {"cell": cell, "error": str(e)},
            },
        )
        raise H3ConversionError(f"Error converting H3 cell '{cell}' to polygon: {e}") from e


def cell_to_centroid(cell: H3Cell) -> Point:
    """H3 cell -> shapely point (the cell's center)."""
    try:
        lat, lng = h3.cell_to_latlng(cell)
        return Point(lng, lat)
    except Exception as e:
        logger.error(
            "error converting H3 cell to centroid",
            extra={
                "event": "geo_h3.cell_to_centroid_error",
                "context": {"cell": cell, "error": str(e)},
            },
        )
        raise H3ConversionError(f"Error converting H3 cell '{cell}' to centroid: {e}") from e


def cell_to_bbox(cell: H3Cell) -> Polygon:
    """H3 cell -> shapely polygon (the smallest axis-aligned rectangle containing it)."""
    return box(*cell_to_polygon(cell).bounds)


def cell_to_wkt(cell: H3Cell) -> str:
    """H3 cell -> WKT text of its polygon."""
    return to_wkt(cell_to_polygon(cell))


def cell_to_geojson(cell: H3Cell) -> GeoJSON:
    """H3 cell -> GeoJSON dict of its polygon."""
    return to_geojson(cell_to_polygon(cell))


def polygon_to_cells(geom: BaseGeometry, resolution: int) -> list[H3Cell]:
    """
    H3 coverage of an arbitrary geometry: the cells whose center falls inside it.

    Generic counterpart of h3_grid.compute_h3_cells, which resolves a geometry from
    the AOI registry first — this function takes any shapely geometry directly, e.g.
    an OSM polygon or a protected-area shape that needs to be attached to zone_ids.
    """
    try:
        h3_shape = h3.geo_to_h3shape(geom.__geo_interface__)  # type: ignore
        return h3.h3shape_to_cells(h3_shape, resolution)  # type: ignore
    except Exception as e:
        logger.error(
            "error computing H3 coverage of geometry",
            extra={
                "event": "geo_h3.polygon_to_cells_error",
                "context": {"resolution": resolution, "error": str(e)},
            },
        )
        raise H3ConversionError(
            f"Error computing H3 coverage at resolution {resolution}: {e}"
        ) from e
