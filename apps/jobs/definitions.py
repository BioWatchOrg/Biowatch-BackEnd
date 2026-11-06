"""Définitions des jobs exposés en CLI.

Ce module ne contient PAS de logique métier : il se contente de mapper une
sous-commande CLI vers une fonction de `packages/*`. L'idempotence et le
`job_run(...)` restent gérés dans la fonction métier appelée.

Importer ce module a pour effet de peupler le registre (`register(...)`).
"""

from argparse import ArgumentParser, Namespace

from clients import init_db
from geo import generate_h3_grid
from species import update_gbif_occurrences

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
    )
)


def _configure_update_gbif_occurrences(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--aoi",
        required=True,
        help="Label de l'AOI (voir packages/core/aoi/aoi_registry.json).",
    )


def _run_update_gbif_occurrences(args: Namespace) -> None:
    update_gbif_occurrences(aoi_label=args.aoi)


register(
    Job(
        name="update_gbif_occurrences",
        help=(
            "Récupère les occurrences GBIF pour une AOI sur une fenêtre glissante "
            "de 10 ans et les stocke dans staging.species_occurrences_raw."
        ),
        run=_run_update_gbif_occurrences,
        configure=_configure_update_gbif_occurrences,
    )
)


def _run_init_db(args: Namespace) -> None:
    init_db()


register(
    Job(
        name="init_db",
        help="Initialise le schéma de la base clients (extensions + tables).",
        run=_run_init_db,
    )
)
