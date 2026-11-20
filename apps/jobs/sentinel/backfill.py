"""Backfill : enchaîne le job mensuel sur une plage de mois (ADR-004).

Aucune mécanique de reprise propre : chaque mois est un `job_run` idempotent.
Relancer la même commande saute les mois déjà en `success` et refait ceux en
`failed`, `partial` ou interrompus (`running`).
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from core import BucketId
from jobs.sentinel.extract import (
    EXTRACTION_PIPELINE_VERSION,
    MONTHLY_BUCKET_RE,
    ExtractionStatus,
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


@dataclass
class BackfillReport:
    """Bilan d'un backfill : les mois à relancer, par cause."""

    failed: list[BucketId] = field(default_factory=list)  # exception : run `failed`
    partial: list[BucketId] = field(default_factory=list)  # tuile(s) illisible(s)

    @property
    def to_retry(self) -> list[BucketId]:
        """Mois incomplets : relancer la même commande ne refait que ceux-là."""
        return sorted(self.failed + self.partial)


def backfill_sentinel(
    aoi_label: str,
    months: Sequence[BucketId],
    resolution: int,
    *,
    source_version: str = EXTRACTION_PIPELINE_VERSION,
    extract: Callable[..., ExtractionStatus] = extract_sentinel_data_by_aoi,
    **extract_kwargs: object,
) -> BackfillReport:
    """Lance l'extraction pour chaque mois de `months` ; renvoie les mois à relancer.

    `months` : une plage (`month_range("2016-01", "2026-09")`) ou une liste
    ciblée, typiquement `report.to_retry` d'un backfill précédent.

    Un mois en échec (`failed`) ou incomplet (`partial`, une tuile illisible)
    n'arrête pas le backfill : il est journalisé puis on passe au suivant.
    Relancer la même commande ne refait que ces mois (les autres sont sautés).
    """
    for bucket in months:
        if not MONTHLY_BUCKET_RE.fullmatch(bucket):
            raise ValueError(f"bucket mensuel attendu ('YYYY-MM'), reçu {bucket!r}")
    report = BackfillReport()
    log_ctx = {
        "aoi": aoi_label,
        "first": min(months, default=None),
        "last": max(months, default=None),
        "n_months": len(months),
    }
    logger.info(
        "sentinel backfill started", extra={"event": "sentinel.backfill_start", "context": log_ctx}
    )
    for bucket in months:
        try:
            status = extract(
                aoi_label=aoi_label,
                bucket=bucket,
                resolution=resolution,
                source_version=source_version,
                **extract_kwargs,
            )
        except Exception as e:  # frontière d'orchestration : un mois ne bloque pas les autres
            report.failed.append(bucket)
            logger.exception(
                "sentinel backfill month failed",
                extra={
                    "event": "sentinel.backfill_month_error",
                    "context": {**log_ctx, "bucket": bucket, "error": str(e)},
                },
            )
            continue
        if status == "partial":
            report.partial.append(bucket)
            logger.warning(
                "sentinel backfill month incomplete",
                extra={
                    "event": "sentinel.backfill_month_partial",
                    "context": {**log_ctx, "bucket": bucket},
                },
            )
    logger.info(
        "sentinel backfill finished",
        extra={
            "event": "sentinel.backfill_done",
            "context": {
                **log_ctx,
                "n_to_retry": len(report.to_retry),
                "failed": report.failed,
                "partial": report.partial,
            },
        },
    )
    return report
