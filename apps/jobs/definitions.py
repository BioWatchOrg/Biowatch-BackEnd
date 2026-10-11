"""Définitions des jobs exposés en CLI.

Ce module ne contient PAS de logique métier : il se contente de mapper une
sous-commande CLI vers une fonction de `packages/*` ou d'un pipeline de
`jobs.pipelines`. L'idempotence et le
`job_run(...)` restent gérés dans la fonction métier appelée.

Importer ce module a pour effet de peupler le registre (`register(...)`).
"""

from argparse import ArgumentParser, Namespace

from clients import init_db
from geo import generate_h3_grid

from jobs.pipelines.osm_import import osm_import_scheduled, osm_import_snapshot
from jobs.registry import Job, register

ENVS = ("dev", "prod")


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


def _configure_osm_import_snapshot(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--aoi",
        required=True,
        help="Label de l'AOI (voir packages/core/aoi/aoi_registry.json).",
    )
    parser.add_argument(
        "--bucket",
        required=True,
        help="Bucket annuel (YYYY) : importe le snapshot Geofabrik du 1er janvier.",
    )
    parser.add_argument(
        "--link",
        action="append",
        default=None,
        help="URL .pbf à importer à la place du mapping Geofabrik (répétable).",
    )
    parser.add_argument("--env", choices=ENVS, default="dev", help="Environnement (défaut : dev).")


def _run_osm_import_snapshot(args: Namespace) -> None:
    osm_import_snapshot(aoi_label=args.aoi, bucket=args.bucket, env=args.env, links=args.link)


register(
    Job(
        name="osm_import_snapshot",
        help="Importe un snapshot OSM (Geofabrik) d'une AOI dans osm_tmp_{aoi}_{bucket}.",
        run=_run_osm_import_snapshot,
        configure=_configure_osm_import_snapshot,
    )
)


def _configure_osm_import_scheduled(parser: ArgumentParser) -> None:
    parser.add_argument("--env", choices=ENVS, default="dev", help="Environnement (défaut : dev).")
    parser.add_argument(
        "--bucket",
        default=None,
        help="Bucket annuel (YYYY). Par défaut : l'année en cours.",
    )


def _run_osm_import_scheduled(args: Namespace) -> None:
    osm_import_scheduled(env=args.env, bucket=args.bucket)


register(
    Job(
        name="osm_import_scheduled",
        help="Job planifié (mensuel) : importe la prochaine région OSM pas encore importée.",
        run=_run_osm_import_scheduled,
        configure=_configure_osm_import_scheduled,
    )
)
