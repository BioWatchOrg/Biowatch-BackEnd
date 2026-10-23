"""Définitions des jobs exposés en CLI.

Ce module ne contient PAS de logique métier : il se contente de mapper une
sous-commande CLI vers une fonction de `packages/*`. L'idempotence et le
`job_run(...)` restent gérés dans la fonction métier appelée.

Importer ce module a pour effet de peupler le registre (`register(...)`).
"""

from argparse import ArgumentParser, Namespace

from clients import init_db
from geo import generate_h3_grid

from jobs.registry import Job, register
from clients.planetary_computer_client.reader import DEFAULT_IO_THREADS
from sentinel import (
    EXTRACTION_PIPELINE_VERSION,
    backfill_sentinel,
    extract_sentinel_data_by_aoi,
    previous_month_bucket,
)


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
        bucket=args.bucket_id or previous_month_bucket(),
        resolution=args.h3_res,
        source_version=args.source_version,
        io_threads=args.io_threads,
    )


def _configure_extract_sentinel(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--aoi",
        required=True,
        help="Label de l'AOI (voir packages/core/aoi/aoi_registry.json).",
    )
    parser.add_argument(
        "--bucket_id",
        default=None,
        help="Mois traité, 'YYYY-MM' (ex: '2025-05'). Par défaut : le mois précédent.",
    )
    parser.add_argument(
        "--h3_res",
        type=int,
        default=8,
        help="Résolution H3 (entier). Par défaut : le default_res de l'AOI.",
    )
    parser.add_argument(
        "--source_version",
        default=EXTRACTION_PIPELINE_VERSION,
        help="Version du pipeline d'extraction, pas une date "
        f"(défaut : {EXTRACTION_PIPELINE_VERSION}, la version courante du code).",
    )
    parser.add_argument(
        "--io_threads",
        type=int,
        default=DEFAULT_IO_THREADS,
        help=f"Lectures d'images parallèles par bloc de pixels (défaut : {DEFAULT_IO_THREADS}).",
    )


register(
    Job(
        name="extract_sentinel",
        help="Calcule les indices Sentinel-2 mensuels d'une AOI (Planetary Computer, ADR-004).",
        run=_run_extract_sentinel,
        configure=_configure_extract_sentinel,
    )
)


def _run_backfill_sentinel(args: Namespace) -> None:
    failed = backfill_sentinel(
        args.aoi,
        args.from_bucket,
        args.to_bucket,
        args.h3_res,
        source_version=args.source_version,
        io_threads=args.io_threads,
    )
    if failed:
        # Code de sortie != 0 : visible par systemd / le shell. Relancer la même
        # commande ne refait que ces mois (les autres sont déjà en success).
        raise SystemExit(f"backfill_sentinel : {len(failed)} mois en échec : {', '.join(failed)}")


def _configure_backfill_sentinel(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--aoi",
        required=True,
        help="Label de l'AOI (voir packages/core/aoi/aoi_registry.json).",
    )
    parser.add_argument(
        "--from",
        dest="from_bucket",
        required=True,
        help="Premier mois, 'YYYY-MM' (ex: '2016-01').",
    )
    parser.add_argument(
        "--to",
        dest="to_bucket",
        required=True,
        help="Dernier mois inclus, 'YYYY-MM'.",
    )
    parser.add_argument("--h3_res", type=int, default=8, help="Résolution H3 (défaut : 8).")
    parser.add_argument(
        "--source_version",
        default=EXTRACTION_PIPELINE_VERSION,
        help=f"Version du pipeline d'extraction (défaut : {EXTRACTION_PIPELINE_VERSION}).",
    )
    parser.add_argument(
        "--io_threads",
        type=int,
        default=DEFAULT_IO_THREADS,
        help=f"Lectures d'images parallèles par bloc de pixels (défaut : {DEFAULT_IO_THREADS}).",
    )


register(
    Job(
        name="backfill_sentinel",
        help="Extrait les indices Sentinel-2 mois par mois sur une plage (reprise automatique).",
        run=_run_backfill_sentinel,
        configure=_configure_backfill_sentinel,
    )
)
