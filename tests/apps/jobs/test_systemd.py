"""Tests de la fréquence de lancement des jobs et du manifeste systemd (#27).

Le manifeste est le seul pont entre le registre et le VPS : ce qu'il émet
devient des timers. On vérifie donc qu'il n'émet que des jobs planifiés, dans un
format stable, et qu'aucune fréquence déclarée n'échoue au moment du déploiement
(`systemd-analyze calendar`, disponible sur les runners Ubuntu de la CI).
"""

import shutil
import subprocess

import pytest

import jobs.systemd as jobs_systemd
from jobs.registry import InvalidJobError, Job, get_jobs
from jobs.systemd import timer_manifest


def _noop(_args):
    return None


def test_schedule_defaults_to_none():
    assert Job(name="manual_job", help="", run=_noop).schedule is None


@pytest.mark.parametrize(
    "schedule",
    [
        "*-*-* 02:30:00 Europe/Paris",
        "*-01-15 03:00:00 Europe/Paris",
        "Mon..Fri 04:00",
        "daily",
        "*-*~01 05:00",
        "*-*-* 01:00:00 America/New_York",
    ],
)
def test_valid_schedules_are_accepted(schedule):
    assert Job(name="planned", help="", run=_noop, schedule=schedule).schedule == schedule


@pytest.mark.parametrize(
    "schedule",
    [
        "",
        "daily\nExecStart=/bin/sh",  # injection d'une directive dans l'unité générée
        "daily; rm -rf /",
        "%h",  # spécificateur systemd
        "x" * 101,
    ],
)
def test_invalid_schedules_are_rejected(schedule):
    with pytest.raises(InvalidJobError, match="Fréquence invalide"):
        Job(name="planned", help="", run=_noop, schedule=schedule)


@pytest.mark.parametrize("name", ["", "Init_db", "1job", "job-name", "job@x", "a" * 64])
def test_invalid_job_names_are_rejected(name):
    with pytest.raises(InvalidJobError, match="Nom de job invalide"):
        Job(name=name, help="", run=_noop)


def test_manifest_lists_only_scheduled_jobs_sorted_by_name():
    jobs = {
        "zeta": Job(name="zeta", help="", run=_noop, schedule="daily"),
        "manual": Job(name="manual", help="", run=_noop),
        "alpha": Job(name="alpha", help="", run=_noop, schedule="*-*-* 02:30:00 Europe/Paris"),
    }
    assert timer_manifest(jobs) == "alpha\t*-*-* 02:30:00 Europe/Paris\nzeta\tdaily\n"


def test_manifest_is_empty_without_scheduled_job():
    assert timer_manifest({"manual": Job(name="manual", help="", run=_noop)}) == ""


def test_main_writes_the_registry_manifest_on_stdout(capsys):
    jobs_systemd.main()
    assert capsys.readouterr().out == timer_manifest(get_jobs())


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="requires systemd")
def test_every_registered_schedule_is_a_valid_systemd_calendar():
    """Même validation que `biowatch-install-timers` sur le VPS, mais avant le merge."""
    for job in get_jobs().values():
        if job.schedule is None:
            continue
        result = subprocess.run(
            ["systemd-analyze", "calendar", job.schedule], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{job.name}: {result.stderr.strip()}"
