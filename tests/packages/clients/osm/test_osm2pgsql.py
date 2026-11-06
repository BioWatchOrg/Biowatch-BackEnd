"""Tests for clients.osm.osm2pgsql — command line, credentials handling, exit codes."""

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from clients.db.schemas import InvalidSchemaName
from clients.osm import osm2pgsql
from clients.osm.osm2pgsql import STYLE_PATH, Osm2pgsqlError, build_command, run_osm2pgsql

ENGINE = create_engine("postgresql+psycopg://bw:s3cret@db.local:6543/biowatch_dev")
PBFS = [Path("/data/alsace-240101.osm.pbf"), Path("/data/lorraine-240101.osm.pbf")]


def test_style_file_ships_with_the_package():
    assert STYLE_PATH.is_file()


def test_build_command_targets_schema_in_slim_drop_mode():
    cmd = build_command("osm2pgsql", PBFS, "osm_tmp_ges_2024")
    assert cmd[0] == "osm2pgsql"
    for flag in ("--create", "--slim", "--drop", "--output=flex"):
        assert flag in cmd
    assert f"--style={STYLE_PATH}" in cmd
    assert "--schema=osm_tmp_ges_2024" in cmd
    assert "--middle-schema=osm_tmp_ges_2024" in cmd
    assert cmd[-2:] == [str(p) for p in PBFS]


def test_build_command_rejects_unsafe_schema():
    with pytest.raises(InvalidSchemaName):
        build_command("osm2pgsql", PBFS, "public")


def test_libpq_env_comes_from_the_engine_url():
    assert osm2pgsql._libpq_env(ENGINE) == {
        "PGHOST": "db.local",
        "PGPORT": "6543",
        "PGUSER": "bw",
        "PGDATABASE": "biowatch_dev",
        "PGPASSWORD": "s3cret",
    }


@pytest.fixture
def fake_run(monkeypatch):
    """Capture subprocess.run calls; returncode configurable via `fake_run.returncode`."""
    calls: list[dict] = []

    def run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, fake_run.returncode, "", "FATAL: boom")

    fake_run.returncode = 0
    fake_run.calls = calls
    monkeypatch.setattr(osm2pgsql.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(osm2pgsql.subprocess, "run", run)
    return fake_run


def test_run_passes_password_via_env_not_argv(fake_run):
    run_osm2pgsql(PBFS, "osm_tmp_ges_2024", engine=ENGINE)
    (call,) = fake_run.calls
    assert call["env"]["PGPASSWORD"] == "s3cret"
    assert not any("s3cret" in part for part in call["cmd"])


def test_run_raises_with_stderr_on_failure(fake_run):
    fake_run.returncode = 1
    with pytest.raises(Osm2pgsqlError, match="code 1: FATAL: boom"):
        run_osm2pgsql(PBFS, "osm_tmp_ges_2024", engine=ENGINE)


def test_run_uses_binary_override(fake_run, monkeypatch):
    monkeypatch.setenv("OSM2PGSQL_BIN", "/opt/osm2pgsql/bin/osm2pgsql")
    run_osm2pgsql(PBFS, "osm_tmp_ges_2024", engine=ENGINE)
    assert fake_run.calls[0]["cmd"][0] == "/opt/osm2pgsql/bin/osm2pgsql"


def test_run_fails_fast_when_binary_missing(monkeypatch):
    monkeypatch.setattr(osm2pgsql.shutil, "which", lambda b: None)
    with pytest.raises(Osm2pgsqlError, match="not found"):
        run_osm2pgsql(PBFS, "osm_tmp_ges_2024", engine=ENGINE)
