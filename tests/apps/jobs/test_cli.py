"""Tests pour jobs.cli.build_parser — dispatch et déclaration des args par job.

On ne lance jamais les jobs ici (parse_args ne fait que fixer les defaults) :
on vérifie que chaque job enregistré est exposé en sous-commande, câblé sur son
`run`, et que ses arguments sont bien déclarés.
"""

import logging
import os

import pytest
from core import ENV_VAR

import jobs.cli as cli
from jobs.cli import build_parser
from jobs.registry import Job, get_jobs


def test_every_registered_job_is_a_subcommand():
    """Chaque job du registre est exposé et parse au moins avec `--help`.

    On teste le comportement public (parse_args) plutôt que d'introspecter les
    internes d'argparse : `--help` sort en SystemExit(0) si la sous-commande
    existe, sinon argparse sort en SystemExit(2) avant d'atteindre `--help`.
    """
    parser = build_parser()
    for name in get_jobs():
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([name, "--help"])
        assert exc.value.code == 0


def test_each_job_dispatches_to_its_run():
    """Chaque sous-commande fixe `_run` sur le callable du job enregistré."""
    parser = build_parser()
    for name, job in get_jobs().items():
        # On donne des args factices valides selon le job connu.
        argv = [name]
        if name == "generate_h3_grid":
            argv += ["--aoi", "paris", "--resolution", "8"]
        args = parser.parse_args(argv)
        assert args._run is job.run


def test_generate_h3_grid_parses_args():
    parser = build_parser()
    args = parser.parse_args(["generate_h3_grid", "--aoi", "paris", "--resolution", "8"])
    assert args.job == "generate_h3_grid"
    assert args.aoi == "paris"
    assert args.resolution == 8
    assert isinstance(args.resolution, int)


def test_generate_h3_grid_requires_aoi():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["generate_h3_grid"])


def test_generate_h3_grid_resolution_is_optional_defaults_to_none():
    """--resolution omis ⇒ None : generate_h3_grid retombera sur default_res."""
    parser = build_parser()
    args = parser.parse_args(["generate_h3_grid", "--aoi", "idf"])
    assert args.resolution is None


def test_generate_h3_grid_resolution_must_be_int():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["generate_h3_grid", "--aoi", "paris", "--resolution", "huit"])


def test_no_job_is_rejected():
    """`required=True` sur le sous-parser : appeler sans job échoue."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_unknown_job_is_rejected():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["does_not_exist"])


def test_env_flag_defaults_to_none_so_resolution_can_apply():
    """Sans `--env`, le parser laisse None : c'est `resolve_env` qui tranche."""
    parser = build_parser()
    args = parser.parse_args(["generate_h3_grid", "--aoi", "idf"])
    assert args.env is None


@pytest.mark.parametrize("value", ["dev", "prod"])
def test_env_flag_is_parsed(value):
    parser = build_parser()
    args = parser.parse_args(["--env", value, "generate_h3_grid", "--aoi", "idf"])
    assert args.env == value


@pytest.mark.parametrize("raw", ["PROD", " prod ", "Prod"])
def test_env_flag_is_normalized_like_the_variable(raw):
    """`--env PROD` et `BIOWATCH_ENV=PROD` doivent se comporter pareil."""
    parser = build_parser()
    args = parser.parse_args(["--env", raw, "generate_h3_grid", "--aoi", "idf"])
    assert args.env == "prod"


def test_unknown_env_is_rejected_by_the_parser():
    """`choices` refuse un env inconnu dès le parsing, avant tout accès DB."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--env", "staging", "generate_h3_grid", "--aoi", "idf"])


def _fake_job(run):
    """Un job minimal enregistrable, pour piloter `main` sans job métier réel."""
    return {"fake_job": Job(name="fake_job", help="job de test", run=run)}


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """`main()` configure réellement le logging : on rend le root logger intact ensuite."""
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def test_main_exports_the_env_before_dispatching(monkeypatch, project_root):
    """`main` pose BIOWATCH_ENV avant d'appeler le job.

    Invariant critique : `get_engine` est mis en cache dès le premier appel,
    donc l'env doit être fixé avant que le job ne touche la DB.
    """
    seen: dict[str, str | None] = {}

    def fake_run(_args):
        seen["env"] = os.environ.get(ENV_VAR)

    # Prod exige son fichier : on simule le VPS, où `.env.prod` existe.
    (project_root / ".env.prod").write_text("")
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(cli, "get_jobs", lambda: _fake_job(fake_run))
    monkeypatch.setattr("sys.argv", ["biowatch-jobs", "--env", "prod", "fake_job"])

    cli.main()

    assert seen["env"] == "prod"


def test_log_level_from_env_file_is_applied_to_the_jobs(monkeypatch, project_root):
    """`LOG_LEVEL=WARNING` dans `.env.<env>` ⇒ le root logger tourne en WARNING.

    Régression : le dotenv n'était chargé qu'au premier accès DB, donc après la
    configuration du logging — `LOG_LEVEL` n'avait alors aucun effet sur les
    jobs, alors que `.env.dev.example` le documente comme un réglage actif. On
    observe le niveau réellement configuré, pas l'ordre des appels dans `main`.
    """
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    (project_root / ".env.dev").write_text("LOG_LEVEL=WARNING\n")
    monkeypatch.setattr(cli, "get_jobs", lambda: _fake_job(lambda _args: None))
    monkeypatch.setattr("sys.argv", ["biowatch-jobs", "fake_job"])

    cli.main()

    assert logging.getLogger().level == logging.WARNING


def test_debug_flag_wins_over_the_env_file_level(monkeypatch, project_root):
    """`--debug` force DEBUG même si `.env.dev` dit WARNING (réglage ponctuel de dev)."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    (project_root / ".env.dev").write_text("LOG_LEVEL=WARNING\n")
    monkeypatch.setattr(cli, "get_jobs", lambda: _fake_job(lambda _args: None))
    monkeypatch.setattr("sys.argv", ["biowatch-jobs", "--debug", "fake_job"])

    cli.main()

    assert logging.getLogger().level == logging.DEBUG


@pytest.mark.parametrize(
    "database_url",
    [None, "postgresql+psycopg://u:p@prod.internal:5432/biowatch_prod"],
    ids=["no-connection-var", "DATABASE_URL-exported"],
)
def test_prod_without_env_file_fails_before_db_access(monkeypatch, project_root, database_url):
    """`--env prod` sans `.env.prod` ⇒ sortie propre, le job n'est jamais appelé.

    Scénario clé du ticket : la base prod ne doit pas être atteignable depuis
    un poste local. Le refus ne dépend pas des variables de connexion : même
    avec un `DATABASE_URL` déjà exporté dans le shell, on n'arrive pas au job
    (donc pas au premier `get_engine`).
    """
    # `project_root` est vide : aucun .env.prod.
    monkeypatch.delenv(ENV_VAR, raising=False)
    if database_url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", database_url)

    job_called = False

    def fake_run(_args):
        nonlocal job_called
        job_called = True

    monkeypatch.setattr(cli, "get_jobs", lambda: _fake_job(fake_run))
    monkeypatch.setattr("sys.argv", ["biowatch-jobs", "--env", "prod", "fake_job"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert job_called is False
    message = str(exc.value)
    assert ".env.prod" in message
    assert "prod" in message


def test_unknown_env_from_variable_exits_cleanly(monkeypatch):
    """`BIOWATCH_ENV=staging` (hors flag, donc hors `choices` d'argparse) ⇒ pas de traceback."""
    monkeypatch.setenv(ENV_VAR, "staging")
    monkeypatch.setattr(cli, "get_jobs", lambda: _fake_job(lambda _args: None))
    monkeypatch.setattr("sys.argv", ["biowatch-jobs", "fake_job"])

    with pytest.raises(SystemExit, match="staging"):
        cli.main()


def test_database_config_error_from_the_job_exits_cleanly(monkeypatch, project_root):
    """Base mal nommée pour l'env (ex. tunnel prod en dev) ⇒ exit 1, pas de traceback.

    L'erreur ne peut être levée qu'au premier accès DB, donc à l'intérieur du
    job : `main` doit quand même la transformer en message lisible.
    """
    from clients import DatabaseEnvMismatchError

    def fake_run(_args):
        raise DatabaseEnvMismatchError(
            "Base 'biowatch_prod' incohérente avec l'environnement 'dev'"
        )

    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(cli, "get_jobs", lambda: _fake_job(fake_run))
    monkeypatch.setattr("sys.argv", ["biowatch-jobs", "fake_job"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code != 0
    assert "biowatch_prod" in str(exc.value)


def test_invalid_log_level_from_env_file_exits_cleanly(monkeypatch, project_root):
    """`LOG_LEVEL=verbose` dans `.env.dev` ⇒ erreur de config lisible, pas de ValueError brut.

    Depuis que le dotenv est chargé avant `setup_logging`, cette valeur est
    réellement appliquée : une faute de frappe doit être traitée comme les
    autres erreurs de configuration.
    """
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    (project_root / ".env.dev").write_text("LOG_LEVEL=verbose\n")
    monkeypatch.setattr(cli, "get_jobs", lambda: _fake_job(lambda _args: None))
    monkeypatch.setattr("sys.argv", ["biowatch-jobs", "fake_job"])

    with pytest.raises(SystemExit, match="LOG_LEVEL"):
        cli.main()
