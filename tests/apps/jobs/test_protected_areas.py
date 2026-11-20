"""Tests pour jobs.pipelines.protected_areas — orchestration, idempotence, fermetures.

L'INPN est remplacé par une source factice : la date d'édition et les sites
sont choisis par le test, aucun téléchargement n'a lieu. Les tests marqués
PostGIS enchaînent plusieurs éditions sur une vraie base.
"""

import datetime
import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import jobs.pipelines.protected_areas as job
import pytest
from clients import DatabaseConfigError, JobAlreadySucceeded
from clients.inpn import InpnError, InpnSite, InpnSource
from core import ENV_VAR
from geo import cell_to_polygon, compute_h3_cells, reproject
from protected_areas import TooManyClosuresError, build_source_version
from sqlalchemy import create_engine, delete, select

AOI = "idf"
RES = 5  # 52 zones : grille golden de tests/packages/geo
EDITION_1 = datetime.date(2025, 11, 28)
EDITION_2 = datetime.date(2026, 5, 4)
EDITION_3 = datetime.date(2026, 11, 6)


def _zones() -> list[str]:
    return sorted(compute_h3_cells(AOI, RES))


def _site_on(zone_id: str, created: str = "2009-10-22") -> InpnSite:
    """Une réserve qui recouvre exactement l'hexagone `zone_id` (Lambert-93, comme l'INPN)."""
    return InpnSite(
        site_id=f"site-{zone_id}",
        geom=reproject(cell_to_polygon(zone_id)),
        attrs={"DateCreaENP": created},
    )


class FakeInpn:
    """Édition et sites de chaque type, modifiables d'un run à l'autre."""

    def __init__(self, monkeypatch, tmp_root):
        self.editions: dict[str, datetime.date] = {}
        self.sites: dict[str, list[InpnSite]] = {}
        self.downloads: list[str] = []
        self.work_dirs: list = []

        def last_modified(url, client=None):
            area_type = self._type_of(url)
            if area_type not in self.editions:
                raise InpnError(f"injoignable : {url}")
            return self.editions[area_type]

        def download(url, dest_dir, client=None):
            self.downloads.append(self._type_of(url))
            self.work_dirs.append(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / "archive.zip"
            path.write_bytes(b"PK")
            return path

        monkeypatch.setattr(job, "archive_last_modified", last_modified)
        monkeypatch.setattr(job, "download_archive", download)
        monkeypatch.setattr(
            job,
            "INPN_SOURCES",
            {
                area_type: InpnSource(f"https://inpn.test/{area_type}.zip", self._reader(area_type))
                for area_type in ("natura2000", "reserve", "znieff_type1")
            },
        )

    @staticmethod
    def _type_of(url: str) -> str:
        return url.rsplit("/", 1)[-1].removesuffix(".zip")

    def _reader(self, area_type):
        def read(archive, work_dir, bbox):
            assert archive.exists()  # lu avant le nettoyage du répertoire de travail
            return list(self.sites.get(area_type, []))

        return read


@pytest.fixture
def in_tmp_dev(monkeypatch, project_root):
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("BIOWATCH_DATA_ROOT", raising=False)
    return project_root


@pytest.fixture
def inpn(monkeypatch, in_tmp_dev):
    return FakeInpn(monkeypatch, in_tmp_dev)


@pytest.fixture
def fake_db(monkeypatch):
    """Remplace la base : zones de l'AOI connues, aucune édition précédente, écritures capturées."""
    captured: dict = {"rows": [], "keys": [], "zone_errors": [], "output_refs": []}

    @contextmanager
    def fake_job_run(job_name, scope, idempotency_key, engine=None):
        captured["keys"].append((scope, idempotency_key))
        run = SimpleNamespace(run_id=uuid.uuid4(), output_ref=None)
        yield run, object()
        captured["output_refs"].append(json.loads(run.output_ref))

    @contextmanager
    def fake_session_scope(engine=None):
        yield object()

    monkeypatch.setattr(job, "job_run", fake_job_run)
    monkeypatch.setattr(job, "session_scope", fake_session_scope)
    monkeypatch.setattr(job, "zone_ids_for_aoi", lambda session, aoi, res: _zones())
    monkeypatch.setattr(job, "previous_protected_area_rows", lambda session, *args: (None, []))
    monkeypatch.setattr(job, "upsert", lambda session, model, rows: captured["rows"].extend(rows))
    monkeypatch.setattr(
        job,
        "record_zone_error",
        lambda run_id, zone_id, message, engine=None: captured["zone_errors"].append(zone_id),
    )
    return captured


# --- orchestration (sans base) ----------------------------------------------------


def test_each_type_is_processed_in_its_own_run(inpn, fake_db):
    zones = _zones()
    inpn.editions = {"natura2000": EDITION_1, "reserve": EDITION_3, "znieff_type1": EDITION_1}
    inpn.sites = {"reserve": [_site_on(zones[0])], "natura2000": [_site_on(zones[1], "")]}

    statuses = job.update_protected_areas(AOI, resolution=RES)

    assert statuses == {"natura2000": "success", "reserve": "success", "znieff_type1": "success"}
    assert [scope for scope, _ in fake_db["keys"]] == [
        "idf:natura2000",
        "idf:reserve",
        "idf:znieff_type1",
    ]
    # Le site Natura 2000 sans date est écarté ; la réserve donne une ligne ouverte.
    [row] = fake_db["rows"]
    assert row["zone_id"] == zones[0]
    assert row["protected_area_type"] == "reserve"
    assert row["valid_from"] == datetime.date(2009, 10, 22)
    assert row["valid_to"] is None
    assert row["coverage_ratio"] == pytest.approx(1.0)
    assert row["source_version"] == build_source_version("reserve", EDITION_3)
    assert fake_db["output_refs"][1] == {
        "source_version": build_source_version("reserve", EDITION_3),
        "n_rows": 1,
        "n_closed": 0,
    }


def test_the_downloaded_archive_is_deleted_after_reading(inpn, fake_db, in_tmp_dev):
    inpn.editions = {"reserve": EDITION_3}
    job.update_protected_areas(AOI, ["reserve"], resolution=RES)

    [work_dir] = inpn.work_dirs
    assert work_dir.is_relative_to(in_tmp_dev / "data" / "dev" / "inpn")
    assert not work_dir.exists()


def test_the_archive_is_deleted_even_when_reading_fails(inpn, fake_db, monkeypatch):
    inpn.editions = {"reserve": EDITION_3}

    def broken(archive, work_dir, bbox):
        raise InpnError("format inattendu")

    monkeypatch.setitem(
        job.INPN_SOURCES, "reserve", InpnSource("https://inpn.test/reserve.zip", broken)
    )

    assert job.update_protected_areas(AOI, ["reserve"], resolution=RES) == {"reserve": "failed"}
    assert not inpn.work_dirs[0].exists()


def test_a_failing_source_does_not_block_the_others(inpn, fake_db):
    inpn.editions = {"natura2000": EDITION_1, "znieff_type1": EDITION_1}  # reserve injoignable

    statuses = job.update_protected_areas(AOI, resolution=RES)

    assert statuses == {"natura2000": "success", "reserve": "failed", "znieff_type1": "success"}
    assert inpn.downloads == ["natura2000", "znieff_type1"]


def test_an_unchanged_archive_is_skipped_without_download(inpn, monkeypatch):
    inpn.editions = {"reserve": EDITION_3}

    @contextmanager
    def already_done(job_name, scope, idempotency_key, engine=None):
        raise JobAlreadySucceeded(idempotency_key, uuid.uuid4())
        yield  # pragma: no cover

    monkeypatch.setattr(job, "job_run", already_done)

    assert job.update_protected_areas(AOI, ["reserve"], resolution=RES) == {"reserve": "skipped"}
    assert inpn.downloads == []


def test_zone_errors_make_the_run_partial(inpn, fake_db, monkeypatch):
    zones = _zones()
    inpn.editions = {"reserve": EDITION_3}
    inpn.sites = {"reserve": [_site_on(zones[0])]}

    def coverage_with_error(zone_geoms, sites):
        from protected_areas import ZoneCoverage

        return (
            {zones[0]: ZoneCoverage(1.0, datetime.date(2009, 10, 22))},
            {zones[1]: "GEOS a refusé la géométrie"},
        )

    monkeypatch.setattr(job, "compute_zone_coverage", coverage_with_error)

    assert job.update_protected_areas(AOI, ["reserve"], resolution=RES) == {"reserve": "partial"}
    assert fake_db["zone_errors"] == [zones[1]]


def test_missing_grid_fails_the_type_with_a_clear_message(inpn, fake_db, monkeypatch, caplog):
    inpn.editions = {"reserve": EDITION_3}
    monkeypatch.setattr(job, "zone_ids_for_aoi", lambda session, aoi, res: [])

    assert job.update_protected_areas(AOI, ["reserve"], resolution=RES) == {"reserve": "failed"}
    [failure] = [r for r in caplog.records if r.event == "protected_areas.type_failed"]
    assert "generate_h3_grid" in failure.context["error"]
    assert inpn.downloads == []


def test_a_broken_database_config_stops_the_job(inpn, monkeypatch):
    inpn.editions = {"natura2000": EDITION_1, "reserve": EDITION_3}

    @contextmanager
    def no_database(*args, **kwargs):
        raise DatabaseConfigError("DATABASE_URL manquant")
        yield  # pragma: no cover

    monkeypatch.setattr(job, "job_run", no_database)
    with pytest.raises(DatabaseConfigError):
        job.update_protected_areas(AOI, resolution=RES)


def test_unknown_type_is_rejected_before_any_request(inpn, fake_db):
    with pytest.raises(job.ProtectedAreasError, match="parc_national"):
        job.update_protected_areas(AOI, ["reserve", "parc_national"], resolution=RES)
    assert fake_db["keys"] == []


def test_resolution_defaults_to_the_aoi_one(inpn, fake_db, monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(
        job, "zone_ids_for_aoi", lambda session, aoi, res: seen.append(res) or _zones()
    )
    inpn.editions = {"reserve": EDITION_3}

    job.update_protected_areas(AOI, ["reserve"])

    from core import load_aoi

    assert seen == [load_aoi(AOI).default_res]


def test_same_key_for_the_same_archive_new_key_for_a_new_edition(inpn, fake_db):
    inpn.editions = {"reserve": EDITION_2}
    job.update_protected_areas(AOI, ["reserve"], resolution=RES)
    job.update_protected_areas(AOI, ["reserve"], resolution=RES)
    inpn.editions = {"reserve": EDITION_3}
    job.update_protected_areas(AOI, ["reserve"], resolution=RES)

    first, again, new_edition = (key for _, key in fake_db["keys"])
    assert first == again
    assert new_edition != first


# --- bout en bout sur PostGIS ----------------------------------------------------


def _forget_runs_and_rows(engine) -> None:
    """Repart de zéro pour ce job sur l'AOI de test (base de test réutilisable)."""
    from clients import ProtectedAreasByZone, session_scope
    from clients.db import JobRun, JobRunZoneError

    ours = JobRun.job_name == job.JOB_NAME
    with session_scope(engine) as session:
        session.execute(
            delete(JobRunZoneError).where(
                JobRunZoneError.run_id.in_(select(JobRun.run_id).where(ours))
            )
        )
        session.execute(delete(JobRun).where(ours))
        session.execute(
            delete(ProtectedAreasByZone).where(ProtectedAreasByZone.zone_id.in_(_zones()))
        )


def _rows(engine, source_version: str) -> dict[str, tuple]:
    """Lignes d'une édition sur les zones de test (la base peut porter d'autres AOI ou résolutions)."""
    from clients import ProtectedAreasByZone, session_scope

    with session_scope(engine) as session:
        return {
            row.zone_id: (row.valid_from, row.valid_to, round(row.coverage_ratio or 0.0, 3))
            for row in session.scalars(
                select(ProtectedAreasByZone).where(
                    ProtectedAreasByZone.source_version == source_version,
                    ProtectedAreasByZone.zone_id.in_(_zones()),
                )
            )
        }


def _statuses(engine) -> list[str]:
    from clients.db import JobRun

    with engine.connect() as conn:
        return list(
            conn.scalars(
                select(JobRun.status)
                .where(JobRun.job_name == job.JOB_NAME)
                .order_by(JobRun.started_at)
            )
        )


@pytest.fixture
def engine(postgis_url, in_tmp_dev, monkeypatch):
    from clients.db.init_db import init_db
    from geo import generate_h3_grid

    monkeypatch.setenv(ENV_VAR, "dev")
    eng = create_engine(postgis_url, future=True)
    init_db(eng)
    generate_h3_grid(AOI, RES, engine=eng)  # sauté si la grille golden existe déjà
    _forget_runs_and_rows(eng)
    yield eng
    _forget_runs_and_rows(eng)


def test_editions_open_close_and_reopen_zones(inpn, engine):
    z = _zones()
    created = datetime.date(2009, 10, 22)

    # Édition 1 : trois zones couvertes.
    inpn.editions = {"reserve": EDITION_1}
    inpn.sites = {"reserve": [_site_on(z[0]), _site_on(z[1]), _site_on(z[2])]}
    assert job.update_protected_areas(AOI, ["reserve"], resolution=RES, engine=engine) == {
        "reserve": "success"
    }
    v1 = build_source_version("reserve", EDITION_1)
    assert _rows(engine, v1) == {zone: (created, None, 1.0) for zone in z[:3]}

    # Même archive relancée : sautée, rien de nouveau.
    assert job.update_protected_areas(AOI, ["reserve"], resolution=RES, engine=engine) == {
        "reserve": "skipped"
    }

    # Édition 2 : z1 et z2 ont disparu → fermées à la date de l'édition 2.
    inpn.editions = {"reserve": EDITION_2}
    inpn.sites = {"reserve": [_site_on(z[0])]}
    job.update_protected_areas(AOI, ["reserve"], resolution=RES, engine=engine)
    v2 = build_source_version("reserve", EDITION_2)
    assert _rows(engine, v2) == {
        z[0]: (created, None, 1.0),
        z[1]: (created, EDITION_2, 1.0),
        z[2]: (created, EDITION_2, 1.0),
    }
    assert _rows(engine, v1) == {zone: (created, None, 1.0) for zone in z[:3]}  # historique intact

    # Édition 3 : z1 revient (même date de création) → rouverte ; z2 reste fermée à l'édition 2.
    inpn.editions = {"reserve": EDITION_3}
    inpn.sites = {"reserve": [_site_on(z[0]), _site_on(z[1])]}
    job.update_protected_areas(AOI, ["reserve"], resolution=RES, engine=engine)
    assert _rows(engine, build_source_version("reserve", EDITION_3)) == {
        z[0]: (created, None, 1.0),
        z[1]: (created, None, 1.0),
        z[2]: (created, EDITION_2, 1.0),
    }
    assert _statuses(engine) == ["success", "success", "success"]


def test_mass_closure_fails_the_run_and_writes_nothing(inpn, engine):
    z = _zones()
    inpn.editions = {"reserve": EDITION_1}
    inpn.sites = {"reserve": [_site_on(zone) for zone in z[:20]]}
    job.update_protected_areas(AOI, ["reserve"], resolution=RES, engine=engine)

    # Archive tronquée : il ne reste que 5 sites sur 20.
    inpn.editions = {"reserve": EDITION_2}
    inpn.sites = {"reserve": [_site_on(zone) for zone in z[:5]]}
    assert job.update_protected_areas(AOI, ["reserve"], resolution=RES, engine=engine) == {
        "reserve": "failed"
    }
    assert _rows(engine, build_source_version("reserve", EDITION_2)) == {}
    assert _statuses(engine) == ["success", "failed"]

    # La baisse est réelle : relancée avec le garde-fou levé, elle passe.
    statuses = job.update_protected_areas(
        AOI, ["reserve"], resolution=RES, max_closed_share=1, engine=engine
    )
    assert statuses == {"reserve": "success"}
    closed = [
        zone
        for zone, (_, valid_to, _) in _rows(
            engine, build_source_version("reserve", EDITION_2)
        ).items()
        if valid_to
    ]
    assert len(closed) == 15


def test_too_many_closures_error_is_what_fails_the_run(inpn, fake_db, monkeypatch):
    """Garde-fou levé à l'intérieur du job_run : le run est `failed`, rien n'est écrit."""
    inpn.editions = {"reserve": EDITION_2}

    def refuse(*args, **kwargs):
        raise TooManyClosuresError("trop de fermetures")

    monkeypatch.setattr(job, "carry_over_closed_rows", refuse)
    assert job.update_protected_areas(AOI, ["reserve"], resolution=RES) == {"reserve": "failed"}
    assert fake_db["rows"] == []
