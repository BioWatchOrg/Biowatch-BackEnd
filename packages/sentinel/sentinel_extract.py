import contextvars
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from clients import (
    JobAlreadySucceeded,
    SatelliteFeaturesByZone,
    SentinelHubAuthError,
    SentinelHubRequestError,
    fetch_zone_statistics,
    job_run,
    record_zone_error,
    upsert,
)
from core import (
    AoiLabel,
    BucketId,
    compute_idempotency_key,
    get_bucket_range,
)
from geo import cell_to_geojson, compute_h3_cells

logger = logging.getLogger(__name__)


SENTINEL_JOB_NAME = "extract_sentinel_data"  # Nom du job pour idempotency_key
VALID_PIXEL_RATIO_THRESHOLD = (
    0.1  # Seuil de validité des pixels pour considérer les données comme valides
)
DEFAULT_MAX_WORKERS = 5

# Version du pipeline d'extraction (evalscript, seuils qualité, formules des
# indices) — PAS une date. À incrémenter à la main quand l'un de ces éléments
# change, pour que satellite_features_by_zone garde les anciennes lignes
# (UNIQUE(zone_id, bucket_id, source_version)) au lieu de les écraser
# silencieusement avec un résultat calculé différemment.
EXTRACTION_PIPELINE_VERSION = "1.0.0"


def _fetch_cell_row(
    cell: str,
    time_range: tuple[str, str],
    bucket_id: BucketId,
    source_version: str,
    client: httpx.Client,
) -> dict[str, object]:
    """
    Fetch one H3 cell's statistics and shape it into an upsert-ready row.

    Pure w.r.t. the DB: raises on failure (SentinelHubAuthError,
    SentinelHubRequestError, or an httpx transport error) rather than writing
    anything itself — the caller decides what a failure means (abort the run,
    or record a per-zone error and move on).
    """
    geometry = cell_to_geojson(cell)
    zone_stat = fetch_zone_statistics(geometry=geometry, time_range=time_range, client=client)
    return {
        "zone_id": cell,
        "bucket_id": bucket_id,
        "source_version": source_version,
        "ndvi": zone_stat.ndvi_mean,
        "ndwi": zone_stat.ndwi_mean,
        "ndbi": zone_stat.ndbi_mean,
        "swir": zone_stat.swir_mean,
        "obs_count": zone_stat.obs_count,
        "valid_pixel_ratio": zone_stat.valid_pixel_ratio,
        "cloud_score": zone_stat.cloud_score,
        "is_valid_data": zone_stat.valid_pixel_ratio >= VALID_PIXEL_RATIO_THRESHOLD,
        "computed_at": datetime.datetime.now(datetime.timezone.utc),
    }


def extract_sentinel_data_by_aoi(
    aoi_label: AoiLabel,
    bucket: str,
    resolution: int,
    source_version: str = EXTRACTION_PIPELINE_VERSION,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> None:
    """
    Extrait les données Sentinel pour une AOI donnée et les persiste dans la base de données.

    Args:
        aoi_label (str): Le label de l'AOI (voir packages/core/aoi/aoi_registry.json).
        bucket (str): L'ID du bucket de temps (ex: "2026-02").
        resolution (int, optional): La résolution H3. Par défaut : le default_res de l'AOI.
        source_version (str): Version du pipeline d'extraction, pas une date — voir
            EXTRACTION_PIPELINE_VERSION. Par défaut : la version courante du code.
        max_workers (int): Nombre de requêtes Sentinel Hub en parallèle (défaut : 5).
    """
    # `bucket` is already a bucket_id string (e.g. "2026-02") from the CLI —
    # nothing to compute here. `get_bucket_range` below validates its format
    # and raises if it's malformed, so there's no need to re-check it twice.
    bucket_id: BucketId = bucket
    idempotency_key = compute_idempotency_key(
        job_name=SENTINEL_JOB_NAME,
        scope=aoi_label,
        source_version=source_version,
        period=bucket_id,
        resolution=resolution,
    )
    try:
        with job_run(
            job_name=SENTINEL_JOB_NAME,
            scope=aoi_label,
            idempotency_key=idempotency_key,
            bucket_id=bucket_id,
        ) as (run, session):
            with httpx.Client(timeout=30.0) as client:
                # run_id is injected into every log below by ContextFilter (job_run).
                log_ctx = {
                    "aoi": aoi_label,
                    "resolution": resolution,
                    "bucket": bucket_id,
                    "idempotency_key": idempotency_key,
                }
                logger.info(
                    "extracting Sentinel data",
                    extra={"event": "sentinel.extract", "context": log_ctx},
                )

                start_date, end_date = get_bucket_range(bucket_id)
                # Sentinel Hub's aggregationInterval="P1M" needs a full
                # calendar month to fit inside [from, to] to produce one
                # interval. `end_date` is the bucket's last INCLUDED day
                # (get_bucket_range), so stopping at its 23:59:59 is exactly
                # one second short of a full month — the API then returns
                # zero intervals for every single request. Use the exclusive
                # upper bound (start of the day after end_date) instead.
                next_day = end_date + datetime.timedelta(days=1)
                time_range = (
                    f"{start_date.isoformat()}T00:00:00Z",
                    f"{next_day.isoformat()}T00:00:00Z",
                )

                cells = compute_h3_cells(aoi_label, resolution)

                logger.info(
                    "computed H3 cells for AOI",
                    extra={
                        "event": "h3_grid.computed",
                        "context": {
                            **log_ctx,
                            "n_cells": len(cells),
                            "start_date": start_date,
                            "end_date": end_date,
                        },
                    },
                )

                rows: list[dict[str, object]] = []
                # Copy the current context (carries run_id_var, set by job_run)
                # so worker threads log with the same run_id as the main
                # thread — a plain ThreadPoolExecutor does not inherit it.
                # A fresh Context *per submission*, not one shared across all
                # of them: `Context.run()` raises RuntimeError if the same
                # Context object is entered from more than one thread at once
                # — with max_workers > 1 that happens as soon as two cells run
                # concurrently. Each copy carries the same values (nothing
                # mutates run_id_var between cells), just not the same object.
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(
                            contextvars.copy_context().run,
                            _fetch_cell_row,
                            cell,
                            time_range,
                            bucket_id,
                            source_version,
                            client,
                        ): cell
                        for cell in cells
                    }
                    for future in as_completed(futures):
                        cell = futures[future]
                        try:
                            rows.append(future.result())
                        except SentinelHubAuthError:
                            # An auth failure (bad/expired credentials, CDSE
                            # outage) hits every zone identically — it's not a
                            # per-zone problem. Cancel whatever hasn't started
                            # yet instead of burning through every remaining
                            # cell to get the exact same failure, and abort the
                            # whole run (job_run marks it `failed`, not `partial`).
                            for pending in futures:
                                pending.cancel()
                            raise
                        except (SentinelHubRequestError, httpx.HTTPError) as e:
                            record_zone_error(run.run_id, cell, str(e))
                            logger.warning(
                                "zone extraction failed, recorded for targeted retry",
                                extra={
                                    "event": "sentinel.zone_error",
                                    "context": {**log_ctx, "zone_id": cell, "error": str(e)},
                                },
                            )

                upsert(session, SatelliteFeaturesByZone, rows)

                logger.info(
                    "computed and upserted Sentinel data",
                    extra={
                        "event": "sentinel.upsert",
                        "context": {**log_ctx, "n_cells": len(cells), "n_rows": len(rows)},
                    },
                )

    except JobAlreadySucceeded:
        logger.info(
            "sentinel data already extracted, skipping",
            extra={
                "event": "sentinel.skip",
                "context": {
                    "aoi": aoi_label,
                    "resolution": resolution,
                    "bucket": bucket_id,
                    "idempotency_key": idempotency_key,
                },
            },
        )
        return
