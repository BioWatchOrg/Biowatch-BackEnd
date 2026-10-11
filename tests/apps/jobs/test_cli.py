"""Tests pour jobs.cli.build_parser — dispatch et déclaration des args par job.

On ne lance jamais les jobs ici (parse_args ne fait que fixer les defaults) :
on vérifie que chaque job enregistré est exposé en sous-commande, câblé sur son
`run`, et que ses arguments sont bien déclarés.
"""

import pytest

from jobs.cli import build_parser
from jobs.registry import get_jobs

# Args factices valides pour les jobs qui ont des arguments obligatoires.
REQUIRED_ARGS = {
    "generate_h3_grid": ["--aoi", "paris", "--resolution", "8"],
    "osm_import_snapshot": ["--aoi", "idf", "--bucket", "2016"],
}


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
        argv = [name, *REQUIRED_ARGS.get(name, [])]
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


def test_osm_import_snapshot_parses_args():
    parser = build_parser()
    args = parser.parse_args(
        [
            "osm_import_snapshot",
            "--aoi",
            "idf",
            "--bucket",
            "2016",
            "--link",
            "https://x/a.osm.pbf",
            "--link",
            "https://x/b.osm.pbf",
            "--env",
            "prod",
        ]
    )
    assert (args.aoi, args.bucket, args.env) == ("idf", "2016", "prod")
    assert args.link == ["https://x/a.osm.pbf", "https://x/b.osm.pbf"]


def test_osm_import_snapshot_defaults_to_dev_without_links():
    args = build_parser().parse_args(["osm_import_snapshot", "--aoi", "idf", "--bucket", "2016"])
    assert args.env == "dev"
    assert args.link is None


def test_osm_import_snapshot_requires_aoi_and_bucket():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["osm_import_snapshot", "--aoi", "idf"])
    with pytest.raises(SystemExit):
        parser.parse_args(["osm_import_snapshot", "--bucket", "2016"])


def test_osm_import_snapshot_rejects_unknown_env():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["osm_import_snapshot", "--aoi", "idf", "--bucket", "2016", "--env", "staging"]
        )


def test_osm_import_scheduled_defaults():
    args = build_parser().parse_args(["osm_import_scheduled"])
    assert args.env == "dev"
    assert args.bucket is None


def test_osm_jobs_call_their_pipeline(monkeypatch):
    import jobs.definitions as definitions

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        definitions, "osm_import_snapshot", lambda **kw: calls.append(("snapshot", kw))
    )
    monkeypatch.setattr(
        definitions, "osm_import_scheduled", lambda **kw: calls.append(("scheduled", kw))
    )
    parser = build_parser()
    for argv in (
        ["osm_import_snapshot", "--aoi", "idf", "--bucket", "2016"],
        ["osm_import_scheduled", "--env", "prod"],
    ):
        args = parser.parse_args(argv)
        args._run(args)

    assert calls == [
        ("snapshot", {"aoi_label": "idf", "bucket": "2016", "env": "dev", "links": None}),
        ("scheduled", {"env": "prod", "bucket": None}),
    ]
