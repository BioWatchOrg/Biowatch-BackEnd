import pytest
from geo.intersections import (
    IntersectionError,
    _intersection,
    _intersects,
    coverage_ratio,
    filter_intersecting,
)
from shapely.geometry import Point, Polygon

FULL_SQUARE = Polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
HALF_OVERLAP = Polygon([(5, 0), (15, 0), (15, 10), (5, 10), (5, 0)])  # chevauche à 50%
FAR_AWAY = Polygon([(100, 100), (110, 100), (110, 110), (100, 110), (100, 100)])
INNER_SQUARE = Polygon([(2, 2), (8, 2), (8, 8), (2, 8), (2, 2)])  # entièrement dans FULL_SQUARE


def test_intersects_true_for_overlapping_geometries():
    assert _intersects(FULL_SQUARE, HALF_OVERLAP) is True


def test_intersects_false_for_disjoint_geometries():
    assert _intersects(FULL_SQUARE, FAR_AWAY) is False


def test_intersection_returns_overlap_geometry():
    overlap = _intersection(FULL_SQUARE, HALF_OVERLAP)
    assert overlap is not None
    assert overlap.area == pytest.approx(50.0)


def test_intersection_returns_none_for_disjoint_geometries():
    assert _intersection(FULL_SQUARE, FAR_AWAY) is None


def test_filter_intersecting_keeps_only_touching_geometries():
    result = filter_intersecting([HALF_OVERLAP, FAR_AWAY, INNER_SQUARE], FULL_SQUARE)
    assert result == [HALF_OVERLAP, INNER_SQUARE]


def test_coverage_ratio_full_inclusion_is_one():
    assert coverage_ratio(INNER_SQUARE, FULL_SQUARE, srid=2154) == pytest.approx(1.0)


def test_coverage_ratio_disjoint_is_zero():
    assert coverage_ratio(FULL_SQUARE, FAR_AWAY, srid=2154) == pytest.approx(0.0)


def test_coverage_ratio_partial_overlap_is_half():
    assert coverage_ratio(FULL_SQUARE, HALF_OVERLAP, srid=2154) == pytest.approx(0.5)


def test_coverage_ratio_zero_area_geom_is_zero():
    point_on_boundary = Point(5, 5)  # aire nulle, mais chevauche FULL_SQUARE
    assert coverage_ratio(point_on_boundary, FULL_SQUARE, srid=2154) == pytest.approx(0.0)


def test_intersects_invalid_input_raises():
    # Un argument non-géométrie fait lever un TypeError côté shapely, pas un ShapelyError.
    with pytest.raises(IntersectionError):
        _intersects(FULL_SQUARE, "not a geometry")  # type: ignore[arg-type]


def test_intersection_invalid_input_raises():
    with pytest.raises(IntersectionError):
        _intersection(FULL_SQUARE, "not a geometry")  # type: ignore[arg-type]


def test_intersects_none_geometry_raises_instead_of_attribute_error():
    # geoms=[None, ...] can reach here from a source feed with a null geometry.
    with pytest.raises(IntersectionError):
        _intersects(None, FULL_SQUARE)  # type: ignore[arg-type]


def test_intersects_none_mask_raises_instead_of_silently_returning_false():
    # square.intersects(None) returns False without raising (shapely 2.1.2) — would
    # otherwise make filter_intersecting silently drop everything on a None mask.
    with pytest.raises(IntersectionError):
        _intersects(FULL_SQUARE, None)  # type: ignore[arg-type]


def test_intersection_none_geometry_raises():
    with pytest.raises(IntersectionError):
        _intersection(None, FULL_SQUARE)  # type: ignore[arg-type]


def test_filter_intersecting_none_mask_raises():
    with pytest.raises(IntersectionError):
        filter_intersecting([FULL_SQUARE], None)  # type: ignore[arg-type]
