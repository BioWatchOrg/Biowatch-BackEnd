"""Tests for clients.db.session — URL building, engine memoization, session factory."""

import pytest
from clients.db.session import _build_database_url, get_sessionmaker, get_engine
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

POSTGRES_VARS = (
    "DATABASE_URL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Isolate the process env: neutralize .env loading and clear POSTGRES_* vars."""
    # Prevent the real .env from leaking into these tests.
    monkeypatch.setattr("clients.db.session.load_dotenv", lambda *a, **k: False)
    for var in POSTGRES_VARS:
        monkeypatch.delenv(var, raising=False)
    # A memoized engine from a previous test must not bleed into this one.
    get_engine.cache_clear()
    yield monkeypatch
    get_engine.cache_clear()


def test_database_url_takes_precedence(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql+psycopg://u:p@example:5432/db")
    # POSTGRES_* are intentionally absent — they must be ignored entirely.
    assert _build_database_url() == "postgresql+psycopg://u:p@example:5432/db"


def test_url_built_from_postgres_vars(clean_env):
    clean_env.setenv("POSTGRES_USER", "biowatch")
    clean_env.setenv("POSTGRES_PASSWORD", "secret")
    clean_env.setenv("POSTGRES_DB", "biowatch_dev")
    clean_env.setenv("POSTGRES_HOST", "db.internal")
    clean_env.setenv("POSTGRES_PORT", "6543")
    assert (
        _build_database_url()
        == "postgresql+psycopg://biowatch:secret@db.internal:6543/biowatch_dev"
    )


def test_host_and_port_default_to_localhost(clean_env):
    clean_env.setenv("POSTGRES_USER", "u")
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "d")
    # No POSTGRES_HOST / POSTGRES_PORT set.
    assert _build_database_url() == "postgresql+psycopg://u:p@127.0.0.1:5432/d"


def test_missing_required_var_raises(clean_env):
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "d")
    # POSTGRES_USER missing → explicit failure rather than a silent bad URL.
    with pytest.raises(KeyError):
        _build_database_url()


def test_get_engine_is_memoized(clean_env):
    clean_env.setenv("POSTGRES_USER", "u")
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "d")
    first = get_engine()
    second = get_engine()
    assert isinstance(first, Engine)
    assert first is second  # single shared engine / pool


def testget_sessionmaker_uses_given_engine(clean_env):
    clean_env.setenv("POSTGRES_USER", "u")
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "d")
    engine = get_engine()
    factory = get_sessionmaker(engine)
    assert isinstance(factory, sessionmaker)
    assert factory.kw["bind"] is engine
    assert factory.kw["expire_on_commit"] is False


def testget_sessionmaker_falls_back_to_shared_engine(clean_env):
    clean_env.setenv("POSTGRES_USER", "u")
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "d")
    factory = get_sessionmaker()
    assert factory.kw["bind"] is get_engine()


def test_sessionmaker_produces_sessions(clean_env):
    clean_env.setenv("POSTGRES_USER", "u")
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "d")
    factory = get_sessionmaker(get_engine())
    session = factory()
    try:
        assert isinstance(session, Session)
    finally:
        session.close()
