import datetime
import logging
import re
import time
from collections import defaultdict

import pandas as pd
import shapely
from shapely.geometry import box

from clients import (
    JobAlreadySucceeded,
    PlanetaryComputerError,
    SatelliteFeaturesByZone,
    deduplicate_scenes,
    group_by_tile,
    job_run,
    record_zone_error,
    search_scenes,
    tile_footprints,
    upsert,
)
from clients.planetary_computer_client.reader import DEFAULT_IO_THREADS, open_tile
from core import (
    AoiLabel,
    BucketFormat,
    BucketId,
    biowatch_now,
    compute_idempotency_key,
    format_bucket_id,
    get_bucket_range,
    load_aoi,
)
from geo import H3Cell, cell_to_polygon, compute_h3_cells
from geo.raster_h3 import assign_owner_tiles
from sentinel.rows import to_feature_rows
from sentinel.tile_processing import process_tile, reader_with_threads

logger = logging.getLogger(__name__)


SENTINEL_JOB_NAME = "extract_sentinel_data"  # Nom du job pour idempotency_key

# Version du pipeline d'extraction (source, masque, formules, seuils) — PAS une
# date. À incrémenter à la main quand l'un de ces éléments change, pour que
# satellite_features_by_zone garde les anciennes lignes
# (UNIQUE(zone_id, bucket_id, source_version)) au lieu de les écraser avec un
# résultat calculé différemment.
#   1.0.0 : Statistical API Sentinel Hub, cadence annuelle (ADR-003, abandonné).
#   2.0.0 : calcul local depuis les COG Planetary Computer, mensuel (ADR-004).
EXTRACTION_PIPELINE_VERSION = "2.0.0"

MONTHLY_BUCKET_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def previous_month_bucket(today: datetime.date | None = None) -> BucketId:
    """Bucket du mois précédent (ex. le 5 novembre -> "YYYY-10").

    Sert de défaut au run mensuel : on traite le mois M-1, complet.
    """
    first_of_month = (today or biowatch_now()).replace(day=1)
    return format_bucket_id(first_of_month - datetime.timedelta(days=1), BucketFormat.MONTHLY)


def _region_of(cells: list[H3Cell]) -> shapely.Polygon:
    """Rectangle englobant (WGS84) des cellules : la zone à lire dans une tuile."""
    return box(*shapely.total_bounds([cell_to_polygon(cell) for cell in cells]))


def extract_sentinel_data_by_aoi(
    aoi_label: AoiLabel,
    bucket: str,
    resolution: int,
    source_version: str = EXTRACTION_PIPELINE_VERSION,
    io_threads: int = DEFAULT_IO_THREADS,
) -> None:
    """
    Calcule les indices Sentinel-2 mensuels d'une AOI et les persiste dans
    `satellite_features_by_zone` (ADR-004).

    Déroulé : cellules H3 de l'AOI -> scènes du mois (Planetary Computer,
    dédoublonnées) -> une tuile propriétaire par cellule -> lecture et
    composite tuile par tuile -> une ligne par cellule, en un seul upsert.

    - Une tuile illisible -> `record_zone_error` pour chacune de ses cellules
      (run `partial`, reprise ciblée possible), les autres tuiles continuent.
    - Une cellule sans aucune image exploitable -> ligne écrite avec indices
      NULL et `is_valid_data = false` (trou explicite, pas une erreur).
    - Recherche STAC en échec -> le run entier échoue (`failed`).

    Args:
        aoi_label: label de l'AOI (voir packages/core/aoi/aoi_registry.json).
        bucket: mois traité, "YYYY-MM".
        resolution: résolution H3.
        source_version: version du pipeline (voir EXTRACTION_PIPELINE_VERSION).
        io_threads: lectures d'images parallèles par bloc de pixels.
    """
    if not MONTHLY_BUCKET_RE.fullmatch(bucket):
        raise ValueError(
            f"extract_sentinel_data_by_aoi attend un bucket_id mensuel ('YYYY-MM'), reçu {bucket!r}."
        )
    bucket_id: BucketId = bucket
    idempotency_key = compute_idempotency_key(
        job_name=SENTINEL_JOB_NAME,
        scope=aoi_label,
        source_version=source_version,
        period=bucket_id,
        resolution=resolution,
    )
    log_ctx = {
        "aoi": aoi_label,
        "resolution": resolution,
        "bucket": bucket_id,
        "idempotency_key": idempotency_key,
    }
    try:
        with job_run(
            job_name=SENTINEL_JOB_NAME,
            scope=aoi_label,
            idempotency_key=idempotency_key,
            bucket_id=bucket_id,
        ) as (run, session):
            # run_id is injected into every log below by ContextFilter (job_run).
            started = time.monotonic()
            logger.info(
                "extracting Sentinel data",
                extra={"event": "sentinel.extract", "context": log_ctx},
            )
            cells = compute_h3_cells(aoi_label, resolution)
            start, end = get_bucket_range(bucket_id)
            scenes = deduplicate_scenes(search_scenes(load_aoi(aoi_label).bbox, start, end))
            groups = group_by_tile(scenes)
            owners = assign_owner_tiles(cells, tile_footprints(groups))

            owned_by_tile: dict[str, list[H3Cell]] = defaultdict(list)
            for cell, tile in owners.items():
                owned_by_tile[tile].append(cell)

            aggregates: list[pd.DataFrame] = []
            failed_cells: set[H3Cell] = set()
            read = reader_with_threads(io_threads)
            for tile, owned in sorted(owned_by_tile.items()):
                tile_ctx = {**log_ctx, "tile": tile, "n_cells": len(owned)}
                tile_started = time.monotonic()
                try:
                    stack = open_tile(groups[tile], _region_of(owned))
                    aggregates.append(process_tile(stack, owned, read=read))
                except PlanetaryComputerError as e:
                    for cell in owned:
                        record_zone_error(run.run_id, cell, f"tile {tile}: {e}")
                    failed_cells.update(owned)
                    logger.warning(
                        "tile extraction failed, its zones recorded for targeted retry",
                        extra={
                            "event": "sentinel.tile_error",
                            "context": {**tile_ctx, "error": str(e)},
                        },
                    )
                    continue
                logger.info(
                    "tile processed",
                    extra={
                        "event": "sentinel.tile_done",
                        "context": {
                            **tile_ctx,
                            "n_dates": len(stack.baselines),
                            "duration_ms": round((time.monotonic() - tile_started) * 1000),
                        },
                    },
                )

            rows = to_feature_rows(
                [cell for cell in cells if cell not in failed_cells],
                pd.concat(aggregates) if aggregates else pd.DataFrame(),
                bucket_id=bucket_id,
                source_version=source_version,
                computed_at=datetime.datetime.now(datetime.UTC),
            )
            upsert(session, SatelliteFeaturesByZone, rows)
            logger.info(
                "computed and upserted Sentinel data",
                extra={
                    "event": "sentinel.upsert",
                    "context": {
                        **log_ctx,
                        "n_scenes": len(scenes),
                        "n_tiles": len(groups),
                        "n_cells": len(cells),
                        "n_rows": len(rows),
                        "n_rows_without_data": sum(row["ndvi"] is None for row in rows),
                        "n_failed_cells": len(failed_cells),
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                },
            )
    except JobAlreadySucceeded:
        logger.info(
            "sentinel data already extracted, skipping",
            extra={"event": "sentinel.skip", "context": log_ctx},
        )
