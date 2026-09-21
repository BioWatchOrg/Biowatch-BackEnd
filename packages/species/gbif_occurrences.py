"""update_gbif_occurrences job: GBIF Download API -> staging.species_occurrences_raw.

Fetches GBIF occurrences for an AOI over a rolling 10-year window via one
Occurrence Download request (predicate = AOI geometry + full date range --
no per-year pagination, since the Download API has no offset cap, unlike
the Search API this replaced), applies a quality filter, converts points to
H3 `zone_id`, and upserts them into `staging.species_occurrences_raw`. Runs
once a year: for year N, the window is [N-9, N].
"""

import logging
from datetime import date

from clients import (
    GbifApiError,
    JobAlreadySucceeded,
    SpeciesOccurrencesRaw,
    ZonesHex,
    build_predicate,
    job_run,
    load_gbif_credentials,
    request_download,
    stream_download,
    upsert,
    wait_for_download,
)
from core import AOI, AoiLabel, biowatch_now, compute_idempotency_key, load_aoi
from geo import point_to_h3_cell
from sqlalchemy import Engine, delete, select

from species.quality import quality_filter

logger = logging.getLogger(__name__)

GBIF_SOURCE_VERSION_PREFIX = "gbif-download-v1"
WINDOW_YEARS = 10
UPSERT_BATCH_SIZE = 5000


def rolling_window(current_year: int) -> tuple[date, date]:
    """Return (date_min, date_max) for the 10-year window ending at current_year."""
    return date(current_year - (WINDOW_YEARS - 1), 1, 1), date(current_year, 12, 31)


def _to_float(value: str | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _normalize_csv_row(row: dict) -> dict:
    """
    Normalize one SIMPLE_CSV row into the dict shape `quality_filter` expects
    (the same field names the old Search API JSON records used) -- keeps
    `quality_filter` itself untouched by this pivot.
    """
    return {
        "key": row.get("gbifID") or None,
        "scientificName": row.get("scientificName") or None,
        "taxonKey": _to_int(row.get("taxonKey")),
        "decimalLatitude": _to_float(row.get("decimalLatitude")),
        "decimalLongitude": _to_float(row.get("decimalLongitude")),
        "eventDate": row.get("eventDate") or None,
    }


def update_gbif_occurrences(aoi_label: AoiLabel, engine: Engine | None = None) -> None:
    """
    Fetch, filter, and store GBIF occurrences for an AOI's rolling 10-year window.

    Idempotent: guarded by `job_run` (one run per AOI + target year N + AOI
    version + resolution -- skip if already succeeded) and written via
    `upsert(..., update_columns=[])` (`ON CONFLICT DO NOTHING` -- a GBIF
    occurrence is an immutable historical record, re-running never needs to
    overwrite an existing row).
    """
    aoi: AOI = load_aoi(aoi_label)
    current_year = biowatch_now().year
    date_min, date_max = rolling_window(current_year)

    idempotency_key = compute_idempotency_key(
        job_name="update_gbif_occurrences",
        scope=aoi_label,
        period=str(current_year),
        source_version=f"{GBIF_SOURCE_VERSION_PREFIX}+aoi{aoi.version}",
        resolution=aoi.default_res,
    )
    try:
        with job_run(
            job_name="update_gbif_occurrences",
            scope=aoi_label,
            idempotency_key=idempotency_key,
            engine=engine,
        ) as (_, session):
            log_ctx = {"aoi": aoi_label, "year": current_year}
            logger.info(
                "purging staging rows outside the rolling window",
                extra={"event": "gbif_occurrences.purge", "context": log_ctx},
            )
            session.execute(
                delete(SpeciesOccurrencesRaw).where(
                    SpeciesOccurrencesRaw.aoi_id == aoi_label,
                    SpeciesOccurrencesRaw.event_date < date_min,
                )
            )

            valid_zone_ids = set(
                session.scalars(
                    select(ZonesHex.zone_id).where(ZonesHex.aoi_id == aoi_label)
                ).all()
            )
            # Same reasoning as the old Search API's `geometry` param: the
            # exact polygon is too complex for GBIF, the convex hull is
            # small and always contains it. Safe here too -- occurrences
            # outside the AOI's real shape are still filtered by the
            # zone-membership check below.
            geometry_wkt = aoi.geom.convex_hull.wkt
            predicate = build_predicate(geometry_wkt, date_min, date_max)
            creds = load_gbif_credentials()

            try:
                download_key = request_download(predicate, creator=creds.username, auth=creds.auth)
                logger.info(
                    "GBIF download requested",
                    extra={
                        "event": "gbif_occurrences.download_requested",
                        "context": {**log_ctx, "download_key": download_key},
                    },
                )
                wait_for_download(download_key, auth=creds.auth)
                logger.info(
                    "GBIF download ready",
                    extra={
                        "event": "gbif_occurrences.download_ready",
                        "context": {**log_ctx, "download_key": download_key},
                    },
                )

                rows: list[dict[str, object]] = []
                n_total = 0
                for raw_row in stream_download(download_key, auth=creds.auth):
                    record = _normalize_csv_row(raw_row)
                    quality_row = quality_filter(record, date_min, date_max)
                    if quality_row is None:
                        continue
                    zone_id = point_to_h3_cell(
                        quality_row["decimal_latitude"],
                        quality_row["decimal_longitude"],
                        aoi.default_res,
                    )
                    if zone_id not in valid_zone_ids:
                        continue
                    rows.append({**quality_row, "zone_id": zone_id, "aoi_id": aoi_label})
                    if len(rows) >= UPSERT_BATCH_SIZE:
                        upsert(session=session, model=SpeciesOccurrencesRaw, rows=rows, update_columns=[])
                        n_total += len(rows)
                        rows = []
                if rows:
                    upsert(session=session, model=SpeciesOccurrencesRaw, rows=rows, update_columns=[])
                    n_total += len(rows)

                logger.info(
                    "upserted GBIF occurrences",
                    extra={
                        "event": "gbif_occurrences.upserted",
                        "context": {**log_ctx, "download_key": download_key, "n_rows": n_total},
                    },
                )
            except GbifApiError:
                logger.error(
                    "GBIF download failed",
                    extra={"event": "gbif_occurrences.download_error", "context": log_ctx},
                )
                raise

    except JobAlreadySucceeded:
        logger.info(
            "GBIF occurrences already up to date, skipping",
            extra={
                "event": "gbif_occurrences.skip",
                "context": {"aoi": aoi_label, "idempotency_key": idempotency_key},
            },
        )
        return
