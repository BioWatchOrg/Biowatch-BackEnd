"""
Tests for sentinel.sentinel_extract.extract_sentinel_data_by_aoi.

`job_run`, `upsert` and `record_zone_error` are mocked (same pattern as
tests/packages/geo/test_generate_h3_grid.py) rather than hit a real
Postgres: `upsert` generates Postgres-specific ON CONFLICT syntax that a
lightweight SQLite engine can't run, and the DB plumbing itself is already
covered by tests/packages/clients/. What's new and untested elsewhere is this
module's own orchestration: per-zone error isolation, the auth-error fast
abort, and context propagation into the worker threads.
"""

import time
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock

import httpx
import pytest

import sentinel.sentinel_extract as sentinel_extract
from clients import (
    JobAlreadySucceeded,
    SentinelHubAuthError,
    SentinelHubRequestError,
    ZoneStatistics,
    run_id_var,
)


class _FakeRunContext:
    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id


def _zone_stat(valid_pixel_ratio: float = 0.9) -> ZoneStatistics:
    return ZoneStatistics(
        ndvi_mean=0.4,
        ndwi_mean=-0.2,
        ndbi_mean=0.1,
        swir_mean=1500.0,
        obs_count=1000,
        valid_pixel_ratio=valid_pixel_ratio,
        cloud_score=1.0 - valid_pixel_ratio,
    )


@pytest.fixture
def fake_job_run(monkeypatch):
    """Replace job_run with a bare context manager yielding (run, session)."""

    @contextmanager
    def _fake(**kwargs):
        yield _FakeRunContext(run_id=uuid.uuid4()), MagicMock(name="session")

    monkeypatch.setattr(sentinel_extract, "job_run", _fake)


@pytest.fixture
def fake_upsert(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sentinel_extract, "upsert", lambda session, model, rows: calls.append((model, list(rows)))
    )
    return calls


@pytest.fixture
def fake_record_zone_error(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sentinel_extract,
        "record_zone_error",
        lambda run_id, zone_id, message: calls.append((run_id, zone_id, message)),
    )
    return calls


@pytest.fixture
def fake_cells(monkeypatch):
    """Three fake H3 cells instead of a real AOI's thousands."""
    monkeypatch.setattr(
        sentinel_extract, "compute_h3_cells", lambda aoi_label, resolution: ["cellA", "cellB", "cellC"]
    )
    monkeypatch.setattr(sentinel_extract, "cell_to_geojson", lambda cell: {"cell": cell})


def test_happy_path_upserts_every_cell(fake_job_run, fake_upsert, fake_record_zone_error, fake_cells, monkeypatch):
    monkeypatch.setattr(
        sentinel_extract, "fetch_zone_statistics", lambda geometry, time_range, client: _zone_stat()
    )

    sentinel_extract.extract_sentinel_data_by_aoi("idf", "2026", 8, "1.0.0")

    assert len(fake_upsert) == 1
    model, rows = fake_upsert[0]
    assert model is sentinel_extract.SatelliteFeaturesByZone
    assert {row["zone_id"] for row in rows} == {"cellA", "cellB", "cellC"}
    assert all(row["bucket_id"] == "2026" for row in rows)
    assert all(row["source_version"] == "1.0.0" for row in rows)
    assert fake_record_zone_error == []


def test_time_range_is_may_of_the_bucket_year_with_exclusive_end(
    fake_job_run, fake_upsert, fake_record_zone_error, fake_cells, monkeypatch
):
    """
    Cadence annuelle (ADR-003) : la requête interroge toujours mai de l'année
    du bucket, pas l'année calendaire entière. La borne de fin est exclusive
    (1er juin, pas 31 mai 23:59:59) : aggregationInterval="P1M" côté Sentinel
    Hub a besoin qu'un mois calendaire complet rentre dans [from, to] pour
    produire un intervalle — même piège que le bug corrigé le 2026-10-04,
    mois différent.
    """
    seen_time_ranges = []

    def _fetch(geometry, time_range, client):
        seen_time_ranges.append(time_range)
        return _zone_stat()

    monkeypatch.setattr(sentinel_extract, "fetch_zone_statistics", _fetch)

    sentinel_extract.extract_sentinel_data_by_aoi("idf", "2026", 8, "1.0.0")

    assert seen_time_ranges == [("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z")] * 3


def test_rejects_non_yearly_bucket_id(fake_cells, monkeypatch):
    called = {"fetch": False}
    monkeypatch.setattr(
        sentinel_extract, "fetch_zone_statistics", lambda *a, **k: called.update(fetch=True)
    )

    with pytest.raises(ValueError, match="annuel"):
        sentinel_extract.extract_sentinel_data_by_aoi("idf", "2026-02", 8, "1.0.0")

    assert called["fetch"] is False  # fails before any network call


def test_job_already_succeeded_is_a_noop(fake_cells, monkeypatch):
    @contextmanager
    def _raise_already_succeeded(**kwargs):
        raise JobAlreadySucceeded("key", uuid.uuid4())
        yield  # pragma: no cover - unreachable

    monkeypatch.setattr(sentinel_extract, "job_run", _raise_already_succeeded)
    called = {"fetch": False}

    def _fail_if_called(*args, **kwargs):
        called["fetch"] = True

    monkeypatch.setattr(sentinel_extract, "fetch_zone_statistics", _fail_if_called)

    sentinel_extract.extract_sentinel_data_by_aoi("idf", "2026", 8, "1.0.0")  # must not raise

    assert called["fetch"] is False


def test_per_zone_request_error_is_recorded_and_does_not_block_others(
    fake_job_run, fake_upsert, fake_record_zone_error, fake_cells, monkeypatch
):
    def _fetch(geometry, time_range, client):
        if geometry["cell"] == "cellB":
            raise SentinelHubRequestError("boom")
        return _zone_stat()

    monkeypatch.setattr(sentinel_extract, "fetch_zone_statistics", _fetch)

    sentinel_extract.extract_sentinel_data_by_aoi("idf", "2026", 8, "1.0.0", max_workers=1)

    assert [call[1] for call in fake_record_zone_error] == ["cellB"]
    _, rows = fake_upsert[0]
    assert {row["zone_id"] for row in rows} == {"cellA", "cellC"}


def test_network_error_is_also_recorded_as_a_zone_error(
    fake_job_run, fake_upsert, fake_record_zone_error, fake_cells, monkeypatch
):
    def _fetch(geometry, time_range, client):
        if geometry["cell"] == "cellA":
            raise httpx.ConnectError("boom")
        return _zone_stat()

    monkeypatch.setattr(sentinel_extract, "fetch_zone_statistics", _fetch)

    sentinel_extract.extract_sentinel_data_by_aoi("idf", "2026", 8, "1.0.0", max_workers=1)

    assert [call[1] for call in fake_record_zone_error] == ["cellA"]


def test_auth_error_aborts_the_whole_run_instead_of_recording_per_zone(
    fake_job_run, fake_upsert, fake_record_zone_error, fake_cells, monkeypatch
):
    def _fetch(geometry, time_range, client):
        raise SentinelHubAuthError("bad credentials")

    monkeypatch.setattr(sentinel_extract, "fetch_zone_statistics", _fetch)

    with pytest.raises(SentinelHubAuthError):
        sentinel_extract.extract_sentinel_data_by_aoi("idf", "2026", 8, "1.0.0", max_workers=1)

    assert fake_record_zone_error == []  # not a per-zone error
    assert fake_upsert == []  # never reached the upsert call


def test_run_id_propagates_into_worker_threads(fake_upsert, fake_record_zone_error, fake_cells, monkeypatch):
    """A plain ThreadPoolExecutor does not inherit contextvars — this checks
    the explicit contextvars.copy_context() propagation actually works.

    The sleep is deliberate, not padding: it forces the 3 workers to overlap
    in time. Without it, each call could start and finish before the next one
    begins, which would hide a regression where a single Context object gets
    entered concurrently from more than one thread (contextvars.Context.run
    raises RuntimeError in that case — see the real crash this test guards
    against, hit when running extract_sentinel against a real AOI).
    """
    expected_run_id = uuid.uuid4()
    seen_run_ids = []

    @contextmanager
    def _fake_job_run_setting_context(**kwargs):
        token = run_id_var.set(str(expected_run_id))
        try:
            yield _FakeRunContext(run_id=expected_run_id), MagicMock(name="session")
        finally:
            run_id_var.reset(token)

    monkeypatch.setattr(sentinel_extract, "job_run", _fake_job_run_setting_context)

    def _fetch(geometry, time_range, client):
        time.sleep(0.05)
        seen_run_ids.append(run_id_var.get())
        return _zone_stat()

    monkeypatch.setattr(sentinel_extract, "fetch_zone_statistics", _fetch)

    sentinel_extract.extract_sentinel_data_by_aoi("idf", "2026", 8, "1.0.0", max_workers=3)

    assert seen_run_ids == [str(expected_run_id)] * 3


def test_fetch_cell_row_shape_and_is_valid_data_flag(monkeypatch):
    monkeypatch.setattr(sentinel_extract, "cell_to_geojson", lambda cell: {"cell": cell})
    monkeypatch.setattr(
        sentinel_extract, "fetch_zone_statistics", lambda geometry, time_range, client: _zone_stat(0.05)
    )

    row = sentinel_extract._fetch_cell_row(
        cell="cellX",
        time_range=("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z"),
        bucket_id="2026",
        source_version="1.0.0",
        client=None,
    )

    assert row["zone_id"] == "cellX"
    assert row["bucket_id"] == "2026"
    assert row["source_version"] == "1.0.0"
    assert row["is_valid_data"] is False  # 0.05 is below the 0.1 threshold
    assert "computed_at" in row


def test_fetch_cell_row_valid_pixel_ratio_threshold_is_inclusive(monkeypatch):
    """Décision produit : valid_pixel_ratio >= 10% (pas de rejet strict pile à 10%)."""
    monkeypatch.setattr(sentinel_extract, "cell_to_geojson", lambda cell: {"cell": cell})
    monkeypatch.setattr(
        sentinel_extract,
        "fetch_zone_statistics",
        lambda geometry, time_range, client: _zone_stat(
            sentinel_extract.VALID_PIXEL_RATIO_THRESHOLD
        ),
    )

    row = sentinel_extract._fetch_cell_row(
        cell="cellX",
        time_range=("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z"),
        bucket_id="2026",
        source_version="1.0.0",
        client=None,
    )

    assert row["is_valid_data"] is True
