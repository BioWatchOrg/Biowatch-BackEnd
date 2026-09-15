"""Tests for clients.db.job_runs — run lifecycle, idempotent skip/retry, zone errors."""

import pytest
from sqlalchemy import create_engine, func, select, text

from clients.db import (
    JobAlreadySucceeded,
    JobRun,
    JobRunZoneError,
    get_failed_zones,
    job_run,
    record_zone_error,
)
from clients.db.session import get_sessionmaker

KEY = "abc123"


@pytest.fixture
def engine(tmp_path):
    """A file-backed SQLite engine (shared across connections) with the run tables."""
    eng = create_engine(f"sqlite:///{tmp_path}/runs.db", future=True)
    # job_runs / job_run_zone_errors carry no geometry, so SQLite is enough here.
    JobRun.__table__.create(eng)
    JobRunZoneError.__table__.create(eng)
    # A neutral table to observe commit/rollback of the session yielded by job_run.
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE probe (id INTEGER PRIMARY KEY, val TEXT)"))
    return eng


def _get_run(engine, key=KEY):
    with get_sessionmaker(engine)() as s:
        return s.scalar(select(JobRun).where(JobRun.idempotency_key == key))


def _count_runs(engine, key=KEY):
    with get_sessionmaker(engine)() as s:
        return s.scalar(
            select(func.count()).select_from(JobRun).where(JobRun.idempotency_key == key)
        )


def _probe_count(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM probe")).scalar()


def test_creates_run_and_marks_success(engine):
    with job_run("ingest", scope="idf", idempotency_key=KEY, bucket_id="2024-Q1", engine=engine):
        pass
    run = _get_run(engine)
    assert run.status == "success"
    assert run.ended_at is not None
    assert run.error_message is None


def test_marks_failed_and_reraises(engine):
    with pytest.raises(ValueError, match="boom"):
        with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine):
            raise ValueError("boom")
    run = _get_run(engine)
    assert run.status == "failed"
    assert "boom" in run.error_message
    assert run.ended_at is not None


def test_yielded_session_commits_on_success(engine):
    with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine) as (run, session):
        session.execute(text("INSERT INTO probe (val) VALUES ('x')"))
    assert _probe_count(engine) == 1  # committed at clean exit
    assert _get_run(engine).status == "success"


def test_yielded_session_rolls_back_on_failure(engine):
    with pytest.raises(ValueError):
        with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine) as (run, session):
            session.execute(text("INSERT INTO probe (val) VALUES ('x')"))
            raise ValueError("boom")
    assert _probe_count(engine) == 0  # data reverted...
    assert _get_run(engine).status == "failed"  # ...but the failed status persists


def test_skips_when_already_succeeded(engine):
    with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine):
        pass

    executed = False
    with pytest.raises(JobAlreadySucceeded) as exc:
        with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine):
            executed = True  # pragma: no cover - must never run
    assert executed is False
    assert exc.value.idempotency_key == KEY
    assert _count_runs(engine) == 1  # no duplicate run created


def test_retries_a_failed_run_in_place(engine):
    with pytest.raises(ValueError):
        with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine):
            raise ValueError("first attempt fails")
    first_id = _get_run(engine).run_id

    # Same key again: failed → retryable, must NOT raise JobAlreadySucceeded.
    with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine):
        pass

    run = _get_run(engine)
    assert run.status == "success"
    assert run.run_id == first_id  # reused, not a new row
    assert _count_runs(engine) == 1


def test_zone_errors_downgrade_success_to_partial(engine):
    with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine) as (run, session):
        record_zone_error(run.run_id, "zone-a", "no valid pixels", engine=engine)
    run = _get_run(engine)
    assert run.status == "partial"
    assert run.error_message is None  # partial is not a hard failure


def test_get_failed_zones_lists_failed_zones(engine):
    with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine) as (run, session):
        record_zone_error(run.run_id, "zone-a", "boom", engine=engine)
        record_zone_error(run.run_id, "zone-b", "boom", engine=engine)
    assert set(get_failed_zones(KEY, engine=engine)) == {"zone-a", "zone-b"}


def test_get_failed_zones_empty_when_no_run(engine):
    assert get_failed_zones("does-not-exist", engine=engine) == []


def test_retry_of_partial_clears_previous_zone_errors(engine):
    # First attempt: partial (zone-a failed).
    with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine) as (run, session):
        record_zone_error(run.run_id, "zone-a", "boom", engine=engine)
    assert _get_run(engine).status == "partial"
    first_id = _get_run(engine).run_id

    # Retry reuses the run; a clean pass this time must resolve to success with
    # no stale zone errors left behind.
    with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine):
        pass

    run = _get_run(engine)
    assert run.status == "success"
    assert run.run_id == first_id
    with get_sessionmaker(engine)() as s:
        remaining = s.scalars(select(JobRunZoneError)).all()
    assert remaining == []  # previous attempt's zone errors were purged


def test_record_zone_error_is_idempotent(engine):
    with job_run("ingest", scope="idf", idempotency_key=KEY, engine=engine) as (run, session):
        record_zone_error(run.run_id, "zone-a", "nodata", engine=engine)
        record_zone_error(run.run_id, "zone-a", "nodata again", engine=engine)  # same zone
        record_zone_error(run.run_id, "zone-b", "timeout", engine=engine)

    with get_sessionmaker(engine)() as s:
        errors = s.scalars(select(JobRunZoneError)).all()
        by_zone = {e.zone_id: e.error_message for e in errors}

    assert len(errors) == 2  # zone-a de-duplicated
    assert by_zone["zone-a"] == "nodata again"  # message updated in place
    assert by_zone["zone-b"] == "timeout"
