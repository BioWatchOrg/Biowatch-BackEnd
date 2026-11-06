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
            argv += ["--aoi", "paris", "--months", "2016-01"]
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
        definitions,
        "extract_sentinel_data_by_aoi",
        lambda **kwargs: seen.update(kwargs) or "success",
    )
    args = build_parser().parse_args(["extract_sentinel", "--aoi", "idf"])
    args._run(args)
    assert seen["bucket"] == "2026-09"
    assert seen["io_threads"] == 16


def test_extract_sentinel_explicit_bucket(monkeypatch):
    import jobs.definitions as definitions

    seen = {}
    monkeypatch.setattr(
        definitions,
        "extract_sentinel_data_by_aoi",
        lambda **kwargs: seen.update(kwargs) or "success",
    )
    args = build_parser().parse_args(
        ["extract_sentinel", "--aoi", "idf", "--bucket_id", "2025-05", "--io_threads", "4"]
    )
    args._run(args)
    assert seen["bucket"] == "2025-05"
    assert seen["io_threads"] == 4


def test_extract_sentinel_partial_month_exits_non_zero(monkeypatch):
    """Un mois incomplet doit se voir (systemd, shell), pas passer pour réussi."""
    import jobs.definitions as definitions

    monkeypatch.setattr(definitions, "extract_sentinel_data_by_aoi", lambda **kw: "partial")
    args = build_parser().parse_args(["extract_sentinel", "--aoi", "idf", "--bucket_id", "2025-05"])
    with pytest.raises(SystemExit, match="2025-05 incomplet"):
        args._run(args)


@pytest.mark.parametrize("status", ["success", "skipped"])
def test_extract_sentinel_success_or_skip_exits_zero(monkeypatch, status):
    import jobs.definitions as definitions

    monkeypatch.setattr(definitions, "extract_sentinel_data_by_aoi", lambda **kw: status)
    args = build_parser().parse_args(["extract_sentinel", "--aoi", "idf", "--bucket_id", "2025-05"])
    args._run(args)  # ne lève pas


def _backfill_args(*extra):
    return build_parser().parse_args(["backfill_sentinel", "--aoi", "idf", *extra])


def _fake_backfill(monkeypatch, report):
    import jobs.definitions as definitions

    seen = {}

    def fake(aoi, months, resolution, **kwargs):
        seen.update(aoi=aoi, months=months, resolution=resolution, **kwargs)
        return report

    monkeypatch.setattr(definitions, "backfill_sentinel", fake)
    return seen


def test_backfill_sentinel_range(monkeypatch):
    from jobs.sentinel import BackfillReport

    seen = _fake_backfill(monkeypatch, BackfillReport())
    args = _backfill_args("--from", "2016-01", "--to", "2016-03", "--io_threads", "8")
    args._run(args)
    assert seen == {
        "aoi": "idf",
        "months": ["2016-01", "2016-02", "2016-03"],
        "resolution": 8,
        "source_version": "2.0.0",
        "io_threads": 8,
    }


def test_backfill_sentinel_targeted_months(monkeypatch):
    from jobs.sentinel import BackfillReport

    seen = _fake_backfill(monkeypatch, BackfillReport())
    args = _backfill_args("--months", "2016-04, 2019-07")
    args._run(args)
    assert seen["months"] == ["2016-04", "2019-07"]


def test_backfill_sentinel_requires_a_range_or_months(monkeypatch):
    from jobs.sentinel import BackfillReport

    _fake_backfill(monkeypatch, BackfillReport())
    args = _backfill_args("--from", "2016-01")
    with pytest.raises(SystemExit, match="--months"):
        args._run(args)


def test_backfill_sentinel_exits_non_zero_with_retry_command(monkeypatch):
    """Les mois à relancer (échec ET incomplets) sont listés, avec la commande ciblée."""
    from jobs.sentinel import BackfillReport

    _fake_backfill(monkeypatch, BackfillReport(failed=["2019-07"], partial=["2016-02"]))
    args = _backfill_args("--from", "2016-01", "--to", "2019-12")
    with pytest.raises(SystemExit) as exc:
        args._run(args)
    message = str(exc.value)
    assert "incomplets : 2016-02" in message and "échec : 2019-07" in message
    assert "--months 2016-02,2019-07" in message
