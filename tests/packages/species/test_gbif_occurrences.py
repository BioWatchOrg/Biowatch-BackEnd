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


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_update_gbif_occurrences_is_idempotent_and_deduplicates(monkeypatch):
    """Full run against Postgres: rerun with an overlapping fetch → no duplicates."""
    from clients.db import JobRun
    from clients.db.init_db import init_db

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)

    with engine.begin() as conn:
        conn.execute(
            delete(SpeciesOccurrencesRaw).where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI)
        )
        conn.execute(
            delete(JobRun).where(JobRun.job_name == "update_gbif_occurrences")
        )

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
    monkeypatch.setattr(
        gbif_occurrences, "load_aoi", lambda label: aoi.__class__(**{**aoi.__dict__, "default_res": 5})
    )

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)  # 2nd run must skip (same year)

    with engine.connect() as conn:
        count = conn.scalar(
            select(func.count())
            .select_from(SpeciesOccurrencesRaw)
            .where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI)
        )
        row = conn.execute(
            select(SpeciesOccurrencesRaw).where(
                SpeciesOccurrencesRaw.gbif_occurrence_id == "1001"
            )
        ).one()

    assert count == 1  # no duplicate despite two runs
    assert row.scientific_name == "Vulpes vulpes"
    assert row.event_date == date(2020, 6, 15)
