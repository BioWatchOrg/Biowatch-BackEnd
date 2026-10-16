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


def _golden_key(env: str) -> str:
    """Clé d'idempotence attendue pour la grille golden dans l'env donné."""
    from core import compute_idempotency_key, load_aoi

    return compute_idempotency_key(
        job_name="generate_h3_grid",
        scope=GOLDEN_AOI,
        resolution=GOLDEN_RES,
        source_version=load_aoi(GOLDEN_AOI).version,
        env=env,
    )


def _golden_zone_count(engine) -> int:
    """Cellules de la grille golden en base (filtrées : la base de test peut en porter d'autres)."""
    from clients.db import ZonesHex

    with engine.connect() as conn:
        return (
            conn.scalar(
                select(func.count())
                .select_from(ZonesHex)
                .where(ZonesHex.aoi_id == GOLDEN_AOI, ZonesHex.resolution == GOLDEN_RES)
            )
            or 0
        )


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_generate_h3_grid_is_idempotent(project_root, monkeypatch):
    """Full run against PostGIS: two runs → one `job_runs` row, no duplicated zones.

    Assertions scoped to this run's idempotency key and AOI so the test is
    replayable against a database that already holds other runs (CI reuses the
    same service DB across the module; a local throwaway DB is reused across
    sessions). Nothing is dropped or deleted.
    """
    from clients.db import JobRun
    from clients.db.init_db import init_db
    from core import ENV_VAR

    monkeypatch.setenv(ENV_VAR, "dev")
    # parquet artifact lands under project_root/data (isolated tmp dir)
    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)
    key = _golden_key("dev")

    h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES, engine=engine)
    h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES, engine=engine)  # 2nd run must skip

    with engine.connect() as conn:
        statuses = list(conn.scalars(select(JobRun.status).where(JobRun.idempotency_key == key)))

    assert statuses == ["success"]  # exactly one run for this key, and it succeeded
    assert _golden_zone_count(engine) == GOLDEN_COUNT  # no duplicates despite two runs


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_generate_h3_grid_dev_and_prod_runs_do_not_skip_each_other(project_root, monkeypatch):
    """Sur une vraie base : un `success` en dev ne fait pas skipper le run prod.

    C'est le comportement bout-en-bout attendu de « env dans la clé » : deux
    lignes `job_runs` en `success`, une par env, et toujours aucun doublon
    dans `zones_hex` (UPSERT). Complète le test unitaire sur les clés.
    """
    from clients.db import JobRun
    from clients.db.init_db import init_db
    from core import ENV_VAR

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)
    keys = {env: _golden_key(env) for env in ("dev", "prod")}

    for env in ("dev", "prod"):
        monkeypatch.setenv(ENV_VAR, env)
        h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES, engine=engine)

    with engine.connect() as conn:
        statuses = dict(
            conn.execute(
                select(JobRun.idempotency_key, JobRun.status).where(
                    JobRun.idempotency_key.in_(list(keys.values()))
                )
            ).all()
        )

    # Rejouable : sur une base déjà peuplée, un run skippe (déjà `success`),
    # ce qui est exactement l'idempotence attendue — les deux clés restent en succès.
    assert statuses == {keys["dev"]: "success", keys["prod"]: "success"}
    assert _golden_zone_count(engine) == GOLDEN_COUNT  # même grille, pas de doublon entre les envs


def test_generate_h3_grid_writes_the_artifact_under_data_env(monkeypatch, project_root):
    """L'artefact parquet atterrit dans `<racine>/data/<env>/grids/...`, plus dans `docs/`.

    Le chemin de stockage par environnement est un livrable central de la
    séparation dev/prod : il doit être couvert par un test qui tourne en CI,
    pas seulement par le test d'idempotence qui exige une vraie PostGIS.
    """
    from types import SimpleNamespace

    from core import ENV_VAR, load_aoi

    tmp_path = project_root
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


def test_generate_h3_grid_isolates_prod_artifacts_from_dev(monkeypatch, project_root):
    """Le même job en prod écrit ailleurs : les deux envs ne se marchent pas dessus."""
    from types import SimpleNamespace

    from core import ENV_VAR

    tmp_path = project_root
    monkeypatch.setenv(ENV_VAR, "prod")

    @contextmanager
    def fake_job_run(*args, **kwargs):
        yield SimpleNamespace(run_id=uuid.uuid4()), object()

    monkeypatch.setattr(h3_grid, "job_run", fake_job_run)
    monkeypatch.setattr(h3_grid, "upsert", lambda **kwargs: None)

    h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES)

    assert list((tmp_path / "data").iterdir()) == [tmp_path / "data" / "prod"]
    assert not (tmp_path / "data" / "dev").exists()


def test_env_discriminates_the_idempotency_key_of_the_job(monkeypatch, project_root):
    """Le JOB passe bien l'env dans sa clé : même AOI, même résolution, dev ≠ prod.

    On capture la clé reçue par `job_run` plutôt que d'appeler
    `compute_idempotency_key` directement : si quelqu'un retire `env=env`
    dans `generate_h3_grid`, ce test doit casser.
    """
    from types import SimpleNamespace

    from core import ENV_VAR, compute_idempotency_key, load_aoi

    keys: dict[str, str] = {}

    @contextmanager
    def fake_job_run(*args, idempotency_key="", **kwargs):
        keys[os.environ[ENV_VAR]] = idempotency_key
        yield SimpleNamespace(run_id=uuid.uuid4()), object()

    monkeypatch.setattr(h3_grid, "job_run", fake_job_run)
    monkeypatch.setattr(h3_grid, "upsert", lambda **kwargs: None)

    for env in ("dev", "prod"):
        monkeypatch.setenv(ENV_VAR, env)
        h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES)

    assert keys["dev"] != keys["prod"]
    version = load_aoi(GOLDEN_AOI).version
    for env in ("dev", "prod"):
        assert keys[env] == compute_idempotency_key(
            job_name="generate_h3_grid",
            scope=GOLDEN_AOI,
            resolution=GOLDEN_RES,
            source_version=version,
            env=env,
        )
