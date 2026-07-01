import json

import pytest
from core.aoi import (
    AOI,
    GeoJsonValueError,
    UndefinedAOIError,
    _load_polygon,
    aoi_data,
    aoi_registry,
    load_aoi,
)
from shapely.geometry import MultiPolygon, Polygon

REGISTRY_LABELS = [aoi["label"] for aoi in aoi_data["aois"]]

FR_LON = (-5.5, 9.8)
FR_LAT = (41.0, 51.5)


def test_aoi_registry_known_label_returns_true():
    assert aoi_registry("idf") is True


def test_aoi_registry_unknown_label_returns_false():
    assert aoi_registry("zzz") is False


def test_aoi_registry_is_case_sensitive():
    assert aoi_registry("IDF") is False


def test_load_aoi_returns_aoi_instance():
    assert isinstance(load_aoi("idf"), AOI)


def test_load_aoi_sets_label_and_name():
    aoi = load_aoi("idf")
    assert aoi.label == "idf"
    assert aoi.name == "Île-de-France"


def test_load_aoi_geometry_is_polygonal_valid_and_non_empty():
    geom = load_aoi("idf").geom
    assert isinstance(geom, (Polygon, MultiPolygon))
    assert geom.is_valid
    assert not geom.is_empty


def test_load_aoi_bbox_matches_geometry_bounds():
    aoi = load_aoi("idf")
    assert aoi.bbox == aoi.geom.bounds
    assert len(aoi.bbox) == 4


def test_load_aoi_bbox_is_within_metropolitan_france():
    minx, miny, maxx, maxy = load_aoi("idf").bbox
    assert minx < maxx and miny < maxy
    assert FR_LON[0] <= minx <= maxx <= FR_LON[1]
    assert FR_LAT[0] <= miny <= maxy <= FR_LAT[1]


def test_load_aoi_is_deterministic():
    a, b = load_aoi("idf"), load_aoi("idf")
    assert a.bbox == b.bbox
    assert a.geom.equals(b.geom)


def test_load_aoi_unknown_raises_undefined():
    with pytest.raises(UndefinedAOIError):
        load_aoi("zzz")


@pytest.mark.parametrize("label", REGISTRY_LABELS)
def test_every_registered_aoi_loads_a_valid_geometry(label):
    geom = load_aoi(label).geom
    assert isinstance(geom, (Polygon, MultiPolygon))
    assert geom.is_valid


@pytest.mark.parametrize("label", REGISTRY_LABELS)
def test_registry_ids_follow_lowercase_convention(label):
    assert label.islower()
    assert label.isalpha()
    assert len(label) == 3


# --------------------------------------------------------------------------- #
# _load_polygon — testé directement pour couvrir la branche d'erreur
# (géométrie non polygonale), inatteignable via load_aoi + registry réel.
# --------------------------------------------------------------------------- #
def _write_feature(tmp_path, geometry: dict) -> str:
    path = tmp_path / "aoi.geojson"
    path.write_text(json.dumps({"type": "Feature", "geometry": geometry, "properties": {}}))
    return str(path)


def test_load_polygon_returns_polygon(tmp_path):
    geom = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}
    result = _load_polygon(_write_feature(tmp_path, geom))
    assert isinstance(result, Polygon)


def test_load_polygon_returns_multipolygon(tmp_path):
    geom = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
            [[[2, 2], [2, 3], [3, 3], [3, 2], [2, 2]]],
        ],
    }
    result = _load_polygon(_write_feature(tmp_path, geom))
    assert isinstance(result, MultiPolygon)


def test_load_polygon_rejects_non_polygonal_geometry(tmp_path):
    geom = {"type": "Point", "coordinates": [2.35, 48.85]}
    with pytest.raises(GeoJsonValueError):
        _load_polygon(_write_feature(tmp_path, geom))
