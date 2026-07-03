"""Tests for geo.generate_h3_grid — idempotence via job_run + zones_hex upsert."""

import os
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, func, select

import geo.h3_grid as h3_grid
from clients import JobAlreadySucceeded

GOLDEN_AOI = "idf"
GOLDEN_RES = 5
GOLDEN_COUNT = 52  # cf. tests/packages/geo/test_h3_grid.py


def test_generate_h3_grid_skips_when_already_succeeded(monkeypatch, tmp_path):
    """If job_run reports the run already succeeded, the job is a no-op (no raise)."""
    monkeypatch.chdir(tmp_path)
    called = {"body": False}

    @contextmanager
    def fake_job_run(*args, idempotency_key="", **kwargs):
        raise JobAlreadySucceeded(idempotency_key, uuid.uuid4())
        called["body"] = True  # pragma: no cover - unreachable
        yield  # pragma: no cover

    monkeypatch.setattr(h3_grid, "job_run", fake_job_run)

    # Must swallow JobAlreadySucceeded and return cleanly.
    h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES)
    assert called["body"] is False


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_generate_h3_grid_is_idempotent(tmp_path, monkeypatch):
    """Full run against PostGIS: two runs → one job, no duplicated zones."""
    from clients.db import JobRun, ZonesHex
    from clients.db.init_db import init_db

    monkeypatch.chdir(tmp_path)  # parquet artifact lands in the tmp dir
    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)

    h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES, engine=engine)
    h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES, engine=engine)  # 2nd run must skip

    with engine.connect() as conn:
        zones = conn.scalar(select(func.count()).select_from(ZonesHex))
        runs = conn.scalar(
            select(func.count()).select_from(JobRun).where(JobRun.job_name == "generate_h3_grid")
        )
        successes = conn.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(JobRun.job_name == "generate_h3_grid", JobRun.status == "success")
        )

    assert zones == GOLDEN_COUNT  # no duplicates despite two runs
    assert runs == 1  # single run reused (idempotent), not two
    assert successes == 1
