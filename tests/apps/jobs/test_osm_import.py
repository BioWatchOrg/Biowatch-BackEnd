"""Tests for jobs.pipelines.osm_import — orchestration, idempotence, signal, scheduler.

Real `job_runs` (SQLite, no geometry involved); Geofabrik, osm2pgsql, PostgreSQL
schemas and SQS are faked at the module boundary.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from clients import JobRun, JobRunZoneError
from clients.db.session import get_sessionmaker
from jobs.pipelines import osm_import
from jobs.pipelines.osm_import import (
    osm_import_scheduled,
    osm_import_snapshot,
    tmp_schema_name,
)

QUEUE_URL = "https://sqs.eu-west-3.amazonaws.com/123456789012/osm_import_done"


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
        messages: list[tuple[str, dict]] = []
        downloaded: list[Path] = []

    f = Fakes()
    f.events, f.existing_schemas, f.messages, f.downloaded = [], set(), [], []

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

    def send_message(queue_url, body):
        maybe_fail("sqs")
        f.messages.append((queue_url, body))

    monkeypatch.setattr(osm_import, "download", download)
    monkeypatch.setattr(osm_import, "create_schema", create_schema)
    monkeypatch.setattr(osm_import, "drop_schema", drop_schema)
    monkeypatch.setattr(osm_import, "schema_exists", lambda n, e=None: n in f.existing_schemas)
    monkeypatch.setattr(osm_import, "run_osm2pgsql", run_osm2pgsql)
    monkeypatch.setattr(osm_import, "send_message", send_message)
    monkeypatch.setenv("OSM_DATA_DIR", str(tmp_path / "osm"))
    monkeypatch.delenv("OSM_IMPORT_DONE_QUEUE_URL", raising=False)
    return f


def _runs(engine):
    with get_sessionmaker(engine)() as s:
        return list(s.scalars(select(JobRun)).all())


def test_tmp_schema_name():
    assert tmp_schema_name("idf", "2016") == "osm_tmp_idf_2016"


def test_import_downloads_imports_and_records_success(engine, fakes):
    assert osm_import_snapshot("idf", "2016", engine=engine) is True

    assert fakes.events == [
        ("download", "https://download.geofabrik.de/europe/france/ile-de-france-160101.osm.pbf"),
        ("create_schema", "osm_tmp_idf_2016"),
        ("osm2pgsql", "osm_tmp_idf_2016", ["ile-de-france-160101.osm.pbf"]),
    ]
    (run,) = _runs(engine)
    assert run.status == "success"
    assert run.job_name == "osm_import_snapshot"
    assert run.scope == "region:idf"
    assert run.bucket_id == "2016"
    assert run.output_ref == "osm_tmp_idf_2016"


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
    assert "osm_tmp_idf_2016" in fakes.existing_schemas  # leftover of the crash

    fakes.fail_on = None
    fakes.events.clear()
    assert osm_import_snapshot("idf", "2016", engine=engine) is True

    assert fakes.events[0] == ("drop_schema", "osm_tmp_idf_2016")
    assert ("create_schema", "osm_tmp_idf_2016") in fakes.events
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


def test_sqs_signal_is_disabled_without_queue_url(engine, fakes):
    osm_import_snapshot("idf", "2016", engine=engine)
    assert fakes.messages == []


def test_sqs_signal_is_published_when_queue_configured(engine, fakes, monkeypatch):
    monkeypatch.setenv("OSM_IMPORT_DONE_QUEUE_URL", QUEUE_URL)
    osm_import_snapshot("idf", "2016", env="prod", engine=engine)

    ((queue_url, body),) = fakes.messages
    (run,) = _runs(engine)
    assert queue_url == QUEUE_URL
    assert body == {
        "region_id": "idf",
        "bucket_id": "2016",
        "schema_name": "osm_tmp_idf_2016",
        "source_version": "geofabrik_2016-01-01",
        "h3_res": 8,
        "env": "prod",
        "pbf_links": ["https://download.geofabrik.de/europe/france/ile-de-france-160101.osm.pbf"],
        "run_id": str(run.run_id),
    }


def test_sqs_failure_fails_the_run_so_it_is_retried(engine, fakes, monkeypatch):
    monkeypatch.setenv("OSM_IMPORT_DONE_QUEUE_URL", QUEUE_URL)
    fakes.fail_on = "sqs"
    with pytest.raises(RuntimeError, match="sqs failed"):
        osm_import_snapshot("idf", "2016", engine=engine)
    (run,) = _runs(engine)
    assert run.status == "failed"


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


def test_scheduler_defaults_to_current_year(engine, fakes, monkeypatch):
    monkeypatch.setattr(osm_import, "bucket_id", lambda format: "2026")
    osm_import_scheduled(engine=engine)
    (run,) = _runs(engine)
    assert run.bucket_id == "2026"
