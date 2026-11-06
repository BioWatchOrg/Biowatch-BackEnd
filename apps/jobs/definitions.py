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

from clients import DEFAULT_IO_THREADS, init_db
from geo import generate_h3_grid

from jobs.registry import Job, register
from jobs.sentinel import (
    EXTRACTION_PIPELINE_VERSION,
    backfill_sentinel,
    extract_sentinel_data_by_aoi,
    month_range,
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


def _run_extract_sentinel(args: Namespace) -> None:
    bucket = args.bucket_id or previous_month_bucket()
    status = extract_sentinel_data_by_aoi(
        aoi_label=args.aoi,
        bucket=bucket,
        resolution=args.h3_res,
        source_version=args.source_version,
        io_threads=args.io_threads,
    )
    if status == "partial":
        # Code de sortie != 0 : systemd marque l'unité en échec (visible, alertable).
        # Le timer retente le mois les nuits suivantes ; à la main, relancer la commande.
        raise SystemExit(
            f"extract_sentinel : {bucket} incomplet (tuile(s) illisible(s)), à relancer"
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
    if args.months:
        months = [m.strip() for m in args.months.split(",") if m.strip()]
    elif args.from_bucket and args.to_bucket:
        months = month_range(args.from_bucket, args.to_bucket)
    else:
        raise SystemExit("backfill_sentinel : donner --from et --to, ou --months")
    report = backfill_sentinel(
        args.aoi,
        months,
        args.h3_res,
        source_version=args.source_version,
        io_threads=args.io_threads,
    )
    if report.to_retry:
        # Code de sortie != 0 : visible par systemd / le shell. Relancer la même
        # commande ne refait que ces mois (les autres sont déjà en success).
        raise SystemExit(
            f"backfill_sentinel : {len(report.to_retry)} mois à relancer "
            f"(échec : {', '.join(report.failed) or 'aucun'} ; "
            f"incomplets : {', '.join(report.partial) or 'aucun'}).\n"
            f"Pour ne relancer que ceux-là : uv run biowatch-jobs backfill_sentinel "
            f"--aoi {args.aoi} --h3_res {args.h3_res} --months {','.join(report.to_retry)}"
        )


def _configure_backfill_sentinel(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--aoi",
        required=True,
        help="Label de l'AOI (voir packages/core/aoi/aoi_registry.json).",
    )
    parser.add_argument(
        "--from",
        dest="from_bucket",
        help="Premier mois de la plage, 'YYYY-MM' (ex: '2016-01'). Avec --to.",
    )
    parser.add_argument(
        "--to",
        dest="to_bucket",
        help="Dernier mois inclus de la plage, 'YYYY-MM'. Avec --from.",
    )
    parser.add_argument(
        "--months",
        help="Liste de mois à (re)lancer, séparés par des virgules (ex: '2016-04,2019-07') — "
        "à la place de --from/--to, typiquement les mois signalés par un backfill précédent.",
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
