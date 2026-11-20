import h3 as h3_lib
import pytest
from geo.h3 import (
    H3ConversionError,
    cell_to_bbox,
    cell_to_centroid,
    cell_to_geojson,
    cell_to_polygon,
    cell_to_wkt,
    polygon_to_cells,
)
from geo.io import from_geojson, from_wkt
from shapely.geometry import LineString, Point

CELL = h3_lib.latlng_to_cell(48.8566, 2.3522, 9)  # Paris, résolution 9


def test_cell_to_polygon_is_valid():
    polygon = cell_to_polygon(CELL)
    assert polygon.is_valid
    assert polygon.exterior.is_ring


def test_cell_to_centroid_is_inside_polygon():
    polygon = cell_to_polygon(CELL)
    centroid = cell_to_centroid(CELL)
    assert polygon.contains(centroid)


def test_cell_to_bbox_contains_polygon():
    polygon = cell_to_polygon(CELL)
    bbox = cell_to_bbox(CELL)
    assert bbox.covers(polygon)
    assert bbox.bounds == polygon.bounds


def test_cell_to_wkt_round_trips_to_same_polygon():
    polygon = cell_to_polygon(CELL)
    wkt = cell_to_wkt(CELL)
    assert from_wkt(wkt).equals(polygon)


def test_cell_to_geojson_round_trips_to_same_polygon():
    polygon = cell_to_polygon(CELL)
    geojson = cell_to_geojson(CELL)
    assert geojson["type"] == "Polygon"
    assert from_geojson(geojson).equals(polygon)


def test_polygon_to_cells_returns_valid_cells_at_requested_resolution():
    polygon = cell_to_bbox(CELL)  # zone un peu plus large que la cell elle-même
    cells = polygon_to_cells(polygon, resolution=9)
    assert cells
    assert all(h3_lib.is_valid_cell(c) for c in cells)
    assert all(h3_lib.get_resolution(c) == 9 for c in cells)


def test_polygon_to_cells_includes_the_source_cell():
    polygon = cell_to_polygon(CELL)
    cells = polygon_to_cells(polygon, resolution=9)
    assert CELL in cells


def test_cell_to_polygon_unknown_cell_raises():
    with pytest.raises(H3ConversionError):
        cell_to_polygon("not-a-cell")


def test_cell_to_centroid_unknown_cell_raises():
    with pytest.raises(H3ConversionError):
        cell_to_centroid("not-a-cell")


def test_polygon_to_cells_invalid_geometry_raises():
    with pytest.raises(H3ConversionError):
        polygon_to_cells(None, resolution=9)  # type: ignore[arg-type]


def test_polygon_to_cells_default_overlap_does_not_drop_small_geometry():
    # A tiny polygon straddling a cell boundary vertex contains no cell center at all —
    # under the 'center' rule this returns [] silently (see docs/shared-functions.md).
    boundary_vertex_lat, boundary_vertex_lng = h3_lib.cell_to_boundary(CELL)[0]
    tiny = Point(boundary_vertex_lng, boundary_vertex_lat).buffer(1e-7)
    assert polygon_to_cells(tiny, resolution=9, contain="center") == []
    cells = polygon_to_cells(tiny, resolution=9)  # default: overlap
    assert cells
    assert CELL in cells


def test_polygon_to_cells_rejects_non_polygon_geometry():
    # h3.geo_to_h3shape only recognizes Polygon/MultiPolygon (e.g. not a LineString,
    # the shape OSM roads come in as) — surfaced as H3ConversionError, not a raw ValueError.
    with pytest.raises(H3ConversionError):
        polygon_to_cells(LineString([(2.35, 48.85), (2.36, 48.85)]), resolution=9)  # type: ignore[arg-type]
