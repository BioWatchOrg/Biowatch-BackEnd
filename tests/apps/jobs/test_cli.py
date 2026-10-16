"""Tests pour jobs.cli — parser, puis `main()` de bout en bout.

Les tests de `build_parser` ne lancent rien (parse_args ne fait que fixer les
defaults). Les tests de `main` observent le comportement réel : variables
posées, niveau de log configuré, events JSON émis sur stdout, code de sortie —
avec un faux job quand le job importe peu, avec les vrais jobs enregistrés
quand c'est le trajet complet (CLI → job → `get_engine` → garde-fous) qui est
en jeu, et jusqu'à une vraie base avec `TEST_DATABASE_URL`.
"""

import json
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


def test_update_iucn_taxonomy_country_defaults_to_fr():
    parser = build_parser()
    assert parser.parse_args(["update_iucn_taxonomy"]).country == "FR"
    assert parser.parse_args(["update_iucn_taxonomy", "--country", "BE"]).country == "BE"


def test_update_iucn_taxonomy_dispatches_to_the_pipeline(monkeypatch):
    import jobs.definitions as definitions

    seen: list[str] = []
    monkeypatch.setattr(definitions, "update_iucn_taxonomy", lambda country: seen.append(country))
    args = build_parser().parse_args(["update_iucn_taxonomy", "--country", "BE"])

    args._run(args)

    assert seen == ["BE"]


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


POSTGRES_VARS = (
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
)


def _fake_job(run):
    """Un job minimal enregistrable, pour piloter `main` sans job métier réel."""
    return {"fake_job": Job(name="fake_job", help="job de test", run=run)}


def _exit_status(exc: SystemExit) -> int:
    """Statut réel du process pour un `SystemExit`, avec la sémantique de l'interpréteur.

    `SystemExit("message")` affiche le message sur stderr et sort en 1 ;
    `exc.code` vaut alors la chaîne, donc `exc.code != 0` ne prouverait rien.
    """
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    return 1


def _json_events(stdout: str) -> list[dict]:
    """Lignes JSON émises par le logging sur stdout (ce que Logdy ingère)."""
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def _no_connection(*_args, **_kwargs):
    raise AssertionError(
        "create_engine ne doit pas être appelé : refus attendu avant toute connexion"
    )


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
def test_prod_without_env_file_fails_before_db_access(
    monkeypatch, project_root, capsys, database_url
):
    """`--env prod` sans `.env.prod` ⇒ sortie propre, le job n'est jamais appelé.

    Scénario clé du ticket : la base prod ne doit pas être atteignable depuis
    un poste local. Le refus ne dépend pas des variables de connexion : même
    avec un `DATABASE_URL` déjà exporté dans le shell, on n'arrive pas au job
    (donc pas au premier `get_engine`). L'échec sort aussi en JSON sur stdout :
    sur le VPS, c'est la seule trace que Logdy verrait.
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
    assert _exit_status(exc.value) == 1
    message = str(exc.value)
    assert ".env.prod" in message
    assert "prod" in message
    events = _json_events(capsys.readouterr().out)
    assert [(e["event"], e["level"]) for e in events] == [("cli.config_error", "WARNING")]


def test_unknown_env_from_variable_exits_cleanly(monkeypatch):
    """`BIOWATCH_ENV=staging` (hors flag, donc hors `choices` d'argparse) ⇒ pas de traceback."""
    monkeypatch.setenv(ENV_VAR, "staging")
    monkeypatch.setattr(cli, "get_jobs", lambda: _fake_job(lambda _args: None))
    monkeypatch.setattr("sys.argv", ["biowatch-jobs", "fake_job"])

    with pytest.raises(SystemExit, match="staging"):
        cli.main()


def test_database_config_error_from_the_job_exits_cleanly(monkeypatch, project_root, capsys):
    """Base mal nommée pour l'env (ex. tunnel prod en dev) ⇒ exit 1, pas de traceback.

    L'erreur ne peut être levée qu'au premier accès DB, donc à l'intérieur du
    job : `main` doit quand même la transformer en message lisible, et la
    logger en WARNING (erreur attendue), après le `cli.start` en INFO.
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

    assert _exit_status(exc.value) == 1
    assert "biowatch_prod" in str(exc.value)
    events = _json_events(capsys.readouterr().out)
    assert [(e["event"], e["level"]) for e in events] == [
        ("cli.start", "INFO"),
        ("cli.config_error", "WARNING"),
    ]
    assert events[0]["context"] == {"env": "dev", "job": "fake_job"}


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


@pytest.mark.parametrize(
    "argv",
    [["init_db"], ["generate_h3_grid", "--aoi", "idf", "--resolution", "5"]],
    ids=["init_db", "generate_h3_grid"],
)
def test_forgotten_prod_tunnel_is_refused_by_a_real_job_before_any_connection(
    monkeypatch, project_root, capsys, argv
):
    """Tunnel prod oublié dans le shell + job RÉEL sans `--env` ⇒ refus avant toute connexion.

    Pas de faux job : on traverse la vraie chaîne CLI → job enregistré →
    `get_engine` → `_build_database_url`. `create_engine` est piégé pour prouver
    qu'aucune connexion n'est même tentée. Aucune base requise.
    """
    from clients.db import session as db_session

    monkeypatch.delenv(ENV_VAR, raising=False)
    for var in POSTGRES_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:55432/biowatch_prod")
    monkeypatch.setattr(db_session, "create_engine", _no_connection)
    db_session.get_engine.cache_clear()
    monkeypatch.setattr("sys.argv", ["biowatch-jobs", *argv])

    try:
        with pytest.raises(SystemExit) as exc:
            cli.main()
    finally:
        db_session.get_engine.cache_clear()

    message = str(exc.value)
    assert _exit_status(exc.value) == 1
    assert "biowatch_prod" in message and "_dev" in message
    assert "u:p@" not in message  # jamais l'URL ni le mot de passe
    events = _json_events(capsys.readouterr().out)
    assert ("cli.config_error", "WARNING") in [(e["event"], e["level"]) for e in events]
    assert not [
        e for e in events if e["level"] == "ERROR"
    ]  # un refus attendu n'est pas une exception


def test_init_db_reaches_the_dev_database_through_the_guards(
    monkeypatch, project_root, capsys, postgis_url
):
    """Chemin nominal bout-en-bout : CLI → `init_db` → `get_engine` → garde-fous → vraie base.

    Le nom de la base de test finit par `_dev` : les deux garde-fous laissent
    passer, le schéma est créé, l'event de succès est émis. Complète les tests
    de refus : les garde-fous ne bloquent pas le cas légitime.
    """
    from sqlalchemy import text

    from clients.db import session as db_session

    monkeypatch.delenv(ENV_VAR, raising=False)
    for var in POSTGRES_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", postgis_url)
    db_session.get_engine.cache_clear()
    monkeypatch.setattr("sys.argv", ["biowatch-jobs", "init_db"])

    try:
        cli.main()
        engine = db_session.get_engine()
        assert (engine.url.database or "").endswith("_dev")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('public.zones_hex') IS NOT NULL"))
        engine.dispose()
    finally:
        db_session.get_engine.cache_clear()

    events = [e["event"] for e in _json_events(capsys.readouterr().out)]
    assert events[0] == "cli.start"
    assert "db.init_db.success" in events
