"""Manifeste des timers systemd, dérivé du registre des jobs (#27).

Lancé par le déploiement sur le VPS, depuis la release qui vient d'être
installée :

    /opt/biowatch/app/.venv/bin/python -m jobs.systemd

Sortie : une ligne `<job>\\t<OnCalendar>` par job planifié, triée par nom.
`biowatch-install-timers` (root) la lit, la revalide et en génère les timers
`biowatch-job@<job>.timer`. Le manifeste ne transporte que des DONNÉES (nom,
fréquence) : le contenu des unités est figé côté VPS, la CI ne peut pas
fabriquer un service arbitraire.
"""

import sys
from collections.abc import Mapping

import jobs.definitions  # noqa: F401  (import pour peupler le registre)
from jobs.registry import Job, get_jobs


def timer_manifest(jobs: Mapping[str, Job]) -> str:
    """Manifeste TSV des jobs planifiés (`schedule` non nul), trié par nom."""
    lines = [
        f"{job.name}\t{job.schedule}"
        for job in sorted(jobs.values(), key=lambda j: j.name)
        if job.schedule is not None
    ]
    return "".join(f"{line}\n" for line in lines)


def main() -> None:
    # stdout est le canal de données (lu par un script shell) : pas de logging ici.
    sys.stdout.write(timer_manifest(get_jobs()))


if __name__ == "__main__":
    main()
