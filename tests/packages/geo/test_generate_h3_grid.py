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


def test_generate_h3_grid_writes_the_artifact_under_data_env(monkeypatch, tmp_path):
    """L'artefact parquet atterrit dans `data/<env>/grids/...`, plus dans `docs/`.

    Le chemin de stockage par environnement est un livrable central de la
    séparation dev/prod : il doit être couvert par un test qui tourne en CI,
    pas seulement par le test d'idempotence qui exige une vraie PostGIS.
    """
    from types import SimpleNamespace

    from core import ENV_VAR, load_aoi

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ENV_VAR, raising=False)  # rien de posé ⇒ dev par défaut

    @contextmanager
    def fake_job_run(*args, **kwargs):
        yield SimpleNamespace(run_id=uuid.uuid4()), object()

    written: list[list[dict]] = []
    monkeypatch.setattr(h3_grid, "job_run", fake_job_run)
    monkeypatch.setattr(h3_grid, "upsert", lambda **kwargs: written.append(kwargs["rows"]))

    h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES)

    version = load_aoi(GOLDEN_AOI).version
    expected = (
        tmp_path
        / "data"
        / "dev"
        / "grids"
        / f"aoi={GOLDEN_AOI}"
        / f"version={version}"
        / f"res={GOLDEN_RES}"
        / "grid.parquet"
    )
    assert expected.is_file(), f"artefact attendu en {expected}"
    assert not (tmp_path / "docs").exists(), "l'ancien emplacement docs/grids ne doit plus servir"
    assert len(written[0]) == GOLDEN_COUNT


def test_generate_h3_grid_isolates_prod_artifacts_from_dev(monkeypatch, tmp_path):
    """Le même job en prod écrit ailleurs : les deux envs ne se marchent pas dessus."""
    from types import SimpleNamespace

    from core import ENV_VAR

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ENV_VAR, "prod")

    @contextmanager
    def fake_job_run(*args, **kwargs):
        yield SimpleNamespace(run_id=uuid.uuid4()), object()

    monkeypatch.setattr(h3_grid, "job_run", fake_job_run)
    monkeypatch.setattr(h3_grid, "upsert", lambda **kwargs: None)

    h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES)

    assert list((tmp_path / "data").iterdir()) == [tmp_path / "data" / "prod"]
    assert not (tmp_path / "data" / "dev").exists()


def test_env_discriminates_the_idempotency_key(monkeypatch):
    """Même AOI, même résolution, deux envs ⇒ deux clés distinctes."""
    from core import ENV_VAR, compute_idempotency_key, load_aoi

    aoi = load_aoi(GOLDEN_AOI)
    monkeypatch.delenv(ENV_VAR, raising=False)

    def key_for(env: str) -> str:
        return compute_idempotency_key(
            job_name="generate_h3_grid",
            scope=GOLDEN_AOI,
            resolution=GOLDEN_RES,
            source_version=aoi.version,
            env=env,
        )

    assert key_for("dev") != key_for("prod")
