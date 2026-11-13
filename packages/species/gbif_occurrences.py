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
    download_zip,
    geometry_to_gbif_wkt,
    iter_zip_rows,
    job_run,
    load_gbif_credentials,
    request_download,
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

    Keeps the full history ever fetched -- data older than the current
    window is never deleted. Each run replaces the data for the window it
    just refetched (`[date_min, date_max]`): existing rows in that range are
    deleted first, then the fresh fetch is inserted, so an occurrence whose
    GBIF record changed since the last run (corrected coordinates, updated
    taxonomy, etc.) reflects the latest data instead of silently keeping
    whatever was stored years ago.

    Refuses to replace the window when the fetch yields 0 usable rows
    (ValueError, transaction rolled back, previous data untouched).

    The refresh from GBIF happens once per run, and the job is meant to run
    once a year: the idempotency key includes the year, so a successful run
    blocks any further refresh until the next calendar year.

    Idempotent: guarded by `job_run` (one run per AOI + target year N + AOI
    version + resolution -- skip if already succeeded) and written via
    `upsert(..., update_columns=[])` (`ON CONFLICT DO NOTHING` -- belt and
    suspenders now that the window is cleared before inserting, but still
    the correct call: a GBIF occurrence is otherwise an immutable historical
    record).
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
        ) as (run, session):
            log_ctx = {"aoi": aoi_label, "year": current_year, "run_id": str(run.run_id)}
            # Same reasoning as the old Search API's `geometry` param: the
            # exact polygon is too complex for GBIF, the convex hull is
            # small and always contains it. Safe here too -- occurrences
            # outside the AOI's real shape are still filtered by the
            # zone-membership check below.
            geometry_wkt = geometry_to_gbif_wkt(aoi.geom)
            predicate = build_predicate(geometry_wkt, date_min, date_max)
            creds = load_gbif_credentials()

            try:
                # No DB reads/writes happen between here and wait_for_download
                # returning -- GBIF's own processing time (tens of minutes for
                # a large AOI) would otherwise hold this job_run's transaction
                # open idle-in-transaction for the whole wait. The purge and
                # zone-fetch (below) still run inside this same transaction,
                # just after the wait and archive download instead of before
                # them, so a later failure still rolls them back together
                # with the upserts.
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
                # Fetch the whole zip to disk *before* touching the DB: the
                # window-replace delete below takes row locks, and holding them
                # open during a multi-minute download (and risking a network
                # failure with them held) is avoidable. Past this point the
                # transaction only does local work (parse, filter, upsert).
                zip_path = download_zip(download_key, auth=creds.auth)
                logger.info(
                    "GBIF archive downloaded",
                    extra={
                        "event": "gbif_occurrences.archive_downloaded",
                        "context": {**log_ctx, "download_key": download_key},
                    },
                )
                try:
                    # The window-replace delete and every upsert below (potentially
                    # millions of rows) share this one job_run-managed transaction --
                    # deliberate all-or-nothing: either the whole refetched window
                    # lands cleanly or none of it does (rollback), no partial state.
                    # No record_zone_error/job_run_zone_errors granularity here: the
                    # loop below is per-row within a single AOI-wide fetch, not
                    # per-zone, so there's no natural unit to attribute a partial
                    # failure to -- a failure anywhere fails the whole run, and a
                    # rerun is safe and cheap.
                    logger.info(
                        "replacing staging rows for the refetched window",
                        extra={"event": "gbif_occurrences.replace_window", "context": log_ctx},
                    )
                    session.execute(
                        delete(SpeciesOccurrencesRaw).where(
                            SpeciesOccurrencesRaw.aoi_id == aoi_label,
                            SpeciesOccurrencesRaw.event_date >= date_min,
                            SpeciesOccurrencesRaw.event_date <= date_max,
                        )
                    )

                    valid_zone_ids = set(
                        session.scalars(
                            select(ZonesHex.zone_id).where(ZonesHex.aoi_id == aoi_label)
                        ).all()
                    )

                    rows: list[dict[str, object]] = []
                    n_total = 0
                    n_streamed = 0
                    n_rejected_quality = 0
                    n_outside_zones = 0
                    for raw_row in iter_zip_rows(zip_path, download_key):
                        n_streamed += 1
                        record = _normalize_csv_row(raw_row)
                        quality_row = quality_filter(record, date_min, date_max)
                        if quality_row is None:
                            n_rejected_quality += 1
                            continue
                        zone_id = point_to_h3_cell(
                            quality_row["decimal_latitude"],
                            quality_row["decimal_longitude"],
                            aoi.default_res,
                        )
                        if zone_id not in valid_zone_ids:
                            n_outside_zones += 1
                            continue
                        rows.append({**quality_row, "zone_id": zone_id, "aoi_id": aoi_label})
                        if len(rows) >= UPSERT_BATCH_SIZE:
                            upsert(
                                session=session,
                                model=SpeciesOccurrencesRaw,
                                rows=rows,
                                update_columns=[],
                            )
                            n_total += len(rows)
                            rows = []
                    if rows:
                        upsert(
                            session=session,
                            model=SpeciesOccurrencesRaw,
                            rows=rows,
                            update_columns=[],
                        )
                        n_total += len(rows)

                    if n_total == 0:
                        # The window was already deleted above: committing now
                        # would silently empty it (bad/partial GBIF archive, or
                        # quality filter rejecting everything). Raising rolls the
                        # whole transaction back, the old rows stay in place.
                        raise ValueError(
                            f"GBIF download {download_key} yielded 0 usable rows "
                            f"(streamed={n_streamed}, rejected_quality={n_rejected_quality}, "
                            f"outside_zones={n_outside_zones}); refusing to replace the window"
                        )

                    logger.info(
                        "upserted GBIF occurrences",
                        extra={
                            "event": "gbif_occurrences.upserted",
                            "context": {
                                **log_ctx,
                                "download_key": download_key,
                                "n_streamed": n_streamed,
                                "n_rejected_quality": n_rejected_quality,
                                "n_outside_zones": n_outside_zones,
                                "n_rows": n_total,
                            },
                        },
                    )
                finally:
                    zip_path.unlink(missing_ok=True)
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
