"""Tests pour jobs.cli.build_parser — dispatch et déclaration des args par job.

On ne lance jamais les jobs ici (parse_args ne fait que fixer les defaults) :
on vérifie que chaque job enregistré est exposé en sous-commande, câblé sur son
`run`, et que ses arguments sont bien déclarés.
"""

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
        elif name == "extract_sentinel":
            argv += ["--aoi", "paris", "--bucket_id", "2025-05"]
        elif name == "backfill_sentinel":
            argv += ["--aoi", "paris", "--from", "2016-01", "--to", "2016-03"]
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


def test_extract_sentinel_bucket_defaults_to_previous_month(monkeypatch):
    """Sans --bucket_id, le job traite le mois précédent (run mensuel du timer)."""
    import jobs.definitions as definitions

    seen = {}
    monkeypatch.setattr(definitions, "previous_month_bucket", lambda: "2026-09")
    monkeypatch.setattr(
        definitions, "extract_sentinel_data_by_aoi", lambda **kwargs: seen.update(kwargs)
    )
    args = build_parser().parse_args(["extract_sentinel", "--aoi", "idf"])
    args._run(args)
    assert seen["bucket"] == "2026-09"
    assert seen["io_threads"] == 16


def test_extract_sentinel_explicit_bucket(monkeypatch):
    import jobs.definitions as definitions

    seen = {}
    monkeypatch.setattr(
        definitions, "extract_sentinel_data_by_aoi", lambda **kwargs: seen.update(kwargs)
    )
    args = build_parser().parse_args(
        ["extract_sentinel", "--aoi", "idf", "--bucket_id", "2025-05", "--io_threads", "4"]
    )
    args._run(args)
    assert seen["bucket"] == "2025-05"
    assert seen["io_threads"] == 4


def _backfill_args(*extra):
    return build_parser().parse_args(
        ["backfill_sentinel", "--aoi", "idf", "--from", "2016-01", "--to", "2016-03", *extra]
    )


def test_backfill_sentinel_parses_and_dispatches(monkeypatch):
    import jobs.definitions as definitions

    seen = {}

    def fake_backfill(aoi, first, last, resolution, **kwargs):
        seen.update(aoi=aoi, first=first, last=last, resolution=resolution, **kwargs)
        return []

    monkeypatch.setattr(definitions, "backfill_sentinel", fake_backfill)
    args = _backfill_args("--io_threads", "8")
    args._run(args)
    assert seen == {
        "aoi": "idf",
        "first": "2016-01",
        "last": "2016-03",
        "resolution": 8,
        "source_version": "2.0.0",
        "io_threads": 8,
    }


def test_backfill_sentinel_exits_non_zero_when_months_fail(monkeypatch):
    import jobs.definitions as definitions

    monkeypatch.setattr(definitions, "backfill_sentinel", lambda *a, **k: ["2016-02"])
    args = _backfill_args()
    with pytest.raises(SystemExit, match="2016-02"):
        args._run(args)
