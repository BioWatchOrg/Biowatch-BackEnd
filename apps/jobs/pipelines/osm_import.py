"""
OSM snapshot import (ticket #35): Geofabrik `.pbf` → osm2pgsql → `osm_tmp_{aoi}_{bucket}_{key}`.

First stage of the OSM pipeline. It only stages raw OSM objects (buildings,
roads, urban landuse) in a temporary schema; the compute job (#38) aggregates
them per H3 zone into `osm_features_by_zone` and drops the schema.

Idempotence: one run per (AOI, annual bucket, source version), where the
source version covers both the snapshot and `style.lua` (`STYLE_VERSION`). The
environment is not part of the key: dev and prod each have their own database,
hence their own `job_runs` (dev/prod split, #18). A
run in `success` is skipped. The schema name ends with a prefix of the
idempotency key, so each key has its own schema: on retry, a schema found
under that name is the leftover of a failed attempt of the same key, never the
output of a successful run, and is dropped and re-imported.

End-of-import signal: the `job_runs` row in `success`. Its `output_ref` is a
short JSON object, `{"schema": ..., "source_version": ...}`: the compute job
(#38) reads the successful imports it has not processed yet and finds the
schema there (it never rebuilds the name), with the version to trace its
figures back to.
"""

import json
import logging
import os
from pathlib import Path

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
    STYLE_VERSION,
    Snapshot,
    download,
    run_osm2pgsql,
    snapshot_for,
    snapshot_from_urls,
)
from core import (
    BucketFormat,
    aoi_registry,
    compute_idempotency_key,
    format_bucket_id,
    load_aoi,
)
from sqlalchemy import Engine

logger = logging.getLogger(__name__)

JOB_NAME = "osm_import_snapshot"

# Where `.pbf` files are downloaded. Files are deleted after each attempt.
DATA_DIR_ENV = "OSM_DATA_DIR"
DEFAULT_DATA_DIR = "data/osm"


# Length of the idempotency key prefix in the schema name (hex digits).
_SCHEMA_KEY_LEN = 8


def tmp_schema_name(aoi_label: str, bucket: str, idempotency_key: str) -> str:
    """
    Temporary schema holding the raw import, consumed then dropped by #38.

    Suffixed with the idempotency key so that runs with different keys (other
    snapshot or style version) never share, and never drop, a schema.
    """
    return f"osm_tmp_{aoi_label}_{bucket}_{idempotency_key[:_SCHEMA_KEY_LEN]}"


def _scope(aoi_label: str) -> str:
    return f"region:{aoi_label}"


def import_source_version(snapshot: Snapshot) -> str:
    """Snapshot version + style version: a change to either re-imports the bucket."""
    return f"{snapshot.source_version}_style-v{STYLE_VERSION}"


def import_idempotency_key(snapshot: Snapshot, bucket: str) -> str:
    return compute_idempotency_key(
        job_name=JOB_NAME,
        scope=_scope(snapshot.aoi_label),
        period=bucket,
        source_version=import_source_version(snapshot),
    )


def import_output_ref(schema: str, source_version: str) -> str:
    """`job_runs.output_ref` of a successful import: what #38 needs, as short JSON."""
    return json.dumps({"schema": schema, "source_version": source_version})


def osm_import_snapshot(
    aoi_label: str,
    bucket: str,
    links: list[str] | None = None,
    engine: Engine | None = None,
) -> bool:
    """
    Import the OSM snapshot of an AOI for an annual bucket into its temporary schema.

    `links` overrides the Geofabrik URLs resolved from the AOI (manual runs).
    Returns True if an import ran, False if a previous run already succeeded.
    """
    load_aoi(aoi_label)  # fail fast on an unknown AOI, before any run is recorded
    snapshot = (
        snapshot_from_urls(aoi_label, bucket, links) if links else snapshot_for(aoi_label, bucket)
    )
    source_version = import_source_version(snapshot)
    key = import_idempotency_key(snapshot, bucket)
    schema = tmp_schema_name(aoi_label, bucket, key)
    log_ctx = {
        "aoi": aoi_label,
        "bucket": bucket,
        "schema": schema,
        "source_version": source_version,
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
                # The name is unique to this key, and this key is not a success
                # (job_run would have skipped): leftover of a failed attempt.
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

            run.output_ref = import_output_ref(schema, source_version)
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


def osm_import_scheduled(bucket: str | None = None, engine: Engine | None = None) -> str | None:
    """
    Scheduled entrypoint: import the next region not yet imported for the bucket.

    Run monthly (systemd timer) ⇒ one region per month. Regions are walked in
    `GEOFABRIK_REGIONS` order, limited to AOIs present in the registry. Regions
    never attempted come first; regions whose last attempt did not succeed are
    retried only after them, so a region that keeps failing does not block the
    others. Returns the imported AOI label, or None when every region is
    already imported for the bucket.
    """
    bucket = bucket or format_bucket_id(format=BucketFormat.YEARLY)
    never_attempted: list[str] = []
    to_retry: list[str] = []
    for aoi_label in GEOFABRIK_REGIONS:
        if not aoi_registry(aoi_label):
            continue
        key = import_idempotency_key(snapshot_for(aoi_label, bucket), bucket)
        status = get_run_status(key, engine)
        if status is None:
            never_attempted.append(aoi_label)
        elif status != "success":
            to_retry.append(aoi_label)

    if not never_attempted and not to_retry:
        logger.info(
            "every registered region already imported for this bucket",
            extra={"event": "osm_import.nothing_to_do", "context": {"bucket": bucket}},
        )
        return None

    aoi_label = (never_attempted or to_retry)[0]
    context = {"aoi": aoi_label, "bucket": bucket, "retry": not never_attempted}
    logger.info(
        "scheduled OSM import",
        extra={"event": "osm_import.scheduled", "context": context},
    )
    try:
        osm_import_snapshot(aoi_label, bucket, engine=engine)
    except Exception as e:
        logger.error(
            "scheduled OSM import failed, region will be retried after the others",
            extra={"event": "osm_import.region_failed", "context": {**context, "error": str(e)}},
        )
        raise
    return aoi_label
