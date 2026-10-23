"""
Tests for sentinel.sentinel_extract.extract_sentinel_data_by_aoi.

`job_run`, `upsert` and `record_zone_error` are mocked (same pattern as
tests/packages/geo/test_generate_h3_grid.py) rather than hit a real Postgres:
the DB plumbing is covered by tests/packages/clients/. Image search and tile
reading are mocked too (covered by tests/packages/clients/planetary_computer_client
and tests/packages/sentinel/test_tile_processing.py). What's tested here is the
orchestration: which cells get a row, which get a zone error, and when the run
is skipped or rejected.
"""

import datetime
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import h3
import pandas as pd
import pytest
from shapely.geometry import Point, box

import sentinel.sentinel_extract as sentinel_extract
from clients import JobAlreadySucceeded, PlanetaryComputerError
from sentinel import EXTRACTION_PIPELINE_VERSION, previous_month_bucket

# Deux tuiles : WEST possède cellA et cellB, EAST possède cellC. cellD n'est
# couverte par aucune tuile ce mois-là.
CELLS = ["cellA", "cellB", "cellC", "cellD"]
OWNERS = {"cellA": "WEST", "cellB": "WEST", "cellC": "EAST"}


def _scene(tile):
    return SimpleNamespace(
        tile=tile, acquired_at=datetime.datetime(2025, 5, 3, tzinfo=datetime.UTC)
    )


def _aggregates(cells):
    return pd.DataFrame(
        {
            "ndvi": 0.7,
            "ndwi": -0.6,
            "ndbi": -0.3,
            "swir": 0.2,
            "masked_ratio": 0.2,
            "mean_n_obs": 4.0,
            "n_valid_px": 1700,
            "n_total_px": 1800,
        },
        index=pd.Index(cells, name="zone_id"),
    )


@pytest.fixture
def calls(monkeypatch):
    """Remplace DB, recherche et lecture ; renvoie ce qui a été appelé."""
    recorded = SimpleNamespace(
        job_run=[], upsert=[], zone_errors=[], search=[], opened=[], failing_tiles=set()
    )
    run_id = uuid.uuid4()

    @contextmanager
    def fake_job_run(**kwargs):
        recorded.job_run.append(kwargs)
        yield SimpleNamespace(run_id=run_id), MagicMock(name="session")

    def fake_search(bbox, start, end):
        recorded.search.append((start, end))
        return [_scene("WEST"), _scene("EAST")]

    def fake_open_tile(scenes, region):
        tile = scenes[0].tile
        recorded.opened.append(tile)
        if tile in recorded.failing_tiles:
            raise PlanetaryComputerError(f"cannot read {tile}")
        return SimpleNamespace(tile=tile, baselines=("05.11",))

    def fake_process_tile(stack, owned, read):
        return _aggregates(owned)

    monkeypatch.setattr(sentinel_extract, "job_run", fake_job_run)
    monkeypatch.setattr(
        sentinel_extract,
        "upsert",
        lambda session, model, rows: recorded.upsert.append((model, list(rows))),
    )
    monkeypatch.setattr(
        sentinel_extract,
        "record_zone_error",
        lambda run_id, zone_id, message: recorded.zone_errors.append((zone_id, message)),
    )
    monkeypatch.setattr(sentinel_extract, "compute_h3_cells", lambda aoi, res: CELLS)
    monkeypatch.setattr(
        sentinel_extract, "load_aoi", lambda aoi: SimpleNamespace(bbox=(2.0, 48.0, 3.0, 49.0))
    )
    monkeypatch.setattr(sentinel_extract, "search_scenes", fake_search)
    monkeypatch.setattr(sentinel_extract, "deduplicate_scenes", lambda scenes: scenes)
    monkeypatch.setattr(sentinel_extract, "tile_footprints", lambda groups: {})
    monkeypatch.setattr(sentinel_extract, "assign_owner_tiles", lambda cells, fp: OWNERS)
    monkeypatch.setattr(sentinel_extract, "_region_of", lambda cells: box(0, 0, 1, 1))
    monkeypatch.setattr(sentinel_extract, "open_tile", fake_open_tile)
    monkeypatch.setattr(sentinel_extract, "process_tile", fake_process_tile)
    return recorded


def _run(bucket="2025-05"):
    sentinel_extract.extract_sentinel_data_by_aoi("idf", bucket, resolution=8)


def _rows_by_zone(calls):
    ((_, rows),) = calls.upsert
    return {row["zone_id"]: row for row in rows}


def test_every_cell_gets_a_row(calls):
    _run()
    rows = _rows_by_zone(calls)
    assert set(rows) == set(CELLS)
    assert rows["cellA"]["ndvi"] == pytest.approx(0.7)
    assert rows["cellA"]["bucket_id"] == "2025-05"
    assert rows["cellA"]["source_version"] == EXTRACTION_PIPELINE_VERSION == "2.0.0"
    assert calls.zone_errors == []


def test_cell_outside_every_tile_is_an_explicit_gap_not_an_error(calls):
    _run()
    gap = _rows_by_zone(calls)["cellD"]
    assert gap["ndvi"] is None
    assert gap["valid_pixel_ratio"] == 0.0
    assert gap["is_valid_data"] is False
    assert calls.zone_errors == []


def test_searches_the_whole_month(calls):
    _run("2024-02")
    assert calls.search == [(datetime.date(2024, 2, 1), datetime.date(2024, 2, 29))]


def test_failed_tile_records_zone_errors_and_others_continue(calls):
    calls.failing_tiles.add("WEST")
    _run()
    assert sorted(zone for zone, _ in calls.zone_errors) == ["cellA", "cellB"]
    assert all("WEST" in message for _, message in calls.zone_errors)
    rows = _rows_by_zone(calls)
    # Les cellules en erreur n'ont pas de ligne (reprise ciblée), les autres oui.
    assert set(rows) == {"cellC", "cellD"}
    assert rows["cellC"]["ndvi"] == pytest.approx(0.7)


def test_search_failure_fails_the_whole_run(calls, monkeypatch):
    def broken_search(bbox, start, end):
        raise PlanetaryComputerError("catalog down")

    monkeypatch.setattr(sentinel_extract, "search_scenes", broken_search)
    with pytest.raises(PlanetaryComputerError):
        _run()
    assert calls.upsert == []


def test_month_without_any_scene_writes_only_gaps(calls, monkeypatch):
    monkeypatch.setattr(sentinel_extract, "search_scenes", lambda bbox, start, end: [])
    monkeypatch.setattr(sentinel_extract, "assign_owner_tiles", lambda cells, fp: {})
    _run()
    rows = _rows_by_zone(calls)
    assert set(rows) == set(CELLS)
    assert all(row["ndvi"] is None for row in rows.values())
    assert calls.opened == []


def test_idempotency_key_depends_on_month(calls):
    _run("2025-05")
    _run("2025-06")
    keys = [call["idempotency_key"] for call in calls.job_run]
    assert keys[0] != keys[1]
    assert [call["bucket_id"] for call in calls.job_run] == ["2025-05", "2025-06"]


@pytest.mark.parametrize("bucket", ["2025", "2025-13", "2025-5", "2025-b03", "mai"])
def test_rejects_non_monthly_bucket(calls, bucket):
    with pytest.raises(ValueError, match="mensuel"):
        _run(bucket)
    assert calls.job_run == []


def test_job_already_succeeded_is_a_noop(calls, monkeypatch):
    @contextmanager
    def already_done(**kwargs):
        raise JobAlreadySucceeded("key", uuid.uuid4())
        yield  # pragma: no cover

    monkeypatch.setattr(sentinel_extract, "job_run", already_done)
    _run()
    assert calls.upsert == [] and calls.search == []


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (datetime.date(2026, 11, 5), "2026-10"),
        (datetime.date(2026, 1, 1), "2025-12"),
        (datetime.date(2024, 3, 31), "2024-02"),
    ],
)
def test_previous_month_bucket(today, expected):
    assert previous_month_bucket(today) == expected


def test_previous_month_bucket_defaults_to_today():
    assert len(previous_month_bucket()) == len("YYYY-MM")


def test_region_of_covers_the_cells():
    cells = [h3.latlng_to_cell(48.85, 2.35, 8), h3.latlng_to_cell(48.90, 2.45, 8)]
    region = sentinel_extract._region_of(cells)
    for cell in cells:
        lat, lng = h3.cell_to_latlng(cell)
        assert region.contains(Point(lng, lat))
