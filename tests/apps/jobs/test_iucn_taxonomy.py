"""Tests pour jobs.pipelines.iucn_taxonomy — normalisation, copie brute, idempotence."""

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from clients import IucnTaxa, JobAlreadySucceeded
from core import ENV_VAR

import jobs.pipelines.iucn_taxonomy as job


def _assessment(taxon_id, name="Lamna nasus", category="VU", year="2019", assessment_id=None):
    return {
        "sis_taxon_id": taxon_id,
        "taxon_scientific_name": name,
        "red_list_category_code": category,
        "year_published": year,
        "assessment_id": assessment_id if assessment_id is not None else (taxon_id or 0) * 10,
    }


class FakeClient:
    def __init__(self, version="2026-1", assessments=None):
        self.version = version
        self.assessments = assessments if assessments is not None else [_assessment(1)]
        self.fetches = 0
        self.closed = False

    def red_list_version(self):
        return self.version

    def iter_country_assessments(self, country):
        self.fetches += 1
        yield from self.assessments

    def close(self):
        self.closed = True


@pytest.fixture
def in_tmp_dev(monkeypatch, tmp_path):
    """Exécution en dev, artefacts dans un dossier temporaire."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("BIOWATCH_DATA_ROOT", raising=False)
    return tmp_path


@pytest.fixture
def fake_db(monkeypatch):
    """Remplace job_run/upsert : capture la clé et les lignes écrites."""
    captured: dict = {"keys": [], "rows": []}

    @contextmanager
    def fake_job_run(job_name, scope, idempotency_key, engine=None):
        captured["keys"].append(idempotency_key)
        captured["scope"] = scope
        yield object(), object()

    def fake_upsert(session, model, rows):
        captured["model"] = model
        captured["rows"].extend(rows)

    monkeypatch.setattr(job, "job_run", fake_job_run)
    monkeypatch.setattr(job, "upsert", fake_upsert)
    return captured


# --- normalisation (logique pure) ---------------------------------------------


def test_normalize_maps_api_fields_to_table_columns():
    rows = job.normalize_assessments([_assessment(10030, "Hexanchus griseus", "NT", "2020")], "v")
    assert rows == [
        {
            "iucn_taxon_id": 10030,
            "scientific_name": "Hexanchus griseus",
            "category": "NT",
            "assessment_id": 100300,
            "year_published": 2020,
            "source_version": "v",
        }
    ]


def test_normalize_cleans_name_and_category():
    """Le nom est la clé de jointure avec GBIF : espaces parasites supprimés."""
    [row] = job.normalize_assessments([_assessment(1, "  Lamna   nasus ", " vu ")], "v")
    assert row["scientific_name"] == "Lamna nasus"
    assert row["category"] == "VU"


def test_normalize_keeps_one_row_per_taxon_the_most_recent():
    rows = job.normalize_assessments(
        [
            _assessment(1, category="EN", year="2015"),
            _assessment(1, category="VU", year="2021"),
            _assessment(1, category="CR", year="2010"),
        ],
        "v",
    )
    assert [(r["iucn_taxon_id"], r["category"]) for r in rows] == [(1, "VU")]


def test_normalize_skips_incomplete_assessments():
    """Une évaluation incomplète est ignorée, pas fatale pour tout l'import."""
    rows = job.normalize_assessments(
        [
            _assessment(None),
            _assessment(2, name=""),
            _assessment(3, category=None),
            {**_assessment(5), "assessment_id": None},
            _assessment(4),
        ],
        "v",
    )
    assert [r["iucn_taxon_id"] for r in rows] == [4]


def test_normalize_tolerates_missing_year():
    [row] = job.normalize_assessments([_assessment(1, year=None)], "v")
    assert row["year_published"] is None


# --- job ----------------------------------------------------------------------


def test_raw_path_is_versioned_under_the_env_data_root(in_tmp_dev):
    assert job.raw_path("dev", "2026-1", "FR") == Path("data/dev/raw/iucn/2026-1/FR.json")


def test_job_writes_the_raw_copy_and_upserts_rows(in_tmp_dev, fake_db):
    client = FakeClient(assessments=[_assessment(1), _assessment(2, "Hexanchus griseus", "NT")])

    job.update_iucn_taxonomy("fr", client=client)

    raw = in_tmp_dev / "data/dev/raw/iucn/2026-1/FR.json"
    assert json.loads(raw.read_text()) == client.assessments
    assert fake_db["model"] is IucnTaxa
    assert fake_db["scope"] == "FR"  # pays normalisé
    assert {r["iucn_taxon_id"] for r in fake_db["rows"]} == {1, 2}
    assert {r["source_version"] for r in fake_db["rows"]} == {"2026-1"}


def test_job_reuses_the_raw_copy_instead_of_calling_the_api(in_tmp_dev, fake_db):
    raw = in_tmp_dev / "data/dev/raw/iucn/2026-1/FR.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps([_assessment(7)]))
    client = FakeClient()

    job.update_iucn_taxonomy(client=client)

    assert client.fetches == 0
    assert [r["iucn_taxon_id"] for r in fake_db["rows"]] == [7]


def test_idempotency_key_changes_with_the_red_list_version(in_tmp_dev, fake_db):
    job.update_iucn_taxonomy(client=FakeClient(version="2026-1"))
    job.update_iucn_taxonomy(client=FakeClient(version="2026-1"))
    job.update_iucn_taxonomy(client=FakeClient(version="2027-1"))

    first, same, other = fake_db["keys"]
    assert first == same
    assert first != other


def test_job_skips_when_already_succeeded(in_tmp_dev, monkeypatch):
    @contextmanager
    def already_done(*args, idempotency_key="", **kwargs):
        raise JobAlreadySucceeded(idempotency_key, uuid.uuid4())
        yield  # pragma: no cover

    monkeypatch.setattr(job, "job_run", already_done)
    client = FakeClient()

    job.update_iucn_taxonomy(client=client)  # ne lève pas

    assert client.fetches == 0


def test_job_closes_the_client_it_created(in_tmp_dev, fake_db, monkeypatch):
    created = FakeClient()
    monkeypatch.setattr(job, "IucnClient", lambda: created)

    job.update_iucn_taxonomy()

    assert created.closed


def test_job_leaves_an_injected_client_open(in_tmp_dev, fake_db):
    client = FakeClient()
    job.update_iucn_taxonomy(client=client)
    assert not client.closed


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_job_is_idempotent_against_postgres(in_tmp_dev):
    """Deux runs → un seul job_run en success, pas de doublon dans staging.iucn_taxa."""
    from clients.db import JobRun
    from clients.db.init_db import init_db
    from sqlalchemy import create_engine, func, select

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)
    client = FakeClient(assessments=[_assessment(1), _assessment(2)])

    job.update_iucn_taxonomy(client=client, engine=engine)
    job.update_iucn_taxonomy(client=client, engine=engine)

    with engine.connect() as conn:
        taxa = conn.scalar(select(func.count()).select_from(IucnTaxa))
        runs = conn.scalar(
            select(func.count()).select_from(JobRun).where(JobRun.job_name == job.JOB_NAME)
        )
    assert taxa == 2
    assert runs == 1
