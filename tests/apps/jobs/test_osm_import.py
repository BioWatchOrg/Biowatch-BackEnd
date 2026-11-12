"""Tests for jobs.pipelines.osm_import — orchestration, idempotence, output_ref, scheduler.

Real `job_runs` (SQLite, no geometry involved); Geofabrik, osm2pgsql and
PostgreSQL schemas are faked at the module boundary.
"""

import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from clients import JobRun, JobRunZoneError
from clients.osm import snapshot_for
from clients.db.session import get_sessionmaker
from jobs.pipelines import osm_import
from jobs.pipelines.osm_import import (
    import_idempotency_key,
    osm_import_scheduled,
    osm_import_snapshot,
    tmp_schema_name,
)

IDF_2016_PBF = "https://download.geofabrik.de/europe/france/ile-de-france-160101.osm.pbf"


def _schema(env="dev"):
    """Schema of the default idf/2016 import (Geofabrik mapping) for `env`."""
    key = import_idempotency_key(snapshot_for("idf", "2016"), "2016", env)
    return tmp_schema_name("idf", "2016", key)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/runs.db", future=True)
    JobRun.__table__.create(eng)
    JobRunZoneError.__table__.create(eng)
    return eng


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    """Fake every external side effect and record what happened, in order."""

    class Fakes:
        events: list[tuple] = []
        existing_schemas: set[str] = set()
        fail_on: str | None = None
        downloaded: list[Path] = []

    f = Fakes()
    f.events, f.existing_schemas, f.downloaded = [], set(), []

    def maybe_fail(step):
        if f.fail_on == step:
            raise RuntimeError(f"{step} failed")

    def download(url, dest_dir):
        maybe_fail("download")
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / url.rsplit("/", 1)[1]
        path.write_bytes(b"pbf")
        f.downloaded.append(path)
        f.events.append(("download", url))
        return path

    def create_schema(name, engine=None):
        f.existing_schemas.add(name)
        f.events.append(("create_schema", name))

    def drop_schema(name, engine=None):
        f.existing_schemas.discard(name)
        f.events.append(("drop_schema", name))

    def run_osm2pgsql(paths, schema, engine=None):
        assert all(p.exists() for p in paths)
        maybe_fail("osm2pgsql")
        f.events.append(("osm2pgsql", schema, [p.name for p in paths]))

    monkeypatch.setattr(osm_import, "download", download)
    monkeypatch.setattr(osm_import, "create_schema", create_schema)
    monkeypatch.setattr(osm_import, "drop_schema", drop_schema)
    monkeypatch.setattr(osm_import, "schema_exists", lambda n, e=None: n in f.existing_schemas)
    monkeypatch.setattr(osm_import, "run_osm2pgsql", run_osm2pgsql)
    monkeypatch.setenv("OSM_DATA_DIR", str(tmp_path / "osm"))
    return f


def _runs(engine):
    with get_sessionmaker(engine)() as s:
        return list(s.scalars(select(JobRun)).all())


def test_tmp_schema_name_is_suffixed_with_the_key():
    assert tmp_schema_name("idf", "2016", "0123456789abcdef") == "osm_tmp_idf_2016_01234567"


def test_style_version_is_part_of_the_source_version_and_key(monkeypatch):
    snap = snapshot_for("idf", "2016")
    assert osm_import.import_source_version(snap) == "geofabrik_2016-01-01_style-v1"
    key_v1 = import_idempotency_key(snap, "2016", "prod")
    monkeypatch.setattr(osm_import, "STYLE_VERSION", "2")
    assert osm_import.import_source_version(snap) == "geofabrik_2016-01-01_style-v2"
    assert import_idempotency_key(snap, "2016", "prod") != key_v1


def test_import_downloads_imports_and_records_success(engine, fakes):
    assert osm_import_snapshot("idf", "2016", engine=engine) is True

    assert fakes.events == [
        ("download", IDF_2016_PBF),
        ("create_schema", _schema()),
        ("osm2pgsql", _schema(), ["ile-de-france-160101.osm.pbf"]),
    ]
    (run,) = _runs(engine)
    assert run.status == "success"
    assert run.job_name == "osm_import_snapshot"
    assert run.scope == "region:idf"
    assert run.bucket_id == "2016"


def test_output_ref_holds_the_schema_and_source_version(engine, fakes):
    osm_import_snapshot("idf", "2016", env="prod", engine=engine)

    (run,) = _runs(engine)
    assert json.loads(run.output_ref) == {
        "schema": _schema("prod"),
        "source_version": "geofabrik_2016-01-01_style-v1",
    }


def test_downloaded_files_are_deleted(engine, fakes):
    osm_import_snapshot("idf", "2016", engine=engine)
    assert fakes.downloaded and not any(p.exists() for p in fakes.downloaded)


def test_second_run_is_skipped(engine, fakes):
    osm_import_snapshot("idf", "2016", engine=engine)
    fakes.events.clear()

    assert osm_import_snapshot("idf", "2016", engine=engine) is False
    assert fakes.events == []
    assert len(_runs(engine)) == 1


def test_env_is_part_of_the_idempotency_key(engine, fakes):
    osm_import_snapshot("idf", "2016", env="dev", engine=engine)
    assert osm_import_snapshot("idf", "2016", env="prod", engine=engine) is True
    assert len(_runs(engine)) == 2


def test_another_key_never_drops_a_successful_runs_schema(engine, fakes, monkeypatch):
    osm_import_snapshot("idf", "2016", env="prod", engine=engine)
    fakes.events.clear()

    # Same AOI and bucket, other keys: other env, manual links, new style version.
    osm_import_snapshot("idf", "2016", env="dev", engine=engine)
    osm_import_snapshot(
        "idf",
        "2016",
        env="prod",
        links=["https://mirror.example/ile-de-france-160101.osm.pbf"],
        engine=engine,
    )
    monkeypatch.setattr(osm_import, "STYLE_VERSION", "2")
    osm_import_snapshot("idf", "2016", env="prod", engine=engine)

    assert not any(event[0] == "drop_schema" for event in fakes.events)
    assert _schema("prod") in fakes.existing_schemas
    schemas = {json.loads(run.output_ref)["schema"] for run in _runs(engine)}
    assert len(schemas) == 4 and schemas <= fakes.existing_schemas


def test_failure_marks_run_failed_and_cleans_files(engine, fakes):
    fakes.fail_on = "osm2pgsql"
    with pytest.raises(RuntimeError, match="osm2pgsql failed"):
        osm_import_snapshot("idf", "2016", engine=engine)

    (run,) = _runs(engine)
    assert run.status == "failed"
    assert "osm2pgsql failed" in run.error_message
    assert run.output_ref is None
    assert not any(p.exists() for p in fakes.downloaded)


def test_retry_drops_schema_left_by_failed_attempt(engine, fakes):
    fakes.fail_on = "osm2pgsql"
    with pytest.raises(RuntimeError):
        osm_import_snapshot("idf", "2016", engine=engine)
    assert _schema() in fakes.existing_schemas  # leftover of the crash

    fakes.fail_on = None
    fakes.events.clear()
    assert osm_import_snapshot("idf", "2016", engine=engine) is True

    assert fakes.events[0] == ("drop_schema", _schema())
    assert ("create_schema", _schema()) in fakes.events
    (run,) = _runs(engine)
    assert run.status == "success"


def test_unknown_aoi_fails_before_recording_a_run(engine, fakes):
    with pytest.raises(Exception, match="not found"):
        osm_import_snapshot("atlantis", "2016", engine=engine)
    assert _runs(engine) == []


def test_links_override_the_geofabrik_mapping(engine, fakes):
    links = ["https://mirror.example/bretagne-160101.osm.pbf"]
    osm_import_snapshot("idf", "2016", links=links, engine=engine)
    assert fakes.events[0] == ("download", links[0])


def test_scheduler_imports_next_region_then_has_nothing_to_do(engine, fakes):
    assert osm_import_scheduled(bucket="2016", engine=engine) == "idf"
    assert osm_import_scheduled(bucket="2016", engine=engine) is None
    assert len(_runs(engine)) == 1


def test_scheduler_skips_aois_missing_from_registry(engine, fakes, monkeypatch):
    monkeypatch.setattr(osm_import, "aoi_registry", lambda label: label == "bre")
    monkeypatch.setattr(osm_import, "osm_import_snapshot", lambda *a, **kw: True)
    assert osm_import_scheduled(bucket="2016", engine=engine) == "bre"


def test_scheduler_retries_a_failed_region(engine, fakes):
    fakes.fail_on = "download"
    with pytest.raises(RuntimeError):
        osm_import_scheduled(bucket="2016", engine=engine)
    fakes.fail_on = None
    assert osm_import_scheduled(bucket="2016", engine=engine) == "idf"


def test_scheduler_failing_region_does_not_block_the_others(engine, fakes, monkeypatch, caplog):
    monkeypatch.setattr(osm_import, "aoi_registry", lambda label: label in ("idf", "bre"))
    monkeypatch.setattr(osm_import, "load_aoi", lambda label: None)
    fake_download = osm_import.download

    def download(url, dest_dir):
        if "ile-de-france" in url:
            raise RuntimeError("idf extract unavailable")
        return fake_download(url, dest_dir)

    monkeypatch.setattr(osm_import, "download", download)

    def region_failures():
        return [
            r.context
            for r in caplog.records
            if getattr(r, "event", None) == "osm_import.region_failed"
        ]

    with caplog.at_level(logging.ERROR, logger=osm_import.__name__):
        with pytest.raises(RuntimeError, match="idf extract unavailable"):
            osm_import_scheduled(bucket="2016", engine=engine)
        # idf failed, bre was never attempted: bre goes first.
        assert osm_import_scheduled(bucket="2016", engine=engine) == "bre"
        # Only idf is left: it is retried.
        with pytest.raises(RuntimeError):
            osm_import_scheduled(bucket="2016", engine=engine)

    assert [(c["aoi"], c["retry"]) for c in region_failures()] == [("idf", False), ("idf", True)]


def test_scheduler_defaults_to_current_year(engine, fakes, monkeypatch):
    monkeypatch.setattr(osm_import, "format_bucket_id", lambda format: "2026")
    osm_import_scheduled(engine=engine)
    (run,) = _runs(engine)
    assert run.bucket_id == "2026"
