"""Tests pour core.config — résolution de l'environnement d'exécution.

Aucun accès DB. On vérifie la précédence (flag > variable > défaut), la
normalisation, le rejet des valeurs inconnues, et le garde-fou prod de
`load_env_file` (le seul point qui touche le disque, via `tmp_path`).
"""

import os
from pathlib import Path

import pytest

from core.config import (
    DATA_ROOT_VAR,
    DEFAULT_ENV,
    ENV_FILE_REQUIRED,
    ENV_VAR,
    EnvFileNotFoundError,
    UnknownEnvError,
    data_root,
    env_file,
    load_env_file,
    resolve_env,
)


def test_default_is_dev_when_nothing_is_set(monkeypatch):
    """Aucun flag, aucune variable ⇒ dev. C'est le garde-fou principal."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert resolve_env() == "dev"
    assert DEFAULT_ENV == "dev"


def test_env_var_is_used_when_no_flag(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "prod")
    assert resolve_env() == "prod"


def test_cli_flag_overrides_env_var(monkeypatch):
    """Le flag explicite gagne : c'est lui qui tranche pour un run manuel."""
    monkeypatch.setenv(ENV_VAR, "prod")
    assert resolve_env("dev") == "dev"


def test_empty_env_var_falls_back_to_default(monkeypatch):
    """BIOWATCH_ENV="" (variable exportée vide) ne doit pas casser."""
    monkeypatch.setenv(ENV_VAR, "")
    assert resolve_env() == "dev"


@pytest.mark.parametrize("raw", ["PROD", " prod ", "Prod\n"])
def test_value_is_normalized(monkeypatch, raw):
    """Casse et espaces parasites ne doivent pas faire retomber sur le défaut."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert resolve_env(raw) == "prod"


@pytest.mark.parametrize("raw", ["staging", "production", "dev2", "préprod"])
def test_unknown_env_raises(monkeypatch, raw):
    """Un env inconnu échoue bruyamment plutôt que de retomber sur dev."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(UnknownEnvError):
        resolve_env(raw)


def test_unknown_env_from_variable_also_raises(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "staging")
    with pytest.raises(UnknownEnvError):
        resolve_env()


def test_env_file_names():
    assert env_file("dev") == ".env.dev"
    assert env_file("prod") == ".env.prod"


def test_prod_is_the_env_that_requires_its_file():
    """Le garde-fou ne vise que prod : dev peut tourner sur des variables injectées."""
    assert ENV_FILE_REQUIRED == {"prod"}


def test_load_env_file_prod_without_file_raises(monkeypatch, tmp_path):
    """Pas de `.env.prod` ⇒ refus explicite, nommant l'env et le fichier.

    C'est le garde-fou structurel : il ne dépend d'aucune variable de connexion,
    donc un `DATABASE_URL` exporté dans le shell ne le contourne pas.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@prod.internal:5432/biowatch_prod")
    with pytest.raises(EnvFileNotFoundError, match=r"\.env\.prod.*'prod'"):
        load_env_file("prod")


def test_load_env_file_dev_without_file_returns_none(monkeypatch, tmp_path):
    """En dev, l'absence du fichier n'est pas une erreur (CI, variables du shell)."""
    monkeypatch.chdir(tmp_path)
    assert load_env_file("dev") is None


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_load_env_file_loads_the_file_when_present(monkeypatch, tmp_path, env):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BIOWATCH_TEST_MARKER", raising=False)
    (tmp_path / env_file(env)).write_text("BIOWATCH_TEST_MARKER=loaded\n")

    path = load_env_file(env)

    assert path == str(tmp_path / env_file(env))
    assert os.environ["BIOWATCH_TEST_MARKER"] == "loaded"
    monkeypatch.delenv("BIOWATCH_TEST_MARKER")


def test_load_env_file_never_overrides_existing_variables(monkeypatch, tmp_path):
    """`override=False` : une variable déjà posée (shell, CI) gagne sur le fichier."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIOWATCH_TEST_MARKER", "from-shell")
    (tmp_path / ".env.dev").write_text("BIOWATCH_TEST_MARKER=from-file\n")

    load_env_file("dev")

    assert os.environ["BIOWATCH_TEST_MARKER"] == "from-shell"


def test_data_root_separates_environments(monkeypatch):
    """Les deux environnements n'écrivent jamais dans le même répertoire."""
    monkeypatch.delenv(DATA_ROOT_VAR, raising=False)
    assert data_root("dev") == Path("data/dev")
    assert data_root("prod") == Path("data/prod")
    assert data_root("dev") != data_root("prod")


def test_data_root_is_overridable_and_keeps_env_separation(monkeypatch):
    """Sur le VPS, la racine pointe hors du code déployé (lecture seule)."""
    monkeypatch.setenv(DATA_ROOT_VAR, "/var/lib/biowatch/jobs")
    assert data_root("prod") == Path("/var/lib/biowatch/jobs/prod")
    assert data_root("dev") == Path("/var/lib/biowatch/jobs/dev")


def test_empty_data_root_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(DATA_ROOT_VAR, "")
    assert data_root("dev") == Path("data/dev")
