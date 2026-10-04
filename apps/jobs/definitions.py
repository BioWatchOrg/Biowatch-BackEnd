"""Définitions des jobs exposés en CLI.

Ce module ne contient PAS de logique métier : il se contente de mapper une
sous-commande CLI vers une fonction de `packages/*`. L'idempotence et le
`job_run(...)` restent gérés dans la fonction métier appelée.

Importer ce module a pour effet de peupler le registre (`register(...)`).
"""

import datetime
from argparse import ArgumentParser, Namespace

from clients import init_db
from geo import generate_h3_grid

from jobs.registry import Job, register
from sentinel import DEFAULT_MAX_WORKERS, extract_sentinel_data_by_aoi


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


def _run_init_db(args: Namespace) -> None:
    init_db()


register(
    Job(
        name="init_db",
        help="Initialise le schéma de la base clients (extensions + tables).",
        run=_run_init_db,
    )
)


def _run_extract_sentinel(args: Namespace) -> None:
    extract_sentinel_data_by_aoi(
        aoi_label=args.aoi,
        bucket=args.bucket_id,
        resolution=args.h3_res,
        source_version=args.source_version,
        max_workers=args.max_workers,
    )


def _configure_extract_sentinel(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--aoi",
        required=True,
        help="Label de l'AOI (voir packages/core/aoi/aoi_registry.json).",
    )
    parser.add_argument(
        "--bucket_id",
        required=True,
        help="ID du bucket de temps (ex: '2026-02').",
    )
    parser.add_argument(
        "--h3_res",
        type=int,
        default=8,
        help="Résolution H3 (entier). Par défaut : le default_res de l'AOI.",
    )
    parser.add_argument(
        "--source_version",
        required=True,
        default=datetime.datetime.now().strftime("%Y-%m"),
        help="Version de la source (par défaut : la version actuelle).",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f"Requêtes Sentinel Hub en parallèle (défaut : {DEFAULT_MAX_WORKERS}).",
    )


register(
    Job(
        name="extract_sentinel",
        help="Extrait les données Sentinel pour une AOI et un bucket donnés.",
        run=_run_extract_sentinel,
        configure=_configure_extract_sentinel,
    )
)
