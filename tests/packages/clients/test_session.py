"""Tests for clients.db.session — URL building, engine memoization, session factory."""

import pytest
from clients.db.session import (
    DatabaseConfigError,
    DatabaseEnvMismatchError,
    _build_database_url,
    get_engine,
    get_sessionmaker,
)
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
    for var in (ENV_VAR, *POSTGRES_VARS):
        monkeypatch.delenv(var, raising=False)
    # A memoized engine from a previous test must not bleed into this one.
    get_engine.cache_clear()
    yield monkeypatch
    get_engine.cache_clear()


def test_database_url_takes_precedence(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql+psycopg://u:p@example:5432/biowatch_dev")
    # POSTGRES_* are intentionally absent — they must be ignored entirely.
    assert _build_database_url() == "postgresql+psycopg://u:p@example:5432/biowatch_dev"


def test_prod_without_env_file_fails_even_with_database_url(monkeypatch, project_root):
    """Garde-fou prod : `.env.prod` absent ⇒ refus AVANT de lire DATABASE_URL.

    Sans ce test, un `DATABASE_URL` résiduel dans le shell suffirait à
    construire une URL vers la prod depuis un poste local. Ici on ne mocke
    pas `load_env_file` : c'est précisément lui qui doit bloquer.
    """
    # `project_root` est vide : aucun .env.prod.
    monkeypatch.setenv(ENV_VAR, "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@prod.internal:5432/biowatch_prod")
    get_engine.cache_clear()

    with pytest.raises(EnvFileNotFoundError, match=r"\.env\.prod"):
        _build_database_url()


def test_prod_with_env_file_reaches_the_configured_url(monkeypatch, project_root):
    """Sur le VPS, `.env.prod` existe : la résolution prod aboutit normalement."""
    (project_root / ".env.prod").write_text("")
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
    clean_env.setenv("POSTGRES_DB", "biowatch_dev")
    # No POSTGRES_HOST / POSTGRES_PORT set.
    assert _build_database_url() == "postgresql+psycopg://u:p@127.0.0.1:5432/biowatch_dev"


@pytest.mark.parametrize(
    "source",
    ["DATABASE_URL", "POSTGRES_DB"],
)
def test_dev_refuses_a_database_not_suffixed_dev(clean_env, source):
    """Dev (le défaut) + base `biowatch_prod` ⇒ refus, quelle que soit l'origine.

    Scénario : un tunnel SSH vers la prod exporte DATABASE_URL (ou POSTGRES_DB)
    dans le shell ; `.env.dev` ne peut pas l'écraser (override=False). Le nom de
    la base est le dernier filet : il doit finir par `_dev`.
    """
    if source == "DATABASE_URL":
        clean_env.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:55432/biowatch_prod")
    else:
        clean_env.setenv("POSTGRES_USER", "u")
        clean_env.setenv("POSTGRES_PASSWORD", "p")
        clean_env.setenv("POSTGRES_DB", "biowatch_prod")

    with pytest.raises(DatabaseEnvMismatchError, match="biowatch_prod.*'dev'.*_dev") as exc:
        _build_database_url()
    assert "u:p@" not in str(exc.value)  # jamais d'URL ni de mot de passe dans l'erreur


def test_prod_refuses_a_database_not_suffixed_prod(monkeypatch, project_root):
    """Symétrique : en prod, une base `_dev` est refusée aussi (config croisée)."""
    (project_root / ".env.prod").write_text("")
    monkeypatch.setenv(ENV_VAR, "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5432/biowatch_dev")
    get_engine.cache_clear()

    with pytest.raises(DatabaseEnvMismatchError, match="'prod'"):
        _build_database_url()


def test_unparsable_database_url_fails_without_leaking_it(clean_env):
    """URL illisible ⇒ erreur de config nommant l'env et le fichier, jamais l'URL.

    Le message SQLAlchemy d'origine cite la chaîne complète (donc le mot de
    passe) : il ne doit apparaître ni dans le message, ni dans la chaîne
    d'exceptions (`from None`).
    """
    clean_env.setenv("DATABASE_URL", "postgresql+psycopg//u:s3cret@h/biowatch_dev")  # ':' manquant

    with pytest.raises(DatabaseConfigError, match=r"'dev'.*\.env\.dev") as exc:
        _build_database_url()
    assert "s3cret" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_missing_required_var_raises_a_config_error_naming_var_env_and_file(clean_env):
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "biowatch_dev")
    # POSTGRES_USER missing → erreur de config lisible : variable, env, fichier attendu.
    with pytest.raises(DatabaseConfigError, match=r"POSTGRES_USER.*'dev'.*\.env\.dev"):
        _build_database_url()


def test_dbname_query_parameter_cannot_bypass_the_suffix_check(clean_env):
    """`?dbname=` (libpq) prime sur le chemin de l'URL : c'est ce nom-là qui est contrôlé."""
    clean_env.setenv(
        "DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5432/biowatch_dev?dbname=biowatch_prod"
    )
    with pytest.raises(DatabaseEnvMismatchError, match="biowatch_prod"):
        _build_database_url()


def test_get_engine_is_memoized(clean_env):
    clean_env.setenv("POSTGRES_USER", "u")
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "biowatch_dev")
    first = get_engine()
    second = get_engine()
    assert isinstance(first, Engine)
    assert first is second  # single shared engine / pool


def test_get_sessionmaker_uses_given_engine(clean_env):
    clean_env.setenv("POSTGRES_USER", "u")
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "biowatch_dev")
    engine = get_engine()
    factory = get_sessionmaker(engine)
    assert isinstance(factory, sessionmaker)
    assert factory.kw["bind"] is engine
    assert factory.kw["expire_on_commit"] is False


def test_get_sessionmaker_falls_back_to_shared_engine(clean_env):
    clean_env.setenv("POSTGRES_USER", "u")
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "biowatch_dev")
    factory = get_sessionmaker()
    assert factory.kw["bind"] is get_engine()


def test_sessionmaker_produces_sessions(clean_env):
    clean_env.setenv("POSTGRES_USER", "u")
    clean_env.setenv("POSTGRES_PASSWORD", "p")
    clean_env.setenv("POSTGRES_DB", "biowatch_dev")
    factory = get_sessionmaker(get_engine())
    session = factory()
    try:
        assert isinstance(session, Session)
    finally:
        session.close()
