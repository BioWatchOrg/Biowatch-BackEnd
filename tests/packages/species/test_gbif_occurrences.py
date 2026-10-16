"""Tests for species.gbif_occurrences.update_gbif_occurrences (Download API)."""

import os
import uuid
from contextlib import contextmanager
from datetime import date

import httpx
import pytest
from sqlalchemy import create_engine, delete, func, select

import species.gbif_occurrences as gbif_occurrences
from clients import GbifApiError, JobAlreadySucceeded, SpeciesOccurrencesRaw

GOLDEN_AOI = "idf"


def test_update_gbif_occurrences_skips_when_already_succeeded(monkeypatch):
    called = {"body": False}

    @contextmanager
    def fake_job_run(*args, idempotency_key="", **kwargs):
        raise JobAlreadySucceeded(idempotency_key, uuid.uuid4())
        called["body"] = True  # pragma: no cover - unreachable
        yield  # pragma: no cover

    monkeypatch.setattr(gbif_occurrences, "job_run", fake_job_run)

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)
    assert called["body"] is False


def test_rolling_window_covers_ten_years():
    date_min, date_max = gbif_occurrences.rolling_window(2025)
    assert date_min == date(2016, 1, 1)
    assert date_max == date(2025, 12, 31)


def test_normalize_csv_row_maps_gbif_id_to_key():
    row = {
        "gbifID": "1001",
        "scientificName": "Vulpes vulpes",
        "taxonKey": "5219243",
        "decimalLatitude": "48.8566",
        "decimalLongitude": "2.3522",
        "eventDate": "2020-06-15",
    }
    normalized = gbif_occurrences._normalize_csv_row(row)
    assert normalized == {
        "key": "1001",
        "scientificName": "Vulpes vulpes",
        "taxonKey": 5219243,
        "decimalLatitude": 48.8566,
        "decimalLongitude": 2.3522,
        "eventDate": "2020-06-15",
    }


def test_normalize_csv_row_handles_missing_optional_fields():
    row = {
        "gbifID": "",
        "scientificName": "",
        "taxonKey": "",
        "decimalLatitude": "",
        "decimalLongitude": "",
        "eventDate": "",
    }
    normalized = gbif_occurrences._normalize_csv_row(row)
    assert normalized == {
        "key": None,
        "scientificName": None,
        "taxonKey": None,
        "decimalLatitude": None,
        "decimalLongitude": None,
        "eventDate": None,
    }


def _force_default_res(aoi, resolution):
    return aoi.__class__(**{**aoi.__dict__, "default_res": resolution})


class _FakeScalarsResult:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _FakeSession:
    def __init__(self, zone_ids):
        self._zone_ids = zone_ids
        self.executed_statements: list[object] = []

    def execute(self, stmt):
        self.executed_statements.append(stmt)

    def scalars(self, stmt):
        return _FakeScalarsResult(self._zone_ids)


def _fake_job_run_yielding(session):
    @contextmanager
    def fake_job_run(*args, idempotency_key="", **kwargs):
        yield None, session

    return fake_job_run


_GOOD_CSV_ROW = {
    "gbifID": "42",
    "scientificName": "Vulpes vulpes",
    "taxonKey": "5219243",
    "decimalLatitude": "48.8566",
    "decimalLongitude": "2.3522",
    "eventDate": "2020-06-15",
}

_FAKE_CREDS = type("_Creds", (), {"username": "alice", "auth": httpx.BasicAuth("alice", "s")})()


def _patch_download_pipeline(monkeypatch, rows, download_key="key123"):
    """Patch the request/poll/stream trio so update_gbif_occurrences's HTTP
    calls never happen -- `rows` is the list of raw CSV-row dicts `stream_download`
    should yield."""
    monkeypatch.setattr(gbif_occurrences, "load_gbif_credentials", lambda: _FAKE_CREDS)
    monkeypatch.setattr(gbif_occurrences, "request_download", lambda *a, **k: download_key)
    monkeypatch.setattr(gbif_occurrences, "wait_for_download", lambda *a, **k: None)
    monkeypatch.setattr(gbif_occurrences, "stream_download", lambda *a, **k: iter(rows))


def test_update_gbif_occurrences_skips_record_failing_quality_filter(monkeypatch):
    captured_rows: list[dict[str, object]] = []

    def fake_upsert(session, model, rows, update_columns=None):
        captured_rows.extend(rows)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    session = _FakeSession(zone_ids={"expected-zone"})
    monkeypatch.setattr(gbif_occurrences, "job_run", _fake_job_run_yielding(session))
    monkeypatch.setattr(gbif_occurrences, "upsert", fake_upsert)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: aoi)
    monkeypatch.setattr(gbif_occurrences, "point_to_h3_cell", lambda lat, lon, res: "expected-zone")

    bad_row = {"gbifID": "", "scientificName": "no id"}  # missing gbifID -> quality_filter -> None
    _patch_download_pipeline(monkeypatch, [bad_row, _GOOD_CSV_ROW])

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)

    assert len(captured_rows) == 1
    assert captured_rows[0]["gbif_occurrence_id"] == "42"


def test_update_gbif_occurrences_skips_record_outside_valid_zone_ids(monkeypatch):
    captured_rows: list[dict[str, object]] = []

    def fake_upsert(session, model, rows, update_columns=None):
        captured_rows.extend(rows)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    session = _FakeSession(zone_ids=set())  # nothing is a valid zone
    monkeypatch.setattr(gbif_occurrences, "job_run", _fake_job_run_yielding(session))
    monkeypatch.setattr(gbif_occurrences, "upsert", fake_upsert)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: aoi)
    monkeypatch.setattr(gbif_occurrences, "point_to_h3_cell", lambda lat, lon, res: "outside-zone")
    _patch_download_pipeline(monkeypatch, [_GOOD_CSV_ROW])

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)

    assert captured_rows == []


def test_update_gbif_occurrences_builds_predicate_with_convex_hull(monkeypatch):
    """GBIF rejects the idf AOI's raw polygon (466 vertices) -- the convex
    hull (22 vertices) must be what's sent, same reasoning as the old Search
    API fix, now applied to the `within` predicate instead of the `geometry`
    query param."""
    captured = {}

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    session = _FakeSession(zone_ids=set())
    monkeypatch.setattr(gbif_occurrences, "job_run", _fake_job_run_yielding(session))
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: aoi)
    monkeypatch.setattr(gbif_occurrences, "load_gbif_credentials", lambda: _FAKE_CREDS)

    def fake_request_download(predicate, creator, auth, client=None):
        captured["predicate"] = predicate
        return "key123"

    monkeypatch.setattr(gbif_occurrences, "request_download", fake_request_download)
    monkeypatch.setattr(gbif_occurrences, "wait_for_download", lambda *a, **k: None)
    monkeypatch.setattr(gbif_occurrences, "stream_download", lambda *a, **k: iter([]))

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)

    within = next(p for p in captured["predicate"]["predicates"] if p["type"] == "within")
    assert within["geometry"] == aoi.geom.convex_hull.wkt
    assert within["geometry"] != aoi.geom.wkt


def test_update_gbif_occurrences_logs_and_reraises_gbif_api_error(monkeypatch):
    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    session = _FakeSession(zone_ids=set())
    monkeypatch.setattr(gbif_occurrences, "job_run", _fake_job_run_yielding(session))
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: aoi)
    monkeypatch.setattr(gbif_occurrences, "load_gbif_credentials", lambda: _FAKE_CREDS)

    def fake_request_download(*a, **k):
        raise GbifApiError("simulated GBIF outage")

    monkeypatch.setattr(gbif_occurrences, "request_download", fake_request_download)

    with pytest.raises(GbifApiError):
        gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)


def test_update_gbif_occurrences_upserts_in_batches(monkeypatch):
    """Rows are flushed to `upsert` in bounded batches, not one call for the
    whole stream -- covers the memory-safety fix from the final review."""
    upsert_calls: list[int] = []

    def fake_upsert(session, model, rows, update_columns=None):
        upsert_calls.append(len(rows))

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    session = _FakeSession(zone_ids={"expected-zone"})
    monkeypatch.setattr(gbif_occurrences, "job_run", _fake_job_run_yielding(session))
    monkeypatch.setattr(gbif_occurrences, "upsert", fake_upsert)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: aoi)
    monkeypatch.setattr(gbif_occurrences, "point_to_h3_cell", lambda lat, lon, res: "expected-zone")
    monkeypatch.setattr(gbif_occurrences, "UPSERT_BATCH_SIZE", 2)

    rows = [{**_GOOD_CSV_ROW, "gbifID": str(i)} for i in range(5)]
    _patch_download_pipeline(monkeypatch, rows)

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)

    assert upsert_calls == [2, 2, 1]  # 5 rows, batch size 2 -> 2+2+1


def test_update_gbif_occurrences_logs_full_funnel_counts(monkeypatch, caplog):
    """Observability finding from final review: the final log line must
    report streamed -> rejected (quality) -> rejected (outside zones) ->
    stored, not just the stored count."""

    def fake_upsert(session, model, rows, update_columns=None):
        pass

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    session = _FakeSession(zone_ids={"expected-zone"})
    monkeypatch.setattr(gbif_occurrences, "job_run", _fake_job_run_yielding(session))
    monkeypatch.setattr(gbif_occurrences, "upsert", fake_upsert)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: aoi)
    monkeypatch.setattr(
        gbif_occurrences,
        "point_to_h3_cell",
        lambda lat, lon, res: "outside-zone" if lon == 3.0 else "expected-zone",
    )

    bad_quality_row = {"gbifID": "", "scientificName": "no id"}
    outside_zone_row = {**_GOOD_CSV_ROW, "gbifID": "99", "decimalLongitude": "3.0"}
    good_row = {**_GOOD_CSV_ROW, "gbifID": "42"}
    _patch_download_pipeline(monkeypatch, [bad_quality_row, outside_zone_row, good_row])

    with caplog.at_level("INFO"):
        gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)

    upserted_records = [
        r for r in caplog.records if getattr(r, "event", None) == "gbif_occurrences.upserted"
    ]
    assert len(upserted_records) == 1
    context = upserted_records[0].context
    assert context["n_streamed"] == 3
    assert context["n_rejected_quality"] == 1
    assert context["n_outside_zones"] == 1
    assert context["n_rows"] == 1


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_replaces_rows_within_refetched_window_scoped_to_aoi(monkeypatch):
    """The full history is kept (nothing older than the window is ever
    deleted) -- only the window just refetched gets replaced: rows inside
    `[date_min, date_max]` for this AOI are deleted (so stale/corrected GBIF
    data can't survive a refresh), rows outside that window for this AOI
    survive untouched, and another AOI's rows are never touched."""
    from clients.db import JobRun, ZonesHex
    from clients.db.init_db import init_db

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)

    with engine.begin() as conn:
        conn.execute(delete(SpeciesOccurrencesRaw))
        conn.execute(delete(ZonesHex).where(ZonesHex.zone_id.in_(["z-in", "z-out", "z-other"])))
        # Without this, a `success` JobRun row left behind by a previous run
        # of this exact test (same aoi/year/resolution -> same deterministic
        # idempotency_key) makes `job_run()` raise `JobAlreadySucceeded` and
        # skip the window replacement entirely on the next run -- same cleanup pattern the
        # other DB-gated tests below already use.
        conn.execute(delete(JobRun).where(JobRun.job_name == "update_gbif_occurrences"))

    from geo.h3_grid import generate_h3_grid

    generate_h3_grid(GOLDEN_AOI, resolution=5, engine=engine)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: _force_default_res(aoi, 5))
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    _patch_download_pipeline(monkeypatch, [])  # no new fetch needed for this test

    with engine.begin() as conn:
        conn.execute(
            SpeciesOccurrencesRaw.__table__.insert(),
            [
                {  # in-window, this AOI -> must be replaced (deleted, no new fetch data here)
                    "gbif_occurrence_id": "in-window",
                    "event_date": date(2020, 1, 1),
                    "gbif_taxon_key": None,
                    "scientific_name": "A",
                    "decimal_latitude": 1.0,
                    "decimal_longitude": 1.0,
                    "zone_id": "z-in",
                    "aoi_id": GOLDEN_AOI,
                },
                {  # out-of-window, this AOI -> kept forever, never touched
                    "gbif_occurrence_id": "out-of-window",
                    "event_date": date(2000, 1, 1),
                    "gbif_taxon_key": None,
                    "scientific_name": "B",
                    "decimal_latitude": 1.0,
                    "decimal_longitude": 1.0,
                    "zone_id": "z-out",
                    "aoi_id": GOLDEN_AOI,
                },
                {  # out-of-window, OTHER AOI -> must survive (not this AOI's window)
                    "gbif_occurrence_id": "other-aoi-old",
                    "event_date": date(2000, 1, 1),
                    "gbif_taxon_key": None,
                    "scientific_name": "C",
                    "decimal_latitude": 1.0,
                    "decimal_longitude": 1.0,
                    "zone_id": "z-other",
                    "aoi_id": "not-idf",
                },
            ],
        )

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)

    with engine.connect() as conn:
        remaining_ids = set(conn.scalars(select(SpeciesOccurrencesRaw.gbif_occurrence_id)).all())

    assert remaining_ids == {"out-of-window", "other-aoi-old"}


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_update_gbif_occurrences_skips_rerun_same_year(monkeypatch):
    """Job-level idempotence: rerunning for the same target year is a no-op skip."""
    from clients.db import JobRun
    from clients.db.init_db import init_db

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)

    with engine.begin() as conn:
        conn.execute(
            delete(SpeciesOccurrencesRaw).where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI)
        )
        conn.execute(delete(JobRun).where(JobRun.job_name == "update_gbif_occurrences"))

    from geo.h3_grid import generate_h3_grid

    generate_h3_grid(GOLDEN_AOI, resolution=5, engine=engine)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: _force_default_res(aoi, 5))
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    _patch_download_pipeline(monkeypatch, [{**_GOOD_CSV_ROW, "gbifID": "1001"}])

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)  # same year -> skip

    with engine.connect() as conn:
        count = conn.scalar(
            select(func.count())
            .select_from(SpeciesOccurrencesRaw)
            .where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI)
        )
        runs = conn.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(JobRun.job_name == "update_gbif_occurrences")
        )

    assert count == 1
    assert runs == 1


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_update_gbif_occurrences_refreshes_data_across_overlapping_windows(monkeypatch):
    """Two different job runs (different target years, overlapping windows)
    both fetch the same occurrence id -- must not raise, must not duplicate,
    and the second run's data must win (the window-replace delete clears the
    first run's row before the second run's fresh fetch is inserted, so a
    GBIF record that changed between runs -- corrected coordinates, updated
    taxonomy -- is reflected instead of silently kept stale)."""
    from clients.db import JobRun
    from clients.db.init_db import init_db

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)

    with engine.begin() as conn:
        conn.execute(
            delete(SpeciesOccurrencesRaw).where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI)
        )
        conn.execute(delete(JobRun).where(JobRun.job_name == "update_gbif_occurrences"))

    from geo.h3_grid import generate_h3_grid

    generate_h3_grid(GOLDEN_AOI, resolution=5, engine=engine)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: _force_default_res(aoi, 5))

    original_row = {**_GOOD_CSV_ROW, "gbifID": "1001"}
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2024, 1, 1))
    _patch_download_pipeline(monkeypatch, [original_row])
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)

    updated_row = {**original_row, "scientificName": "Vulpes vulpes (updated)"}
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    _patch_download_pipeline(monkeypatch, [updated_row])
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)  # must not raise

    with engine.connect() as conn:
        count = conn.scalar(
            select(func.count())
            .select_from(SpeciesOccurrencesRaw)
            .where(SpeciesOccurrencesRaw.gbif_occurrence_id == "1001")
        )
        successful_runs = conn.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(JobRun.job_name == "update_gbif_occurrences", JobRun.status == "success")
        )
        row = conn.execute(
            select(SpeciesOccurrencesRaw).where(SpeciesOccurrencesRaw.gbif_occurrence_id == "1001")
        ).one()

    assert successful_runs == 2
    assert count == 1  # still no duplicate row
    assert row.scientific_name == "Vulpes vulpes (updated)"  # 2nd run's fresher data won


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_update_gbif_occurrences_new_run_when_resolution_changes(monkeypatch):
    """Idempotency-key finding from final review: changing the AOI's
    resolution for the same target year must NOT be skipped as a rerun."""
    from clients.db import JobRun
    from clients.db.init_db import init_db

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)

    with engine.begin() as conn:
        conn.execute(
            delete(SpeciesOccurrencesRaw).where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI)
        )
        conn.execute(delete(JobRun).where(JobRun.job_name == "update_gbif_occurrences"))

    from geo.h3_grid import generate_h3_grid

    generate_h3_grid(GOLDEN_AOI, resolution=5, engine=engine)
    generate_h3_grid(GOLDEN_AOI, resolution=6, engine=engine)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    _patch_download_pipeline(monkeypatch, [])

    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: _force_default_res(aoi, 5))
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)

    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: _force_default_res(aoi, 6))
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)  # must NOT skip

    with engine.connect() as conn:
        runs = conn.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(JobRun.job_name == "update_gbif_occurrences", JobRun.status == "success")
        )

    assert runs == 2  # different resolution -> different idempotency_key -> both ran
