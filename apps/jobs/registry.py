"""Registre des jobs invocables en CLI.

Chaque job s'enregistre via `register(Job(...))`. Le CLI (`jobs.cli`) construit
un sous-parser argparse par job et dispatch vers son `run`. Ajouter un job =
ajouter une entrée au registre, rien d'autre à toucher côté CLI.

Le registre est aussi la source du déploiement (#27) : la CI lit `schedule`
(via `jobs.systemd`) pour générer les timers systemd du VPS. Ajouter un job
planifié ne demande donc aucune modification du workflow ni du VPS.
"""

import re
from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from dataclasses import dataclass, field

# Le nom devient une sous-commande ET un nom d'unité systemd
# (`biowatch-job@<name>.service`) : on s'en tient à ce qui est sûr des deux côtés.
JOB_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# Caractères d'une expression `OnCalendar=` systemd (`*-*-05 03:00:00 Europe/Paris`,
# `Mon..Fri 02:30`, `daily`). Filtre grossier et volontairement strict : la
# validation complète (`systemd-analyze calendar`) tourne dans les tests et sur
# le VPS, mais rien qui ressemble à une injection dans un fichier d'unité
# (retour à la ligne, `=`, `;`, `%`) ne doit passer, même avant.
SCHEDULE_PATTERN = re.compile(r"^[A-Za-z0-9 :*/,.~+_-]{1,100}$")


class InvalidJobError(ValueError):
    """Nom de job ou fréquence de lancement invalide (bug de déclaration)."""


@dataclass(frozen=True)
class Job:
    """Un job lançable en CLI.

    - `name` : nom de la sous-commande (`uv run biowatch-jobs <name>`).
    - `help` : description affichée dans `--help`.
    - `run` : reçoit les args parsés, appelle la fonction métier (`packages/*`).
    - `configure` : déclare les arguments propres au job sur son sous-parser.
    - `schedule` : fréquence de lancement en prod, au format `OnCalendar=` de
      systemd (`"*-*-* 02:30:00 Europe/Paris"` = nocturne, `"*-01-15 03:00:00
      Europe/Paris"` = annuelle). `None` = pas de planification (job lancé à la
      main). Un job planifié est lancé SANS argument : s'il en a besoin, lui
      donner des défauts ou exposer un job `<nom>_scheduled` dédié.
    """

    name: str
    help: str
    run: Callable[[Namespace], None]
    configure: Callable[[ArgumentParser], None] = field(default=lambda _parser: None)
    schedule: str | None = None

    def __post_init__(self) -> None:
        if not JOB_NAME_PATTERN.fullmatch(self.name):
            raise InvalidJobError(
                f"Nom de job invalide : {self.name!r} (attendu : {JOB_NAME_PATTERN.pattern})."
            )
        if self.schedule is not None and not SCHEDULE_PATTERN.fullmatch(self.schedule):
            raise InvalidJobError(
                f"Fréquence invalide pour le job '{self.name}' : {self.schedule!r} "
                f"(attendu : une expression OnCalendar systemd, ex. "
                f"'*-*-* 02:30:00 Europe/Paris')."
            )


_JOBS: dict[str, Job] = {}


def register(job: Job) -> None:
    """Enregistre un job. Lève si le nom est déjà pris (collision = bug)."""
    if job.name in _JOBS:
        raise ValueError(f"Job '{job.name}' est déjà enregistré.")
    _JOBS[job.name] = job


def get_jobs() -> dict[str, Job]:
    """Copie du registre (lecture seule pour le CLI)."""
    return dict(_JOBS)
