"""Tests for clients.db.session — URL building, engine memoization, session factory."""

import pytest
from clients.db.session import _build_database_url, get_engine, get_sessionmaker
from core import ENV_VAR, EnvFileNotFoundError
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
    # Prevent the real .env.<env> from leaking into these tests.
    monkeypatch.setattr("clients.db.session.load_env_file", lambda *a, **k: None)
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


def test_prod_without_env_file_fails_even_with_database_url(monkeypatch, tmp_path):
    """Garde-fou prod : `.env.prod` absent ⇒ refus AVANT de lire DATABASE_URL.

    Sans ce test, un `DATABASE_URL` résiduel dans le shell suffirait à
    construire une URL vers la prod depuis un poste local. Ici on ne mocke
    pas `load_env_file` : c'est précisément lui qui doit bloquer.
    """
    monkeypatch.chdir(tmp_path)  # aucun .env.prod ici
    monkeypatch.setenv(ENV_VAR, "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@prod.internal:5432/biowatch_prod")
    get_engine.cache_clear()

    with pytest.raises(EnvFileNotFoundError, match=r"\.env\.prod"):
        _build_database_url()


def test_prod_with_env_file_reaches_the_configured_url(monkeypatch, tmp_path):
    """Sur le VPS, `.env.prod` existe : la résolution prod aboutit normalement."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.prod").write_text("")
    monkeypatch.setenv(ENV_VAR, "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@prod.internal:5432/biowatch_prod")
    get_engine.cache_clear()

    assert _build_database_url() == "postgresql+psycopg://u:p@prod.internal:5432/biowatch_prod"


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
    # POSTGRES_USER missing → explicit, readable failure naming the variable.
    with pytest.raises(RuntimeError, match="POSTGRES_USER"):
        _build_database_url()


def test_get_engine_is_memoized(clean_env):
    clean_env.setenv("POSTGRES_USER", "u")
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "d")
    first = get_engine()
    second = get_engine()
    assert isinstance(first, Engine)
    assert first is second  # single shared engine / pool


def test_get_sessionmaker_uses_given_engine(clean_env):
    clean_env.setenv("POSTGRES_USER", "u")
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "d")
    engine = get_engine()
    factory = get_sessionmaker(engine)
    assert isinstance(factory, sessionmaker)
    assert factory.kw["bind"] is engine
    assert factory.kw["expire_on_commit"] is False


def test_get_sessionmaker_falls_back_to_shared_engine(clean_env):
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
