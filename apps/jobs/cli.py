"""Point d'entrée CLI des jobs BioWatch.

Usage :
    uv run biowatch-jobs <job> [options]
    uv run biowatch-jobs generate_h3_grid --aoi <label> --resolution <n>

Un seul entrypoint, un sous-parser par job (registre dans `jobs.definitions`).
Le CLI ne fait que parser et dispatcher : l'idempotence et l'accès DB restent
dans les fonctions métier de `packages/*`.
"""

from argparse import ArgumentParser

from clients import setup_logging

import jobs.definitions  # noqa: F401  (import pour peupler le registre)
from jobs.registry import get_jobs


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="biowatch-jobs", description="Lanceur de jobs BioWatch.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Active les logs DEBUG (dev). En prod, laisser le défaut (LOG_LEVEL / INFO).",
    )
    sub = parser.add_subparsers(dest="job", required=True, metavar="<job>")
    for job in get_jobs().values():
        job_parser = sub.add_parser(job.name, help=job.help, description=job.help)
        job.configure(job_parser)
        job_parser.set_defaults(_run=job.run)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    # Une seule configuration du logging, au démarrage de l'entrypoint.
    setup_logging(service="biowatch-jobs", level="DEBUG" if args.debug else None)
    args._run(args)


if __name__ == "__main__":
    main()
