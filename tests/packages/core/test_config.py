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
    ENV_VAR,
    EnvFileNotFoundError,
    UnknownEnvError,
    data_root,
    env_file,
    expected_db_suffix,
    load_env_file,
    normalize_env,
    resolve_env,
)


def test_default_is_dev_when_nothing_is_set(monkeypatch):
    """Aucun flag, aucune variable ⇒ dev. C'est le garde-fou principal."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert resolve_env() == "dev"


def test_env_var_is_used_when_no_flag(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "prod")
    assert resolve_env() == "prod"


def test_cli_flag_overrides_env_var(monkeypatch):
    """Le flag explicite gagne : c'est lui qui tranche pour un run manuel."""
    monkeypatch.setenv(ENV_VAR, "prod")
    assert resolve_env("dev") == "dev"


@pytest.mark.parametrize("raw", ["", " ", "\t\n"])
def test_blank_env_var_falls_back_to_default(monkeypatch, raw):
    """BIOWATCH_ENV vide ou blanc (export d'une variable non définie) ⇒ dev.

    Régression : "" retombait sur dev mais " " était rejeté comme env inconnu,
    parce que le strip arrivait après le choix du défaut.
    """
    monkeypatch.setenv(ENV_VAR, raw)
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


@pytest.mark.parametrize("raw, expected", [("PROD", "prod"), (" dev ", "dev"), ("Prod\n", "prod")])
def test_normalize_env_is_the_single_normalization_rule(raw, expected):
    """Partagée par `resolve_env` et le `type=` argparse du CLI."""
    assert normalize_env(raw) == expected


def test_load_env_file_pins_the_process_env_before_reading_the_file(monkeypatch, project_root):
    """`BIOWATCH_ENV=prod` collé dans `.env.dev` ne fait pas basculer le process en prod.

    Hors CLI (API, librairie), rien d'autre ne pose BIOWATCH_ENV : c'est
    `load_env_file` qui fige l'env du process, avant de lire le fichier.
    """
    monkeypatch.delenv(ENV_VAR, raising=False)
    (project_root / ".env.dev").write_text("BIOWATCH_ENV=prod\n")

    load_env_file("dev")

    assert os.environ[ENV_VAR] == "dev"
    assert resolve_env() == "dev"


@pytest.mark.skipif(os.geteuid() == 0, reason="root lit tout, le test n'a pas de sens")
@pytest.mark.parametrize("env", ["dev", "prod"])
def test_load_env_file_unreadable_file_is_a_config_error(project_root, env):
    """Fichier présent mais illisible (ex. `.env.prod` en 600 root sur le VPS)."""
    path = project_root / env_file(env)
    path.write_text("")
    path.chmod(0)
    try:
        with pytest.raises(EnvFileNotFoundError, match="illisible"):
            load_env_file(env)
    finally:
        path.chmod(0o600)


def test_env_file_follows_the_dot_env_env_convention():
    """Convention `.env.<env>` : c'est ce nom que `load_env_file` cherche et que prod exige."""
    assert env_file("dev") == ".env.dev"
    assert env_file("prod") == ".env.prod"


def test_expected_db_suffix_is_the_env_name():
    """Convention `<base>_<env>` : `biowatch_dev` / `biowatch_prod`, contrôlée par `clients.db`."""
    assert expected_db_suffix("dev") == "_dev"
    assert expected_db_suffix("prod") == "_prod"


def test_load_env_file_prod_without_file_raises(monkeypatch, project_root):
    """Pas de `.env.prod` ⇒ refus explicite, nommant l'env et le fichier.

    C'est le garde-fou structurel : il ne dépend d'aucune variable de connexion,
    donc un `DATABASE_URL` exporté dans le shell ne le contourne pas.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@prod.internal:5432/biowatch_prod")
    with pytest.raises(EnvFileNotFoundError, match=r"\.env\.prod.*'prod'"):
        load_env_file("prod")


def test_load_env_file_ignores_the_cwd_and_parent_directories(
    monkeypatch, project_root, tmp_path_factory
):
    """Un `.env.prod` dans le cwd (ou un parent) ne satisfait PAS le garde-fou.

    Seule la racine du dépôt compte : un `~/.env.prod` qui traîne sur un
    laptop ne doit pas transformer ce laptop en VPS.
    """
    elsewhere = tmp_path_factory.mktemp("cwd")
    (elsewhere / ".env.prod").write_text("")
    monkeypatch.chdir(elsewhere)

    with pytest.raises(EnvFileNotFoundError):
        load_env_file("prod")


def test_load_env_file_dev_without_file_returns_none(project_root):
    """En dev, l'absence du fichier n'est pas une erreur (CI, variables du shell)."""
    assert load_env_file("dev") is None


def test_load_env_file_is_found_from_any_cwd(monkeypatch, project_root, tmp_path_factory):
    """Lancer depuis `~` (ou un timer sans WorkingDirectory) trouve quand même la config."""
    (project_root / ".env.dev").write_text("")
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))

    assert load_env_file("dev") == str(project_root / ".env.dev")


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_load_env_file_loads_the_file_when_present(monkeypatch, project_root, env):
    monkeypatch.delenv("BIOWATCH_TEST_MARKER", raising=False)
    (project_root / env_file(env)).write_text("BIOWATCH_TEST_MARKER=loaded\n")

    path = load_env_file(env)

    assert path == str(project_root / env_file(env))
    assert os.environ["BIOWATCH_TEST_MARKER"] == "loaded"


def test_load_env_file_never_overrides_existing_variables(monkeypatch, project_root):
    """`override=False` : une variable déjà posée (shell, CI, systemd) gagne sur le fichier."""
    monkeypatch.setenv("BIOWATCH_TEST_MARKER", "from-shell")
    (project_root / ".env.dev").write_text("BIOWATCH_TEST_MARKER=from-file\n")

    load_env_file("dev")

    assert os.environ["BIOWATCH_TEST_MARKER"] == "from-shell"


def test_data_root_separates_environments(monkeypatch, project_root):
    """Les deux environnements n'écrivent jamais dans le même répertoire."""
    monkeypatch.delenv(DATA_ROOT_VAR, raising=False)
    assert data_root("dev") == project_root / "data" / "dev"
    assert data_root("prod") == project_root / "data" / "prod"
    assert data_root("dev") != data_root("prod")


def test_data_root_is_anchored_on_the_project_not_the_cwd(
    monkeypatch, project_root, tmp_path_factory
):
    """Un job lancé depuis `~` n'écrit pas `~/data/dev/`."""
    monkeypatch.delenv(DATA_ROOT_VAR, raising=False)
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    assert data_root("dev") == project_root / "data" / "dev"


def test_relative_data_root_override_is_anchored_on_the_project(
    monkeypatch, project_root, tmp_path_factory
):
    """`BIOWATCH_DATA_ROOT=data` (valeur de l'exemple) ne doit pas dépendre du cwd."""
    monkeypatch.setenv(DATA_ROOT_VAR, "data")
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    assert data_root("dev") == project_root / "data" / "dev"


def test_project_root_is_the_repository_root():
    """Sanity : la racine calculée porte bien le pyproject du dépôt."""
    from core.config import PROJECT_ROOT

    assert (PROJECT_ROOT / "pyproject.toml").is_file()
    assert (PROJECT_ROOT / "packages" / "core" / "config.py").is_file()


def test_data_root_is_overridable_and_keeps_env_separation(monkeypatch):
    """Sur le VPS, la racine pointe hors du code déployé (lecture seule)."""
    monkeypatch.setenv(DATA_ROOT_VAR, "/var/lib/biowatch/jobs")
    assert data_root("prod") == Path("/var/lib/biowatch/jobs/prod")
    assert data_root("dev") == Path("/var/lib/biowatch/jobs/dev")


def test_empty_data_root_falls_back_to_default(monkeypatch, project_root):
    monkeypatch.setenv(DATA_ROOT_VAR, "")
    assert data_root("dev") == project_root / "data" / "dev"
