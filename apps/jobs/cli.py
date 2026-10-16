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

from clients import DatabaseConfigError, InvalidLogLevelError, setup_logging
from core import (
    ENVS,
    EnvFileNotFoundError,
    UnknownEnvError,
    load_env_file,
    normalize_env,
    resolve_env,
)

import jobs.definitions  # noqa: F401  (import pour peupler le registre)
from jobs.registry import get_jobs

logger = logging.getLogger(__name__)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="biowatch-jobs", description="Lanceur de jobs BioWatch.")
    parser.add_argument(
        "--env",
        # Même normalisation que `resolve_env` (`--env PROD` == `BIOWATCH_ENV=PROD`),
        # puis `choices` rejette un env inconnu dès le parsing.
        type=normalize_env,
        choices=ENVS,
        default=None,
        help="Environnement cible. Défaut : BIOWATCH_ENV, sinon 'dev'.",
    )
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
    try:
        env = resolve_env(args.env)
        # `load_env_file` fige BIOWATCH_ENV et charge le dotenv. Les deux doivent
        # arriver AVANT le premier accès DB (`get_engine` est mis en cache à sa
        # première construction) et AVANT setup_logging (qui lit LOG_LEVEL dès
        # son appel). En prod, c'est aussi le garde-fou : pas de `.env.prod`,
        # pas de run.
        load_env_file(env)
        # Une seule configuration du logging, au démarrage de l'entrypoint.
        setup_logging(service="biowatch-jobs", level="DEBUG" if args.debug else None)
    except (UnknownEnvError, EnvFileNotFoundError, InvalidLogLevelError) as e:
        # Erreur de configuration, pas un bug : message lisible sur stderr,
        # code de sortie 1, pas de traceback. Le logging n'est pas (encore)
        # configuré ici, SystemExit est le canal propre.
        raise SystemExit(f"biowatch-jobs: {e}") from e
    logger.info(
        "job start",
        extra={"event": "cli.start", "context": {"env": env, "job": args.job}},
    )
    try:
        args._run(args)
    except DatabaseConfigError as e:
        # Levée au premier accès DB (engine paresseux) : base mal nommée pour
        # l'env, variable manquante, URL illisible. Erreur de config, pas un
        # bug — on la logge (le logging est configuré ici) et on sort proprement.
        logger.error(
            "database configuration error",
            extra={"event": "cli.config_error", "context": {"env": env, "error": str(e)}},
        )
        raise SystemExit(f"biowatch-jobs: {e}") from e


if __name__ == "__main__":
    main()
