import datetime
import logging

import httpx

from packages.clients import (
    JobAlreadySucceeded,
    SatelliteFeaturesByZone,
    fetch_zone_statistics,
    job_run,
    upsert,
)
from packages.core import (
    AoiLabel,
    BucketId,
    compute_idempotency_key,
    get_bucket_range,
    parse_bucket_id,
)
from packages.geo import cell_to_geojson, compute_h3_cells

logger = logging.getLogger(__name__)


SENTINEL_JOB_NAME = "extract_sentinel_data"  # Nom du job pour idempotency_key
VALID_PIXEL_RATIO_THRESHOLD = (
    0.1  # Seuil de validité des pixels pour considérer les données comme valides
)


def extract_sentinel_data_by_aoi(
    aoi_label: AoiLabel, bucket: str, resolution: int, source_version: str
) -> None:
    """
    Extrait les données Sentinel pour une AOI donnée et les persiste dans la base de données.

    Args:
        aoi_label (str): Le label de l'AOI (voir packages/core/aoi/aoi_registry.json).
        bucket (str): L'ID du bucket de temps (ex: "2026-02").
        resolution (int, optional): La résolution H3. Par défaut : le default_res de l'AOI.
        source_version (str): La version de la source.
    """
    bucket_id: BucketId = parse_bucket_id(bucket)
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
        ) as (_, session):
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
                time_range = (
                    f"{start_date.isoformat()}T00:00:00Z",
                    f"{end_date.isoformat()}T23:59:59Z",
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
                for cell in cells:
                    geometry = cell_to_geojson(cell)

                    zone_stat = fetch_zone_statistics(
                        geometry=geometry, time_range=time_range, client=client
                    )
                    row: dict[str, object] = {
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
                        "is_valid_data": zone_stat.valid_pixel_ratio > VALID_PIXEL_RATIO_THRESHOLD,
                        "computed_at": datetime.datetime.now(
                            datetime.timezone.utc
                        ),  # TODO : vérifier si je dois transformer la données en string
                    }
                    rows.append(row)

                upsert(session, SatelliteFeaturesByZone, rows)

                logger.info(
                    "computed and upserted Sentinel data",
                    extra={
                        "event": "sentinel.upsert",
                        "context": {**log_ctx, "n_cells": len(cells)},
                    },
                )

    except JobAlreadySucceeded:
        logger.info(
            "grid already generated, skipping",
            extra={
                "event": "h3_grid.skip",
                "context": {
                    "aoi": aoi_label,
                    "resolution": resolution,
                    "idempotency_key": idempotency_key,
                },
            },
        )
        return
