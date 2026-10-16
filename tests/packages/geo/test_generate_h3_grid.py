"""Tests for geo.generate_h3_grid — idempotence via job_run + zones_hex upsert."""

import os
import uuid
from contextlib import contextmanager

from sqlalchemy import create_engine, func, select

import geo.h3_grid as h3_grid
from clients import JobAlreadySucceeded

GOLDEN_AOI = "idf"
GOLDEN_RES = 5
GOLDEN_COUNT = 52  # cf. tests/packages/geo/test_h3_grid.py


def test_generate_h3_grid_skips_when_already_succeeded(monkeypatch, project_root):
    """If job_run reports the run already succeeded, the job is a no-op (no raise)."""
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


def _golden_runs(engine) -> list[tuple[str, str]]:
    """(clé, statut) des lignes `job_runs` de la grille golden présentes en base."""
    from clients.db import JobRun

    with engine.connect() as conn:
        return [
            (key, status)
            for key, status in conn.execute(
                select(JobRun.idempotency_key, JobRun.status).where(
                    JobRun.job_name == "generate_h3_grid", JobRun.scope == GOLDEN_AOI
                )
            )
        ]


def _forget_golden_runs(engine) -> None:
    """Oublie les runs précédents de la grille golden, pour que le test exécute VRAIMENT un run.

    Sans ça, sur une base réutilisée (base locale jetable d'une session à l'autre),
    tous les appels skipperaient et le test ne prouverait plus rien. Ne touche que
    les lignes `job_runs` de ce job / cette AOI (et leurs erreurs par zone) ;
    `zones_hex` est laissée telle quelle : c'est à l'UPSERT d'absorber l'existant.
    """
    from sqlalchemy import delete

    from clients import session_scope
    from clients.db import JobRun, JobRunZoneError

    golden = (JobRun.job_name == "generate_h3_grid") & (JobRun.scope == GOLDEN_AOI)
    with session_scope(engine) as session:
        run_ids = select(JobRun.run_id).where(golden)
        session.execute(delete(JobRunZoneError).where(JobRunZoneError.run_id.in_(run_ids)))
        session.execute(delete(JobRun).where(golden))


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


def test_generate_h3_grid_is_idempotent(project_root, monkeypatch, postgis_url):
    """Full run against PostGIS: two runs → exactly one `job_runs` row, no duplicated zones.

    Replayable against a reused database: previous golden runs are forgotten
    first, so the first call really executes and the second one really skips.
    Zones are never deleted: the UPSERT must absorb whatever is already there.
    """
    from clients.db.init_db import init_db
    from core import ENV_VAR

    monkeypatch.setenv(ENV_VAR, "dev")
    engine = create_engine(postgis_url, future=True)
    init_db(engine)
    _forget_golden_runs(engine)

    h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES, engine=engine)
    h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES, engine=engine)  # 2nd run must skip

    assert [status for _key, status in _golden_runs(engine)] == ["success"]  # one run, not two
    assert _golden_zone_count(engine) == GOLDEN_COUNT  # no duplicates despite two runs
    assert (project_root / "data" / "dev").is_dir()  # the run really executed


def test_generate_h3_grid_dev_and_prod_runs_do_not_skip_each_other(
    project_root, monkeypatch, postgis_url
):
    """Sur une vraie base : un `success` en dev ne fait pas skipper le run prod.

    Comportement bout-en-bout attendu de « env dans la clé » : deux lignes
    `job_runs` en `success` avec deux clés distinctes, toujours aucun doublon
    dans `zones_hex` (UPSERT), et un répertoire d'artefacts par env.
    """
    from clients.db.init_db import init_db
    from core import ENV_VAR

    engine = create_engine(postgis_url, future=True)
    init_db(engine)
    _forget_golden_runs(engine)

    for env in ("dev", "prod"):
        monkeypatch.setenv(ENV_VAR, env)
        h3_grid.generate_h3_grid(GOLDEN_AOI, GOLDEN_RES, engine=engine)

    runs = _golden_runs(engine)
    assert sorted(status for _key, status in runs) == ["success", "success"]  # deux vrais runs
    assert len({key for key, _status in runs}) == 2  # une clé par environnement
    assert _golden_zone_count(engine) == GOLDEN_COUNT  # même grille, pas de doublon entre les envs
    assert (project_root / "data" / "dev").is_dir() and (project_root / "data" / "prod").is_dir()


def test_generate_h3_grid_writes_the_artifact_under_data_env(monkeypatch, project_root):
    """L'artefact parquet atterrit dans `<racine>/data/<env>/grids/...`, plus dans `docs/`.

    Le chemin de stockage par environnement est un livrable central de la
    séparation dev/prod : il doit être couvert par un test qui tourne en CI,
    pas seulement par le test d'idempotence qui exige une vraie PostGIS.
    """
    from types import SimpleNamespace

    from core import ENV_VAR, load_aoi

    tmp_path = project_root
    # cwd = tmp_path : l'assertion sur `docs/` ci-dessous porte sur l'ancien
    # emplacement `docs/grids`, qui était relatif au cwd.
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
