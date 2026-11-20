"""Tests de la fréquence de lancement des jobs et du manifeste systemd (#27).

Le manifeste est le seul pont entre le registre et le VPS : ce qu'il émet
devient des timers. On vérifie donc qu'il n'émet que des jobs planifiés, dans un
format stable, et qu'aucune fréquence déclarée n'échoue au moment du déploiement
(`systemd-analyze calendar`, disponible sur les runners Ubuntu de la CI).
"""

import runpy
import shutil
import subprocess
import sys

import pytest

import jobs.systemd as jobs_systemd
import jobs.registry as registry
from jobs.registry import InvalidJobError, Job, get_jobs, register
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


@pytest.mark.parametrize("memory_max", ["2200M", "4G", "512000K"])
def test_valid_memory_max_is_accepted(memory_max):
    job = Job(name="planned", help="", run=_noop, schedule="daily", memory_max=memory_max)
    assert job.memory_max == memory_max


@pytest.mark.parametrize("memory_max", ["", "2200", "2.2G", "0M", "2200M\nUser=root", "infinity"])
def test_invalid_memory_max_is_rejected(memory_max):
    with pytest.raises(InvalidJobError, match="memory_max invalide"):
        Job(name="planned", help="", run=_noop, schedule="daily", memory_max=memory_max)


def test_memory_max_requires_a_schedule():
    """Le plafond voyage avec le timer : sur un job manuel, il serait ignoré en silence."""
    with pytest.raises(InvalidJobError, match="sans schedule"):
        Job(name="manual", help="", run=_noop, memory_max="2200M")


def test_manifest_lists_only_scheduled_jobs_sorted_by_name():
    jobs = {
        "zeta": Job(name="zeta", help="", run=_noop, schedule="daily", memory_max="2200M"),
        "manual": Job(name="manual", help="", run=_noop),
        "alpha": Job(name="alpha", help="", run=_noop, schedule="*-*-* 02:30:00 Europe/Paris"),
    }
    assert timer_manifest(jobs) == ("alpha\t*-*-* 02:30:00 Europe/Paris\t-\nzeta\tdaily\t2200M\n")


def test_manifest_is_empty_without_scheduled_job():
    assert timer_manifest({"manual": Job(name="manual", help="", run=_noop)}) == ""


def test_registering_two_jobs_with_the_same_name_fails(monkeypatch):
    """Deux jobs homonymes produiraient deux timers pour la même unité : bug de déclaration."""
    monkeypatch.setattr(registry, "_JOBS", {})
    register(Job(name="twice", help="", run=_noop))
    with pytest.raises(ValueError, match="déjà enregistré"):
        register(Job(name="twice", help="", run=_noop, schedule="daily"))


def test_running_the_module_prints_the_manifest(capsys, monkeypatch):
    """Le VPS lance `python -m jobs.systemd` : c'est ce point d'entrée qui doit répondre."""
    # Comme `python -m` : le module n'est pas encore importé (sinon runpy avertit).
    monkeypatch.delitem(sys.modules, "jobs.systemd")
    runpy.run_module("jobs.systemd", run_name="__main__")
    assert capsys.readouterr().out == timer_manifest(get_jobs())


def test_main_writes_the_registry_manifest_on_stdout(capsys):
    jobs_systemd.main()
    assert capsys.readouterr().out == timer_manifest(get_jobs())


def test_sentinel_monthly_run_is_scheduled_with_its_measured_memory_cap():
    """Le run mensuel de #99 passe par le registre, plus par une unité écrite à la main."""
    line = next(
        line
        for line in timer_manifest(get_jobs()).splitlines()
        if line.startswith("extract_sentinel_monthly\t")
    )
    assert line == "extract_sentinel_monthly\t*-*-05..10 01:30:00 Europe/Paris\t2200M"


def test_osm_monthly_import_is_scheduled_by_the_registry():
    """L'import mensuel de #35 passe par le registre, plus par une unité écrite à la main."""
    assert "osm_import_scheduled\t*-*-05 03:00:00 Europe/Paris\t-" in (
        timer_manifest(get_jobs()).splitlines()
    )


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
