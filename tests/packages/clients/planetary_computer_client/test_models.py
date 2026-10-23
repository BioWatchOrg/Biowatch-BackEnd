"""Tests for clients.planetary_computer_client.models.Scene."""

import datetime

import pytest

from clients.planetary_computer_client import PlanetaryComputerError, Scene
from planetary_computer_items import load_stac_items


@pytest.fixture
def stac_items():
    return load_stac_items()


def test_scene_from_item_reads_metadata(stac_items):
    scene = Scene.from_item(stac_items[2])
    assert scene.scene_id == "S2A_MSIL2A_20220123T110341_R094_T31UDQ_20220124T032157"
    assert scene.tile == "31UDQ"
    assert scene.acquired_at.date() == datetime.date(2022, 1, 23)
    assert scene.acquired_at.tzinfo is not None
    assert scene.baseline == "03.00"
    assert scene.epsg == 32631
    assert 0 <= scene.cloud_cover <= 100
    assert scene.footprint.is_valid and scene.footprint.contains(scene.footprint.centroid)
    assert scene.item is stac_items[2]


def test_scene_from_item_accepts_legacy_proj_epsg(stac_items):
    item = stac_items[0]
    item.properties.pop("proj:code", None)
    item.properties["proj:epsg"] = 32631
    assert Scene.from_item(item).epsg == 32631


@pytest.mark.parametrize("missing", ["s2:mgrs_tile", "s2:processing_baseline", "eo:cloud_cover"])
def test_scene_from_item_missing_metadata_raises(stac_items, missing):
    item = stac_items[0]
    del item.properties[missing]
    with pytest.raises(PlanetaryComputerError, match=item.id):
        Scene.from_item(item)


def test_scene_equality_ignores_item(stac_items):
    a = Scene.from_item(stac_items[0])
    b = Scene.from_item(stac_items[0].clone())
    assert a == b


def test_scene_from_item_without_geometry_raises(stac_items):
    item = stac_items[0]
    item.geometry = None
    with pytest.raises(PlanetaryComputerError, match="no geometry"):
        Scene.from_item(item)


def test_scene_from_item_without_datetime_raises(stac_items):
    item = stac_items[0]
    item.datetime = None
    with pytest.raises(PlanetaryComputerError, match="no acquisition datetime"):
        Scene.from_item(item)
