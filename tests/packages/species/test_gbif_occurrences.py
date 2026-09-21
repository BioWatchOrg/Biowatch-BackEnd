"""Tests for species.gbif_occurrences.update_gbif_occurrences."""

import os
import uuid
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine, delete, func, select

import species.gbif_occurrences as gbif_occurrences
from clients import JobAlreadySucceeded, SpeciesOccurrencesRaw

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


def _force_default_res(aoi, resolution):
    """Return an AOI copy with `default_res` overridden, matching a specific grid."""
    return aoi.__class__(**{**aoi.__dict__, "default_res": resolution})


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_update_gbif_occurrences_skips_rerun_same_year(monkeypatch):
    """Job-level idempotence: rerunning for the *same* target year is a no-op skip.

    Both calls compute the identical idempotency_key (same aoi + same target
    year), so the second call's `job_run` raises `JobAlreadySucceeded` before
    its body runs again -- no second fetch, no second upsert attempt. This
    proves job-level idempotence only; it does NOT exercise the `ON CONFLICT
    DO NOTHING` row-level dedup path, since the body never re-executes. See
    `test_update_gbif_occurrences_row_level_dedup_across_overlapping_windows`
    for that.
    """
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

    fake_records = {
        2020: [
            {
                "key": 1001,
                "scientificName": "Vulpes vulpes",
                "taxonKey": 5219243,
                "decimalLatitude": 48.8566,
                "decimalLongitude": 2.3522,
                "eventDate": "2020-06-15T00:00:00",
            }
        ]
    }

    def fake_fetch_occurrences(geometry_wkt, year, client=None):
        return iter(fake_records.get(year, []))

    monkeypatch.setattr(gbif_occurrences, "fetch_occurrences", fake_fetch_occurrences)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))

    # Force AOI resolution to match the res-5 grid just generated.
    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: _force_default_res(aoi, 5))

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
        row = conn.execute(
            select(SpeciesOccurrencesRaw).where(
                SpeciesOccurrencesRaw.gbif_occurrence_id == "1001"
            )
        ).one()

    assert count == 1
    assert runs == 1  # 2nd call skipped at job_run entry -- no new run recorded
    assert row.scientific_name == "Vulpes vulpes"
    assert row.event_date == date(2020, 6, 15)


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_update_gbif_occurrences_row_level_dedup_across_overlapping_windows(monkeypatch):
    """Row-level dedup: two *different* job runs (adjacent target years whose
    rolling windows overlap) both re-fetch and attempt to upsert the same
    historical GBIF occurrence.

    Target years differ (2024 then 2025) so each call computes a *different*
    idempotency_key -- `job_run` does NOT skip the second call, it genuinely
    re-executes the body and re-attempts the insert. The occurrence's PK
    (`gbif_occurrence_id`) collides with the row from the first run, so this
    exercises the actual `upsert(..., update_columns=[])` -> `ON CONFLICT DO
    NOTHING` path: no exception should surface, no duplicate row should
    appear, and (since DO NOTHING discards rather than merges) the row's data
    must still be the *first* run's data, not the second run's.
    """
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

    original_record = {
        "key": 1001,
        "scientificName": "Vulpes vulpes",
        "taxonKey": 5219243,
        "decimalLatitude": 48.8566,
        "decimalLongitude": 2.3522,
        "eventDate": "2020-06-15T00:00:00",
    }

    # Run 1: target year 2024 -> window [2015, 2024] covers eventDate 2020-06-15.
    def fake_fetch_run1(geometry_wkt, year, client=None):
        return iter([original_record] if year == 2020 else [])

    monkeypatch.setattr(gbif_occurrences, "fetch_occurrences", fake_fetch_run1)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2024, 1, 1))
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)

    # Run 2: target year 2025 -> different idempotency_key (different `period`),
    # so job_run does NOT skip. Window [2016, 2025] also covers 2020-06-15, so
    # the same gbif_occurrence_id=1001 is re-fetched and re-attempted -- with
    # *different* data this time, to prove DO NOTHING discards the conflicting
    # attempt instead of silently merging/overwriting.
    conflicting_record = {**original_record, "scientificName": "SHOULD NOT OVERWRITE"}

    def fake_fetch_run2(geometry_wkt, year, client=None):
        return iter([conflicting_record] if year == 2020 else [])

    monkeypatch.setattr(gbif_occurrences, "fetch_occurrences", fake_fetch_run2)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
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
            select(SpeciesOccurrencesRaw).where(
                SpeciesOccurrencesRaw.gbif_occurrence_id == "1001"
            )
        ).one()

    assert successful_runs == 2  # both runs genuinely executed (distinct idempotency_key)
    assert count == 1  # PK conflict on the 2nd upsert -> DO NOTHING, no duplicate row
    assert row.scientific_name == "Vulpes vulpes"  # original data untouched, not overwritten
    assert row.event_date == date(2020, 6, 15)
