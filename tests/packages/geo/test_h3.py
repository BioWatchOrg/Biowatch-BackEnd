import h3 as h3_lib
import pytest
from geo.h3 import (
    cell_to_bbox,
    cell_to_centroid,
    cell_to_geojson,
    cell_to_polygon,
    cell_to_wkt,
    polygon_to_cells,
)
from geo.io import from_geojson, from_wkt

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
    from geo.h3 import H3ConversionError

    with pytest.raises(H3ConversionError):
        cell_to_polygon("not-a-cell")


def test_cell_to_centroid_unknown_cell_raises():
    from geo.h3 import H3ConversionError

    with pytest.raises(H3ConversionError):
        cell_to_centroid("not-a-cell")


def test_polygon_to_cells_invalid_geometry_raises():
    from geo.h3 import H3ConversionError

    with pytest.raises(H3ConversionError):
        polygon_to_cells(None, resolution=9)  # type: ignore[arg-type]
