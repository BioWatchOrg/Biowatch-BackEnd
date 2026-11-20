"""Job `update_iucn_taxonomy` : référentiel IUCN → `staging.iucn_taxa`.

    API IUCN (jeton)
       │ 1. évaluations des espèces présentes en France (pages de 100)
       ▼
    data/<env>/raw/iucn/<version>/FR.json   copie brute, réutilisée si complète
       │ 2. normalisation : une ligne par espèce
       ▼
    staging.iucn_taxa                       UPSERT, une ligne par espèce × version

La version de la Red List (ex. `2026-1`) est lue sur l'API avant le run : elle
entre dans la clé d'idempotence, donc une nouvelle édition de la Red List relance
le job, et la même édition est sautée si elle a déjà réussi.

Périmètre MVP : France uniquement (`COUNTRY`). La table n'a pas de colonne pays,
importer un autre pays y mélangerait les espèces : ajouter cette colonne d'abord.
"""

import json
import logging
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from clients import IucnTaxa, JobAlreadySucceeded, job_run, upsert
from clients.iucn import IucnApiError, IucnClient
from core import Env, compute_idempotency_key, data_root, resolve_env
from sqlalchemy import Engine

logger = logging.getLogger(__name__)

JOB_NAME = "update_iucn_taxonomy"
COUNTRY = "FR"

# Format des versions de la Red List (`2026-1`). La version vient de l'API et
# sert à construire un chemin : on refuse tout le reste (`../`, vide…).
_SOURCE_VERSION_RE = re.compile(r"^\d{4}-\d+$")


def raw_path(env: Env, source_version: str) -> Path:
    """Emplacement de la copie brute d'une version de la Red List."""
    if not _SOURCE_VERSION_RE.match(source_version):
        raise IucnApiError(f"Version de Red List inattendue : {source_version!r}.")
    return data_root(env) / "raw" / "iucn" / source_version / f"{COUNTRY}.json"


def normalize_assessments(
    assessments: Iterable[dict[str, Any]], source_version: str
) -> list[dict[str, Any]]:
    """Évaluations brutes de l'API → lignes de `staging.iucn_taxa`.

    - une ligne par espèce (`sis_taxon_id`) : en cas de doublon, l'évaluation
      publiée le plus récemment l'emporte (à année égale, le plus grand
      `assessment_id`, pour ne pas dépendre de l'ordre des pages) ;
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
        if current is None or _recency(row) > _recency(current):
            by_taxon[row["iucn_taxon_id"]] = row
    return list(by_taxon.values())


def _recency(row: dict[str, Any]) -> tuple[int, int]:
    return (row["year_published"] or 0, row["assessment_id"])


def _read_raw(path: Path) -> list[dict[str, Any]] | None:
    """Copie brute si elle est lisible et complète, sinon `None` (fichier supprimé).

    Le fichier porte le nombre d'évaluations vérifié au téléchargement : une
    copie tronquée ou retouchée après coup est rejetée, pas importée.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        data = None
    assessments = data.get("assessments") if isinstance(data, dict) else None
    if (
        isinstance(assessments, list)
        and assessments
        and data.get("total_count") == len(assessments)
    ):
        return assessments
    logger.warning(
        "unreadable or incomplete raw IUCN file, fetching again",
        extra={"event": "iucn_taxonomy.raw_invalid", "context": {"path": str(path)}},
    )
    path.unlink()
    return None


def _write_raw(path: Path, assessments: list[dict[str, Any]]) -> None:
    """Écriture atomique : un process tué en cours d'écriture ne laisse pas de JSON tronqué."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = {"total_count": len(assessments), "assessments": assessments}
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _load_or_fetch(client: IucnClient, path: Path) -> list[dict[str, Any]]:
    """Lit la copie brute si elle existe, sinon interroge l'API et l'écrit."""
    if path.exists():
        data = _read_raw(path)
        if data is not None:
            logger.info(
                "reusing raw IUCN file",
                extra={"event": "iucn_taxonomy.raw_reused", "context": {"path": str(path)}},
            )
            return data
    assessments = list(client.iter_country_assessments(COUNTRY))
    if not assessments:
        raise IucnApiError(f"Aucune évaluation IUCN reçue pour {COUNTRY}.")
    _write_raw(path, assessments)
    logger.info(
        "raw IUCN file written",
        extra={
            "event": "iucn_taxonomy.raw_written",
            "context": {"path": str(path), "n_assessments": len(assessments)},
        },
    )
    return assessments


def update_iucn_taxonomy(
    client: IucnClient | None = None,
    engine: Engine | None = None,
) -> None:
    """Importe la Red List IUCN (espèces de France) dans `staging.iucn_taxa`. Idempotent."""
    env = resolve_env()
    owns_client = client is None
    client = client or IucnClient()
    try:
        # Lue hors de `job_run` car elle fait partie de la clé : si l'API est
        # injoignable ici, aucun run n'est créé (l'erreur reste loggée).
        source_version = client.red_list_version()
        path = raw_path(env, source_version)
        idempotency_key = compute_idempotency_key(
            job_name=JOB_NAME, scope=COUNTRY, source_version=source_version, env=env
        )
        log_ctx = {"country": COUNTRY, "source_version": source_version}
        try:
            with job_run(
                job_name=JOB_NAME,
                scope=COUNTRY,
                idempotency_key=idempotency_key,
                engine=engine,
            ) as (_, session):
                assessments = _load_or_fetch(client, path)
                rows = normalize_assessments(assessments, source_version)
                if not rows:
                    raise IucnApiError(f"Aucune évaluation IUCN exploitable ({path}).")
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
