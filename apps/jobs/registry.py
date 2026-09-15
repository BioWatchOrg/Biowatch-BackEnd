"""Registre des jobs invocables en CLI.

Chaque job s'enregistre via `register(Job(...))`. Le CLI (`jobs.cli`) construit
un sous-parser argparse par job et dispatch vers son `run`. Ajouter un job =
ajouter une entrée au registre, rien d'autre à toucher côté CLI.
"""

from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Job:
    """Un job lançable en CLI.

    - `name` : nom de la sous-commande (`uv run biowatch-jobs <name>`).
    - `help` : description affichée dans `--help`.
    - `run` : reçoit les args parsés, appelle la fonction métier (`packages/*`).
    - `configure` : déclare les arguments propres au job sur son sous-parser.
    """

    name: str
    help: str
    run: Callable[[Namespace], None]
    configure: Callable[[ArgumentParser], None] = field(default=lambda _parser: None)


_JOBS: dict[str, Job] = {}


def register(job: Job) -> None:
    """Enregistre un job. Lève si le nom est déjà pris (collision = bug)."""
    if job.name in _JOBS:
        raise ValueError(f"Job '{job.name}' est déjà enregistré.")
    _JOBS[job.name] = job


def get_jobs() -> dict[str, Job]:
    """Copie du registre (lecture seule pour le CLI)."""
    return dict(_JOBS)
