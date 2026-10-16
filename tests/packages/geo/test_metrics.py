import pytest
from geo.metrics import (
    SRID_LAMBERT93,
    MetricsError,
    _get_transformer,
    _reproject,
    area_m2,
    length_m,
)
from shapely.geometry import LineString, Polygon

# Petit carré en Île-de-France (WGS84, degrés) : ~0.01° de côté autour de Paris.
SQUARE_WGS84 = Polygon([(2.35, 48.85), (2.36, 48.85), (2.36, 48.86), (2.35, 48.86), (2.35, 48.85)])
# Segment est-ouest de 0.01° de longitude à la même latitude.
LINE_WGS84 = LineString([(2.35, 48.85), (2.36, 48.85)])


def test_reproject_moves_coordinates_into_lambert93_extent():
    reprojected = _reproject(SQUARE_WGS84, from_srid=4326, to_srid=SRID_LAMBERT93)
    x_coords = [x for x, _ in reprojected.exterior.coords]
    y_coords = [y for _, y in reprojected.exterior.coords]
    # Emprise officielle Lambert-93 pour la France métropolitaine.
    assert all(100_000 < x < 1_300_000 for x in x_coords)
    assert all(6_000_000 < y < 7_200_000 for y in y_coords)


def test_area_m2_matches_expected_order_of_magnitude():
    # ~1113 m (lat) x ~734 m (lon, à 48.85°N) -> ~817 000 m², tolérance large.
    area = area_m2(SQUARE_WGS84, srid=4326)
    assert 700_000 < area < 900_000


def test_area_m2_with_lambert93_input_skips_reprojection():
    square_l93 = Polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
    assert area_m2(square_l93, srid=SRID_LAMBERT93) == pytest.approx(100.0)


def test_length_m_matches_expected_order_of_magnitude():
    # ~734 m attendus (0.01° de longitude à 48.85°N), tolérance large.
    length = length_m(LINE_WGS84, srid=4326)
    assert 650 < length < 800


def test_length_m_with_lambert93_input_skips_reprojection():
    line_l93 = LineString([(0, 0), (3, 4)])
    assert length_m(line_l93, srid=SRID_LAMBERT93) == pytest.approx(5.0)


def test_reproject_invalid_srid_raises():
    with pytest.raises(MetricsError):
        _reproject(SQUARE_WGS84, from_srid=4326, to_srid=99_999_999)


def test_get_transformer_is_cached_per_srid_pair():
    assert _get_transformer(4326, SRID_LAMBERT93) is _get_transformer(4326, SRID_LAMBERT93)
