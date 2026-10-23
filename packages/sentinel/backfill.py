"""Backfill : enchaîne le job mensuel sur une plage de mois (ADR-004).

Aucune mécanique de reprise propre : chaque mois est un `job_run` idempotent.
Relancer la même commande saute les mois déjà en `success` et refait ceux en
`failed`, `partial` ou interrompus (`running`).
"""

import logging
from collections.abc import Callable

from core import BucketId
from sentinel.sentinel_extract import (
    MONTHLY_BUCKET_RE,
    EXTRACTION_PIPELINE_VERSION,
    extract_sentinel_data_by_aoi,
)

logger = logging.getLogger(__name__)


def month_range(first: BucketId, last: BucketId) -> list[BucketId]:
    """Mois de `first` à `last` inclus, ex. ("2025-11", "2026-02") -> 4 mois."""
    for bucket in (first, last):
        if not MONTHLY_BUCKET_RE.fullmatch(bucket):
            raise ValueError(f"bucket mensuel attendu ('YYYY-MM'), reçu {bucket!r}")
    year, month = (int(part) for part in first.split("-"))
    end = tuple(int(part) for part in last.split("-"))
    if (year, month) > end:
        raise ValueError(f"plage vide : {first} est après {last}")
    months: list[BucketId] = []
    while (year, month) <= end:
        months.append(f"{year}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def backfill_sentinel(
    aoi_label: str,
    first: BucketId,
    last: BucketId,
    resolution: int,
    *,
    source_version: str = EXTRACTION_PIPELINE_VERSION,
    extract: Callable[..., None] = extract_sentinel_data_by_aoi,
    **extract_kwargs: object,
) -> list[BucketId]:
    """Lance l'extraction mois par mois ; renvoie les mois en échec.

    Un mois en échec n'arrête pas le backfill : il est journalisé (et son
    `job_run` reste `failed`), puis on passe au suivant. Relancer la commande
    reprend exactement les mois manquants.
    """
    months = month_range(first, last)
    failed: list[BucketId] = []
    log_ctx = {"aoi": aoi_label, "first": first, "last": last, "n_months": len(months)}
    logger.info(
        "sentinel backfill started", extra={"event": "sentinel.backfill_start", "context": log_ctx}
    )
    for bucket in months:
        try:
            extract(
                aoi_label=aoi_label,
                bucket=bucket,
                resolution=resolution,
                source_version=source_version,
                **extract_kwargs,
            )
        except Exception as e:  # frontière d'orchestration : un mois ne bloque pas les autres
            failed.append(bucket)
            logger.exception(
                "sentinel backfill month failed",
                extra={
                    "event": "sentinel.backfill_month_error",
                    "context": {**log_ctx, "bucket": bucket, "error": str(e)},
                },
            )
    logger.info(
        "sentinel backfill finished",
        extra={
            "event": "sentinel.backfill_done",
            "context": {**log_ctx, "n_failed": len(failed), "failed": failed},
        },
    )
    return failed
