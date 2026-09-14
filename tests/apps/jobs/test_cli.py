"""Tests pour jobs.cli.build_parser — dispatch et déclaration des args par job.

On ne lance jamais les jobs ici (parse_args ne fait que fixer les defaults) :
on vérifie que chaque job enregistré est exposé en sous-commande, câblé sur son
`run`, et que ses arguments sont bien déclarés.
"""

import os

import pytest

from jobs.cli import build_parser
from jobs.registry import get_jobs


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


def test_unknown_env_is_rejected_by_the_parser():
    """`choices` refuse un env inconnu dès le parsing, avant tout accès DB."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--env", "staging", "generate_h3_grid", "--aoi", "idf"])


def test_main_exports_the_env_before_dispatching(monkeypatch):
    """`main` pose BIOWATCH_ENV avant d'appeler le job.

    C'est l'invariant critique : `get_engine` est mis en cache dès le premier
    appel, donc l'env doit être fixé avant que le job ne touche la DB.
    """
    from core import ENV_VAR

    import jobs.cli as cli

    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(cli, "setup_logging", lambda **_: None)
    monkeypatch.setattr(
        "sys.argv", ["biowatch-jobs", "--env", "prod", "generate_h3_grid", "--aoi", "idf"]
    )

    seen: dict[str, str | None] = {}

    def fake_run(_args):
        # L'env doit déjà être posé au moment où le job démarre.
        seen["env"] = os.environ.get(ENV_VAR)

    monkeypatch.setattr(cli, "build_parser", _parser_returning(fake_run))
    cli.main()

    assert seen["env"] == "prod"


def _parser_returning(run):
    """Construit un parser réel dont le job pointe sur `run`."""
    original = build_parser

    def factory():
        parser = original()
        for action in parser._subparsers._group_actions:  # type: ignore[union-attr]
            for sub in action.choices.values():
                sub.set_defaults(_run=run)
        return parser

    return factory
