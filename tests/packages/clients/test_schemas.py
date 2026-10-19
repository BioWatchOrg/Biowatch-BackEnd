"""Tests for clients.db.schemas — name validation and the DDL actually issued."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from clients.db import schemas
from clients.db.schemas import (
    InvalidSchemaName,
    create_schema,
    drop_schema,
    schema_exists,
    validate_schema_name,
)


@pytest.fixture
def session(monkeypatch):
    """Replace session_scope with a mock session (pg_namespace is PostgreSQL-only)."""
    mock = MagicMock()

    @contextmanager
    def fake_scope(engine=None):
        yield mock

    monkeypatch.setattr(schemas, "session_scope", fake_scope)
    return mock


def _sql(session) -> list[str]:
    return [str(call.args[0]) for call in session.execute.call_args_list]


@pytest.mark.parametrize("name", ["osm_tmp_idf_2016", "a", "_x1"])
def test_valid_names_are_returned_unchanged(name):
    assert validate_schema_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "public", "pg_catalog", "Osm", "1abc", "osm-tmp", "a; DROP TABLE x", "a" * 64],
)
def test_unsafe_names_are_rejected(name):
    with pytest.raises(InvalidSchemaName):
        validate_schema_name(name)


def test_create_schema_issues_create(session):
    create_schema("osm_tmp_idf_2016")
    assert _sql(session) == ["CREATE SCHEMA osm_tmp_idf_2016"]


def test_drop_schema_issues_drop_cascade(session):
    drop_schema("osm_tmp_idf_2016")
    assert _sql(session) == ["DROP SCHEMA IF EXISTS osm_tmp_idf_2016 CASCADE"]


@pytest.mark.parametrize("found", [True, False])
def test_schema_exists_queries_pg_namespace(session, found):
    session.scalar.return_value = found
    assert schema_exists("osm_tmp_idf_2016") is found
    stmt, params = session.scalar.call_args.args
    assert "pg_namespace" in str(stmt)
    assert params == {"name": "osm_tmp_idf_2016"}


def test_ddl_is_never_issued_for_an_unsafe_name(session):
    for fn in (create_schema, drop_schema, schema_exists):
        with pytest.raises(InvalidSchemaName):
            fn("public")
    session.execute.assert_not_called()
    session.scalar.assert_not_called()
