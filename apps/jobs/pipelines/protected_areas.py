"""Job `update_protected_areas` : référentiels INPN → `protected_areas_by_zone` (ADR-009).

    Archive INPN (Natura 2000, espaces protégés, ZNIEFF)
       │ 1. HEAD : date `Last-Modified` → source_version (archive inchangée ⇒ skip)
       │ 2. téléchargement, lecture des sites qui touchent l'AOI (clients.inpn)
       ▼
    sites datés (Lambert-93)                  packages/protected_areas, calcul pur
       │ 3. par zone H3 : fusion des sites, part de la zone couverte, date la plus ancienne
       │ 4. zones couvertes dans l'édition précédente et absentes de celle-ci → fermées
       ▼
    protected_areas_by_zone                   UPSERT, un état complet par source_version

Un `job_run` par type et par AOI : une source en échec ne bloque pas les deux
autres. La table n'a pas de `bucket_id` : une protection est un intervalle
(`valid_from` → `valid_to`), pas une mesure par période.
"""

import datetime
import json
import logging
import shutil
import time
from collections.abc import Sequence
from typing import Literal

import httpx
import shapely
from clients import (
    DatabaseConfigError,
    JobAlreadySucceeded,
    ProtectedAreasByZone,
    job_run,
    previous_protected_area_rows,
    record_zone_error,
    session_scope,
    upsert,
    zone_ids_for_aoi,
)
from clients.inpn import INPN_SOURCES, InpnSite, archive_last_modified, download_archive
from core import AoiLabel, compute_idempotency_key, data_root, load_aoi, resolve_env
from geo import cell_to_polygon, reproject
from protected_areas import (
    DEFAULT_MAX_CLOSED_SHARE,
    PROTECTED_AREA_TYPES,
    DatedSite,
    PreviousRow,
    ProtectedAreaType,
    build_source_version,
    carry_over_closed_rows,
    compute_zone_coverage,
    edition_of,
    site_valid_from,
    to_area_rows,
)
from sqlalchemy import Engine

logger = logging.getLogger(__name__)

JOB_NAME = "update_protected_areas"

# "partial" = au moins une zone en erreur (dans job_run_zone_errors, sans ligne) ;
# "failed" = le type n'a rien écrit. Les deux sont à relancer.
RunStatus = Literal["success", "partial", "skipped", "failed"]


class ProtectedAreasError(Exception):
    """Le job ne peut pas tourner tel quel (grille H3 absente, type inconnu)."""


def _scope(aoi_label: AoiLabel, area_type: ProtectedAreaType) -> str:
    return f"{aoi_label}:{area_type}"


def _date_sites(
    area_type: ProtectedAreaType, sites: Sequence[InpnSite]
) -> tuple[list[DatedSite], int]:
    """Sites datés, et nombre de sites écartés faute de date (`valid_from` est obligatoire)."""
    dated: list[DatedSite] = []
    for site in sites:
        valid_from = site_valid_from(area_type, site.attrs)
        if valid_from is not None:
            dated.append(DatedSite(site_id=site.site_id, geom=site.geom, valid_from=valid_from))
    return dated, len(sites) - len(dated)


def _update_one(
    aoi_label: AoiLabel,
    area_type: ProtectedAreaType,
    resolution: int,
    max_closed_share: float,
    engine: Engine | None,
    client: httpx.Client | None,
) -> RunStatus:
    source = INPN_SOURCES[area_type]
    # Lue hors de `job_run` car elle fait partie de la clé : si l'INPN est
    # injoignable ici, aucun run n'est créé (l'erreur est loggée par l'appelant).
    edition = archive_last_modified(source.archive_url, client)
    source_version = build_source_version(area_type, edition)
    scope = _scope(aoi_label, area_type)
    idempotency_key = compute_idempotency_key(
        job_name=JOB_NAME, scope=scope, source_version=source_version, resolution=resolution
    )
    log_ctx = {
        "aoi": aoi_label,
        "type": area_type,
        "resolution": resolution,
        "source_version": source_version,
        "idempotency_key": idempotency_key,
    }
    try:
        with job_run(
            job_name=JOB_NAME, scope=scope, idempotency_key=idempotency_key, engine=engine
        ) as (run, session):
            started = time.monotonic()
            # Session courte et séparée : celle du job ne doit pas rester ouverte
            # en transaction pendant un téléchargement de plusieurs minutes.
            with session_scope(engine) as read_session:
                zone_ids = zone_ids_for_aoi(read_session, aoi_label, resolution)
            if not zone_ids:
                raise ProtectedAreasError(
                    f"Aucune zone dans zones_hex pour l'AOI '{aoi_label}' à la résolution "
                    f"{resolution} : lancer d'abord generate_h3_grid."
                )
            zones = {zone_id: reproject(cell_to_polygon(zone_id)) for zone_id in zone_ids}
            xmin, ymin, xmax, ymax = shapely.total_bounds(list(zones.values())).tolist()

            work_dir = data_root(resolve_env()) / "inpn" / f"{aoi_label}_{area_type}"
            shutil.rmtree(work_dir, ignore_errors=True)  # reste d'un run tué en cours de route
            try:
                archive = download_archive(source.archive_url, work_dir, client)
                sites = source.read_sites(archive, work_dir, (xmin, ymin, xmax, ymax))
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

            dated, n_undated = _date_sites(area_type, sites)
            coverage, errors = compute_zone_coverage(zones, dated)
            for zone_id, message in errors.items():
                record_zone_error(run.run_id, zone_id, message, engine)

            previous_version, previous = previous_protected_area_rows(
                session, area_type, aoi_label, resolution, source_version
            )
            # Même archive que l'édition précédente (seul le calcul a changé) :
            # une zone qui disparaît vient du calcul, pas de la source → pas de fermeture.
            same_archive = previous_version is not None and edition_of(previous_version) == edition
            closed, n_closed = carry_over_closed_rows(
                coverage,
                [
                    PreviousRow(row.zone_id, row.valid_from, row.valid_to, row.coverage_ratio)
                    for row in previous
                ],
                closed_on=None if same_archive else edition,
                failed_zones=errors.keys(),
                max_closed_share=max_closed_share,
            )
            rows = to_area_rows(
                coverage,
                closed,
                area_type=area_type,
                source_version=source_version,
                computed_at=datetime.datetime.now(datetime.UTC),
            )
            upsert(session, ProtectedAreasByZone, rows)

            run.output_ref = json.dumps(
                {"source_version": source_version, "n_rows": len(rows), "n_closed": n_closed}
            )
            logger.info(
                "upserted rows into protected_areas_by_zone",
                extra={
                    "event": "protected_areas.upserted",
                    "context": {
                        **log_ctx,
                        "previous_source_version": previous_version,
                        "n_zones": len(zones),
                        "n_sites": len(sites),
                        "n_sites_undated": n_undated,
                        "n_zones_covered": len(coverage),
                        "n_zones_closed": n_closed,
                        "n_rows_closed_carried": len(closed) - n_closed,
                        "n_zone_errors": len(errors),
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                },
            )
    except JobAlreadySucceeded:
        logger.info(
            "protected areas already up to date for this archive, skipping",
            extra={"event": "protected_areas.skip", "context": log_ctx},
        )
        return "skipped"
    return "partial" if errors else "success"


def update_protected_areas(
    aoi_label: AoiLabel,
    area_types: Sequence[str] | None = None,
    resolution: int | None = None,
    max_closed_share: float = DEFAULT_MAX_CLOSED_SHARE,
    engine: Engine | None = None,
    client: httpx.Client | None = None,
) -> dict[ProtectedAreaType, RunStatus]:
    """
    Met à jour `protected_areas_by_zone` pour une AOI, à partir des archives de l'INPN.

    Chaque type (`natura2000`, `reserve`, `znieff_type1`) est traité dans son
    propre `job_run` ; le statut de chacun est renvoyé. Une archive inchangée
    depuis le dernier run réussi est sautée sans téléchargement (`skipped`).

    Args:
        aoi_label: label de l'AOI (voir packages/core/aoi/aoi_registry.json).
        area_types: types à traiter. Par défaut : les trois.
        resolution: résolution H3. Par défaut : le `default_res` de l'AOI.
        max_closed_share: part maximale des zones de l'édition précédente
            qu'une nouvelle édition peut fermer (garde-fou, voir
            `protected_areas.carry_over_closed_rows`). 1 le désactive.
    """
    aoi = load_aoi(aoi_label)  # AOI inconnue : échec avant tout appel réseau
    resolution = resolution if resolution is not None else aoi.default_res
    selected = list(area_types) if area_types else list(PROTECTED_AREA_TYPES)
    unknown = [area_type for area_type in selected if area_type not in PROTECTED_AREA_TYPES]
    if unknown:
        raise ProtectedAreasError(
            f"Type(s) d'aire protégée inconnu(s) : {unknown} (attendu : {PROTECTED_AREA_TYPES})."
        )

    statuses: dict[ProtectedAreaType, RunStatus] = {}
    for area_type in PROTECTED_AREA_TYPES:
        if area_type not in selected:
            continue
        try:
            statuses[area_type] = _update_one(
                aoi_label, area_type, resolution, max_closed_share, engine, client
            )
        except DatabaseConfigError:
            raise  # config cassée : inutile d'essayer les autres types, l'entrypoint la rapporte
        except Exception as e:
            # Une source en échec ne bloque pas les autres ; `job_run` a déjà
            # marqué le run `failed` si l'erreur est survenue dans son bloc.
            statuses[area_type] = "failed"
            logger.error(
                "protected areas update failed for this type, continuing with the others",
                extra={
                    "event": "protected_areas.type_failed",
                    "context": {"aoi": aoi_label, "type": area_type, "error": str(e)},
                },
            )
    return statuses
