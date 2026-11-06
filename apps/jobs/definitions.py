"""Définitions des jobs exposés en CLI.

Ce module ne contient PAS de logique métier : il se contente de mapper une
sous-commande CLI vers une fonction de `packages/*`. L'idempotence et le
`job_run(...)` restent gérés dans la fonction métier appelée.

Importer ce module a pour effet de peupler le registre (`register(...)`).

Chaque job déclare sa fréquence de lancement (`schedule`, voir `jobs.registry`) :
c'est elle qui génère son timer systemd en prod. `None` = lancé à la main. Ne
planifier un job que si sa documentation fixe la fréquence.
"""

from argparse import ArgumentParser, Namespace

from clients import init_db
from geo import generate_h3_grid

from jobs.registry import Job, register


def _configure_generate_h3_grid(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--aoi",
        required=True,
        help="Label de l'AOI (voir packages/core/aoi/aoi_registry.json).",
    )
    parser.add_argument(
        "--resolution",
        type=int,
        default=None,
        help="Résolution H3 (entier). Par défaut : le default_res de l'AOI.",
    )


def _run_generate_h3_grid(args: Namespace) -> None:
    generate_h3_grid(aoi_label=args.aoi, resolution=args.resolution)


register(
    Job(
        name="generate_h3_grid",
        help="Génère la grille H3 d'une AOI et la persiste dans zones_hex.",
        run=_run_generate_h3_grid,
        configure=_configure_generate_h3_grid,
        # Lancé à la main, une fois par AOI : la grille ne change que si l'AOI change.
        schedule=None,
    )
)


def _run_init_db(args: Namespace) -> None:
    init_db()


register(
    Job(
        name="init_db",
        help="Initialise le schéma de la base clients (extensions + tables).",
        run=_run_init_db,
        # Lancé à la main après un déploiement qui modifie le schéma.
        schedule=None,
    )
)
