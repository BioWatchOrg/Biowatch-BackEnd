"""Point d'entrée CLI des jobs BioWatch.

Usage :
    uv run biowatch-jobs <job> [options]
    uv run biowatch-jobs generate_h3_grid --aoi <label> --resolution <n>

Un seul entrypoint, un sous-parser par job (registre dans `jobs.definitions`).
Le CLI ne fait que parser et dispatcher : l'idempotence et l'accès DB restent
dans les fonctions métier de `packages/*`.
"""

import logging
from argparse import ArgumentParser

import jobs.definitions  # noqa: F401  (import pour peupler le registre)
from jobs.registry import get_jobs


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="biowatch-jobs", description="Lanceur de jobs BioWatch.")
    sub = parser.add_subparsers(dest="job", required=True, metavar="<job>")
    for job in get_jobs().values():
        job_parser = sub.add_parser(job.name, help=job.help, description=job.help)
        job.configure(job_parser)
        job_parser.set_defaults(_run=job.run)
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    args._run(args)


if __name__ == "__main__":
    main()
