"""update_gbif_occurrences job: GBIF occurrences → staging.species_occurrences_raw.

Fetches GBIF occurrences for an AOI over a rolling 10-year window, applies a
quality filter, converts points to H3 `zone_id`, and upserts them into
`staging.species_occurrences_raw`. Runs once a year: for year N, the window
is [N-9, N].
"""

import logging
from datetime import date

from clients import (
    GbifApiError,
    JobAlreadySucceeded,
    SpeciesOccurrencesRaw,
    ZonesHex,
    fetch_occurrences,
    job_run,
    upsert,
)
from core import AOI, AoiLabel, biowatch_now, compute_idempotency_key, load_aoi
from geo import point_to_h3_cell
from sqlalchemy import Engine, delete, select

from species.quality import quality_filter

logger = logging.getLogger(__name__)

GBIF_SOURCE_VERSION = "gbif-search-v1"
WINDOW_YEARS = 10


def rolling_window(current_year: int) -> tuple[date, date]:
    """Return (date_min, date_max) for the 10-year window ending at current_year."""
    return date(current_year - (WINDOW_YEARS - 1), 1, 1), date(current_year, 12, 31)


def update_gbif_occurrences(aoi_label: AoiLabel, engine: Engine | None = None) -> None:
    """
    Fetch, filter, and store GBIF occurrences for an AOI's rolling 10-year window.

    Idempotent: guarded by `job_run` (one run per AOI + target year N ⇒ skip if
    already succeeded) and written via `upsert(..., update_columns=[])` (`ON
    CONFLICT DO NOTHING` — a GBIF occurrence is an immutable historical
    record, re-running never needs to overwrite an existing row).
    """
    aoi: AOI = load_aoi(aoi_label)
    current_year = biowatch_now().year
    date_min, date_max = rolling_window(current_year)

    idempotency_key = compute_idempotency_key(
        job_name="update_gbif_occurrences",
        scope=aoi_label,
        period=str(current_year),
        source_version=GBIF_SOURCE_VERSION,
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
            geometry_wkt = aoi.geom.wkt

            for year in range(date_min.year, date_max.year + 1):
                rows: list[dict[str, object]] = []
                try:
                    for record in fetch_occurrences(geometry_wkt, year):
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
                except GbifApiError:
                    logger.error(
                        "GBIF fetch failed for year",
                        extra={
                            "event": "gbif_occurrences.fetch_error",
                            "context": {**log_ctx, "fetch_year": year},
                        },
                    )
                    raise

                upsert(
                    session=session,
                    model=SpeciesOccurrencesRaw,
                    rows=rows,
                    update_columns=[],
                )
                logger.info(
                    "upserted GBIF occurrences for year",
                    extra={
                        "event": "gbif_occurrences.upserted",
                        "context": {**log_ctx, "fetch_year": year, "n_rows": len(rows)},
                    },
                )

    except JobAlreadySucceeded:
        logger.info(
            "GBIF occurrences already up to date, skipping",
            extra={
                "event": "gbif_occurrences.skip",
                "context": {"aoi": aoi_label, "idempotency_key": idempotency_key},
            },
        )
        return
