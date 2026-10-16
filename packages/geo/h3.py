import logging
from typing import Literal, TypeAlias

import h3
from shapely.geometry import MultiPolygon, Point, Polygon, box

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


def polygon_to_cells(
    geom: Polygon | MultiPolygon,
    resolution: int,
    contain: Literal["center", "full", "overlap", "bbox_overlap"] = "overlap",
) -> list[H3Cell]:
    """
    H3 coverage of an arbitrary polygon: the cells overlapping it (default containment rule).

    Generic counterpart of h3_grid.compute_h3_cells, which resolves a geometry from the
    AOI registry first and freezes the 'center' containment rule for the grid — this
    function takes any Polygon/MultiPolygon directly, e.g. an OSM footprint or a
    protected-area shape that needs to be attached to zone_ids. Defaults to 'overlap'
    (not 'center'): a geometry smaller than a cell can contain no cell center at all,
    which would make it silently attach to zero zones under the 'center' rule — see
    docs/shared-functions.md. `contain` can be overridden per call; it's passed to h3's
    experimental multi-mode API (h3shape_to_cells_experimental), which has no
    cross-version API stability guarantee from h3, unlike h3shape_to_cells.

    Only Polygon/MultiPolygon are accepted: h3.geo_to_h3shape rejects other geometry
    types (e.g. a LineString, as OSM roads are) with a ValueError.
    """
    try:
        h3_shape = h3.geo_to_h3shape(geom.__geo_interface__)  # type: ignore
        return h3.h3shape_to_cells_experimental(h3_shape, resolution, contain=contain)  # type: ignore
    except Exception as e:
        logger.error(
            "error computing H3 coverage of geometry",
            extra={
                "event": "geo_h3.polygon_to_cells_error",
                "context": {"resolution": resolution, "contain": contain, "error": str(e)},
            },
        )
        raise H3ConversionError(
            f"Error computing H3 coverage at resolution {resolution}: {e}"
        ) from e
