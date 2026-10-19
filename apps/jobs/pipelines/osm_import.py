"""
OSM snapshot import (ticket #35): Geofabrik `.pbf` → osm2pgsql → `osm_tmp_{aoi}_{bucket}`.

First stage of the OSM pipeline. It only stages raw OSM objects (buildings,
roads, urban landuse) in a temporary schema; the compute job (#38) aggregates
them per H3 zone into `osm_features_by_zone` and drops the schema.

Idempotence: one run per (AOI, annual bucket, source version, env). A run in
`success` is skipped. On retry, a schema left behind by a failed attempt is
dropped and re-imported (it cannot belong to a successful run).

End-of-import signal: `job_runs.status = success` with `output_ref` = schema
name is always written. An SQS "import done" message is also published when
`OSM_IMPORT_DONE_QUEUE_URL` is set — BioWatch has no AWS account yet, so it is
disabled by default and turns on through configuration only.
"""

import logging
import os
from pathlib import Path
from typing import Any

from clients import (
    JobAlreadySucceeded,
    create_schema,
    drop_schema,
    get_run_status,
    job_run,
    schema_exists,
)
from clients.osm import (
    GEOFABRIK_REGIONS,
    Snapshot,
    download,
    run_osm2pgsql,
    snapshot_for,
    snapshot_from_urls,
)
from clients.sqs import send_message
from core import BucketFormat, aoi_registry, bucket_id, compute_idempotency_key, load_aoi
from sqlalchemy import Engine

logger = logging.getLogger(__name__)

JOB_NAME = "osm_import_snapshot"

# SQS queue for the "import done" signal. Unset ⇒ signal disabled (job_runs only).
QUEUE_URL_ENV = "OSM_IMPORT_DONE_QUEUE_URL"

# Where `.pbf` files are downloaded. Files are deleted after each attempt.
DATA_DIR_ENV = "OSM_DATA_DIR"
DEFAULT_DATA_DIR = "data/osm"


def tmp_schema_name(aoi_label: str, bucket: str) -> str:
    """Temporary schema holding the raw import, consumed then dropped by #38."""
    return f"osm_tmp_{aoi_label}_{bucket}"


def _scope(aoi_label: str) -> str:
    return f"region:{aoi_label}"


def import_idempotency_key(snapshot: Snapshot, bucket: str, env: str) -> str:
    return compute_idempotency_key(
        job_name=JOB_NAME,
        scope=_scope(snapshot.aoi_label),
        period=bucket,
        source_version=snapshot.source_version,
        env=env,
    )


def publish_import_done(payload: dict[str, Any]) -> None:
    """
    Publish the "import done" signal on SQS, if a queue is configured.

    Single swap point for the signal transport: when BioWatch moves to AWS,
    setting `OSM_IMPORT_DONE_QUEUE_URL` (+ AWS credentials) is enough.
    """
    queue_url = os.environ.get(QUEUE_URL_ENV)
    if not queue_url:
        logger.info(
            "SQS signal disabled, job_runs is the import-done signal",
            extra={"event": "osm_import.signal_disabled", "context": payload},
        )
        return
    send_message(queue_url, payload)


def osm_import_snapshot(
    aoi_label: str,
    bucket: str,
    env: str = "dev",
    links: list[str] | None = None,
    engine: Engine | None = None,
) -> bool:
    """
    Import the OSM snapshot of an AOI for an annual bucket into its temporary schema.

    `links` overrides the Geofabrik URLs resolved from the AOI (manual runs).
    Returns True if an import ran, False if a previous run already succeeded.
    """
    aoi = load_aoi(aoi_label)  # fail fast on an unknown AOI, before any run is recorded
    snapshot = (
        snapshot_from_urls(aoi_label, bucket, links) if links else snapshot_for(aoi_label, bucket)
    )
    schema = tmp_schema_name(aoi_label, bucket)
    key = import_idempotency_key(snapshot, bucket, env)
    log_ctx = {
        "aoi": aoi_label,
        "bucket": bucket,
        "schema": schema,
        "source_version": snapshot.source_version,
        "env": env,
    }
    try:
        with job_run(
            JOB_NAME,
            scope=_scope(aoi_label),
            idempotency_key=key,
            bucket_id=bucket,
            engine=engine,
        ) as (run, _session):
            if schema_exists(schema, engine):
                # Not a success (job_run would have skipped): leftover of a failed attempt.
                logger.warning(
                    "dropping schema left by a previous failed import",
                    extra={"event": "osm_import.stale_schema", "context": log_ctx},
                )
                drop_schema(schema, engine)

            data_dir = Path(os.environ.get(DATA_DIR_ENV, DEFAULT_DATA_DIR)) / bucket
            paths: list[Path] = []
            try:
                for url in snapshot.urls:
                    paths.append(download(url, data_dir))
                create_schema(schema, engine)
                run_osm2pgsql(paths, schema, engine)
            finally:
                for path in paths:
                    path.unlink(missing_ok=True)

            run.output_ref = schema
            # Last step: if publishing fails the run is marked failed and the
            # retry re-imports, so a "success" never exists without its signal.
            publish_import_done(
                {
                    "region_id": aoi_label,
                    "bucket_id": bucket,
                    "schema_name": schema,
                    "source_version": snapshot.source_version,
                    "h3_res": aoi.default_res,
                    "env": env,
                    "pbf_links": list(snapshot.urls),
                    "run_id": str(run.run_id),
                }
            )
            logger.info(
                "OSM snapshot imported", extra={"event": "osm_import.done", "context": log_ctx}
            )
    except JobAlreadySucceeded:
        logger.info(
            "OSM snapshot already imported, skipping",
            extra={"event": "osm_import.skip", "idempotency_key": key, "context": log_ctx},
        )
        return False
    return True


def osm_import_scheduled(
    env: str = "dev", bucket: str | None = None, engine: Engine | None = None
) -> str | None:
    """
    Scheduled entrypoint: import the next region not yet imported for the bucket.

    Run monthly (systemd timer) ⇒ one region per month. Regions are walked in
    `GEOFABRIK_REGIONS` order, limited to AOIs present in the registry. A region
    whose last attempt failed is retried before moving on. Returns the imported
    AOI label, or None when every region is already imported for the bucket.
    """
    bucket = bucket or bucket_id(format=BucketFormat.YEARLY)
    for aoi_label in GEOFABRIK_REGIONS:
        if not aoi_registry(aoi_label):
            continue
        key = import_idempotency_key(snapshot_for(aoi_label, bucket), bucket, env)
        if get_run_status(key, engine) == "success":
            continue
        logger.info(
            "scheduled OSM import",
            extra={
                "event": "osm_import.scheduled",
                "context": {"aoi": aoi_label, "bucket": bucket},
            },
        )
        osm_import_snapshot(aoi_label, bucket, env=env, engine=engine)
        return aoi_label
    logger.info(
        "every registered region already imported for this bucket",
        extra={"event": "osm_import.nothing_to_do", "context": {"bucket": bucket}},
    )
    return None
