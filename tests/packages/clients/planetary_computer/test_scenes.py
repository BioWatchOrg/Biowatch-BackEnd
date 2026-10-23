"""Tests for clients.planetary_computer.scenes (pure scene preparation)."""

import dataclasses
import datetime

import pytest
from shapely.geometry import box

from clients.planetary_computer import Scene, deduplicate_scenes, group_by_tile, tile_footprints
from planetary_computer_items import load_stac_items


@pytest.fixture
def stac_items():
    return load_stac_items()


UTC = datetime.timezone.utc


def _scene(scene_id, tile="31UDQ", day=1, baseline="05.11", footprint=None):
    return Scene(
        scene_id=scene_id,
        tile=tile,
        acquired_at=datetime.datetime(2025, 5, day, 10, 50, tzinfo=UTC),
        baseline=baseline,
        epsg=32631,
        cloud_cover=10.0,
        footprint=footprint or box(2.0, 48.0, 3.0, 49.0),
        item=None,  # type: ignore[arg-type]
    )


def test_real_duplicate_keeps_most_recent_baseline(stac_items):
    scenes = [Scene.from_item(item) for item in stac_items]
    kept = deduplicate_scenes(scenes)
    assert len(kept) == 2
    dec_11 = next(s for s in kept if s.acquired_at.date() == datetime.date(2023, 12, 11))
    assert dec_11.baseline == "05.10"


def test_baselines_compared_numerically():
    kept = deduplicate_scenes([_scene("a", baseline="10.00"), _scene("b", baseline="04.00")])
    assert [s.scene_id for s in kept] == ["a"]


def test_same_baseline_tie_is_stable():
    first = deduplicate_scenes([_scene("x_2024"), _scene("x_2023")])
    second = deduplicate_scenes([_scene("x_2023"), _scene("x_2024")])
    assert [s.scene_id for s in first] == [s.scene_id for s in second] == ["x_2024"]


def test_distinct_acquisitions_are_all_kept():
    scenes = [_scene("a", day=1), _scene("b", day=2), _scene("c", tile="31UEQ", day=1)]
    assert {s.scene_id for s in deduplicate_scenes(scenes)} == {"a", "b", "c"}


def test_group_by_tile_sorted():
    scenes = [_scene("late", day=20), _scene("other", tile="31UCQ"), _scene("early", day=3)]
    groups = group_by_tile(scenes)
    assert list(groups) == ["31UCQ", "31UDQ"]
    assert [s.scene_id for s in groups["31UDQ"]] == ["early", "late"]


def test_group_by_tile_empty():
    assert group_by_tile([]) == {}


def test_tile_footprints_union_of_scenes():
    west, east = box(2.0, 48.0, 2.6, 49.0), box(2.4, 48.0, 3.0, 49.0)
    groups = {"31UDQ": [_scene("a", footprint=west), _scene("b", day=2, footprint=east)]}
    footprints = tile_footprints(groups)
    assert footprints["31UDQ"].equals(box(2.0, 48.0, 3.0, 49.0))


def test_scene_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _scene("a").tile = "other"  # type: ignore[misc]
