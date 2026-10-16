"""Tests pour core.config — résolution de l'environnement d'exécution.

Logique pure : aucun accès disque ni DB. On vérifie la précédence
(flag > variable > défaut), la normalisation, et le rejet des valeurs inconnues.
"""

from pathlib import Path

import pytest

from core.config import (
    DATA_ROOT_VAR,
    DEFAULT_ENV,
    ENV_VAR,
    UnknownEnvError,
    data_root,
    env_file,
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
