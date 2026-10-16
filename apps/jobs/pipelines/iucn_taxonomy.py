"""Job `update_iucn_taxonomy` : référentiel IUCN → `staging.iucn_taxa`.

    API IUCN (jeton)
       │ 1. évaluations des espèces présentes dans le pays (pages de 100)
       ▼
    data/<env>/raw/iucn/<version>/<pays>.json   copie brute, réutilisée si présente
       │ 2. normalisation : une ligne par espèce
       ▼
    staging.iucn_taxa                           UPSERT, version = celle de la Red List

La version de la Red List (ex. `2026-1`) est lue sur l'API avant le run : elle
entre dans la clé d'idempotence, donc une nouvelle édition de la Red List relance
le job, et la même édition est sautée si elle a déjà réussi.
"""

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from clients import IucnTaxa, JobAlreadySucceeded, job_run, upsert
from clients.iucn import IucnClient
from core import Env, compute_idempotency_key, data_root, resolve_env
from sqlalchemy import Engine

logger = logging.getLogger(__name__)

JOB_NAME = "update_iucn_taxonomy"
DEFAULT_COUNTRY = "FR"


def raw_path(env: Env, source_version: str, country: str) -> Path:
    """Emplacement de la copie brute d'une version de la Red List pour un pays."""
    return data_root(env) / "raw" / "iucn" / source_version / f"{country}.json"


def normalize_assessments(
    assessments: Iterable[dict[str, Any]], source_version: str
) -> list[dict[str, Any]]:
    """Évaluations brutes de l'API → lignes de `staging.iucn_taxa`.

    - une ligne par espèce (`sis_taxon_id`) : en cas de doublon, l'évaluation
      publiée le plus récemment l'emporte ;
    - nom scientifique nettoyé (espaces) car c'est la clé de jointure avec GBIF ;
    - catégorie en majuscules (`cr` → `CR`) ;
    - les évaluations sans identifiants, nom ou catégorie sont ignorées.
    """
    by_taxon: dict[int, dict[str, Any]] = {}
    for a in assessments:
        taxon_id = a.get("sis_taxon_id")
        name = " ".join(str(a.get("taxon_scientific_name") or "").split())
        category = str(a.get("red_list_category_code") or "").strip().upper()
        assessment_id = a.get("assessment_id")
        if taxon_id is None or assessment_id is None or not name or not category:
            continue
        year = a.get("year_published")
        row: dict[str, Any] = {
            "iucn_taxon_id": int(taxon_id),
            "scientific_name": name,
            "category": category,
            "assessment_id": int(assessment_id),
            "year_published": int(year) if year else None,
            "source_version": source_version,
        }
        current = by_taxon.get(row["iucn_taxon_id"])
        if current is None or (row["year_published"] or 0) > (current["year_published"] or 0):
            by_taxon[row["iucn_taxon_id"]] = row
    return list(by_taxon.values())


def _load_or_fetch(client: IucnClient, path: Path, country: str) -> list[dict[str, Any]]:
    """Lit la copie brute si elle existe, sinon interroge l'API et l'écrit."""
    if path.exists():
        logger.info(
            "reusing raw IUCN file",
            extra={"event": "iucn_taxonomy.raw_reused", "context": {"path": str(path)}},
        )
        data: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        return data
    assessments = list(client.iter_country_assessments(country))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(assessments, ensure_ascii=False), encoding="utf-8")
    logger.info(
        "raw IUCN file written",
        extra={
            "event": "iucn_taxonomy.raw_written",
            "context": {"path": str(path), "n_assessments": len(assessments)},
        },
    )
    return assessments


def update_iucn_taxonomy(
    country: str = DEFAULT_COUNTRY,
    client: IucnClient | None = None,
    engine: Engine | None = None,
) -> None:
    """Importe la Red List IUCN d'un pays dans `staging.iucn_taxa`. Idempotent."""
    country = country.strip().upper()
    env = resolve_env()
    owns_client = client is None
    client = client or IucnClient()
    try:
        source_version = client.red_list_version()
        idempotency_key = compute_idempotency_key(
            job_name=JOB_NAME, scope=country, source_version=source_version, env=env
        )
        log_ctx = {"country": country, "source_version": source_version}
        try:
            with job_run(
                job_name=JOB_NAME,
                scope=country,
                idempotency_key=idempotency_key,
                engine=engine,
            ) as (_, session):
                assessments = _load_or_fetch(
                    client, raw_path(env, source_version, country), country
                )
                rows = normalize_assessments(assessments, source_version)
                upsert(session=session, model=IucnTaxa, rows=rows)
                logger.info(
                    "upserted rows into staging.iucn_taxa",
                    extra={
                        "event": "iucn_taxonomy.upserted",
                        "context": {
                            **log_ctx,
                            "n_assessments": len(assessments),
                            "n_rows": len(rows),
                        },
                    },
                )
        except JobAlreadySucceeded:
            logger.info(
                "IUCN taxonomy already imported, skipping",
                extra={
                    "event": "iucn_taxonomy.skip",
                    "context": {**log_ctx, "idempotency_key": idempotency_key},
                },
            )
    finally:
        if owns_client:
            client.close()
