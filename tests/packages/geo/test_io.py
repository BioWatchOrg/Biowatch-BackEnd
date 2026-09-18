import pytest
from geo.io import GeometryIOError, from_geojson, from_wkt, to_geojson, to_wkt
from shapely.geometry import Point, Polygon

SQUARE = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
POINT = Point(2.35, 48.85)


def test_to_wkt_from_wkt_round_trip():
    assert from_wkt(to_wkt(SQUARE)).equals(SQUARE)


def test_to_geojson_from_geojson_round_trip():
    geojson = to_geojson(SQUARE)
    assert geojson["type"] == "Polygon"
    assert from_geojson(geojson).equals(SQUARE)


def test_to_geojson_point():
    geojson = to_geojson(POINT)
    assert geojson["type"] == "Point"
    assert from_geojson(geojson).equals(POINT)


def test_from_wkt_invalid_raises():
    with pytest.raises(GeometryIOError):
        from_wkt("NOT A VALID WKT")


def test_from_geojson_invalid_raises():
    with pytest.raises(GeometryIOError):
        from_geojson({"type": "NotAType", "coordinates": []})


def test_to_wkt_invalid_input_raises():
    with pytest.raises(GeometryIOError):
        to_wkt("not a geometry")  # type: ignore[arg-type]


def test_to_geojson_invalid_input_raises():
    with pytest.raises(GeometryIOError):
        to_geojson("not a geometry")  # type: ignore[arg-type]


def test_to_wkt_is_trimmed_not_padded_with_zeros():
    # Untrimmed shapely.wkt.dumps defaults pad every coordinate to full float precision
    # (e.g. "1.0000000000000000") — trim=True drops that without losing round-trip exactness.
    assert "0000000" not in to_wkt(SQUARE)


def test_from_wkt_error_message_is_truncated_for_a_huge_invalid_wkt():
    huge_invalid_wkt = "NOT A VALID WKT " * 100_000  # ~1.7 MB
    with pytest.raises(GeometryIOError) as exc_info:
        from_wkt(huge_invalid_wkt)
    assert len(str(exc_info.value)) < 1_000
