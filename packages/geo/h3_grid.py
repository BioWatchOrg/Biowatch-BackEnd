import logging
import os
from typing import TypeAlias

import h3
import pandas as pd
from clients import JobAlreadySucceeded, ZonesHex, job_run, upsert
from core import (
    AOI,
    AoiLabel,
    UndefinedAOIError,
    aoi_registry,
    compute_idempotency_key,
    load_aoi,
    write_parquet,
)
from geoalchemy2.shape import from_shape
from shapely.geometry import Point, Polygon, box
from sqlalchemy import Engine

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

GRID_STORE_ROOT = "docs/grids"

H3Cell: TypeAlias = str  # H3 cell id (hex string)


class H3GridGenerationError(Exception):
    """Exception raised when there is an error generating the H3 grid."""


def compute_h3_cells(aoi_label: AoiLabel, resolution: int) -> list[H3Cell]:
    """
    Pure, deterministic H3 coverage of an AOI.

    Given the same aoi_label + resolution (+ AOI geometry version), returns the
    exact same set of H3 cell ids. No I/O, no side effects — this is the piece
    under test for the determinism requirement.
    """
    if not aoi_registry(aoi_label):
        logger.error(
            f"GEO-H3-compute_h3_cells : AOI with label '{aoi_label}' is not defined in the registry."
        )
        raise UndefinedAOIError(f"AOI with label '{aoi_label}' is not defined in the registry.")

    aoi: AOI = load_aoi(aoi_label)

    try:
        h3_poly = h3.geo_to_h3shape(aoi.geom.__geo_interface__)  # type: ignore
        # Règle de bordure FIGÉE : centroid-in-polygon. `h3shape_to_cells` retient
        # une cellule ssi son centre tombe dans le polygone (mode containment par
        # défaut de h3 v4). Ne pas changer sans bump de version d'AOI : la règle
        # détermine quelles cellules de bord entrent dans la grille.
        return h3.h3shape_to_cells(h3_poly, resolution)  # type: ignore
    except Exception as e:
        logger.error(
            f"GEO-H3-compute_h3_cells : Error generating H3 grid for AOI '{aoi_label}' at resolution {resolution}: {e}"
        )
        raise H3GridGenerationError(
            f"Error generating H3 grid for AOI '{aoi_label}' at resolution {resolution}: {e}"
        )


def _cell_to_geometries(cell: H3Cell) -> tuple[Polygon, Point, Polygon]:
    """H3 cell → (polygone, centroïde, bbox) shapely."""
    try:
        boundary = h3.cell_to_boundary(cell)
        # h3 retourne (lat, lng) ; shapely veut (x=lng, y=lat).
        polygon = Polygon([(lng, lat) for lat, lng in boundary])

        lat, lng = h3.cell_to_latlng(cell)
        centroid = Point(lng, lat)

        bbox = box(*polygon.bounds)

        return polygon, centroid, bbox
    except Exception as e:
        logger.error(
            f"GEO-H3-_cell_to_geometries : Error converting H3 cell '{cell}' to geometries: {e}"
        )
        raise H3GridGenerationError(f"Error converting H3 cell '{cell}' to geometries: {e}")


def generate_h3_grid(
    aoi_label: AoiLabel, resolution: int | None = None, engine: Engine | None = None
) -> None:
    """
    Generate the H3 grid for an AOI + resolution and persist it into `zones_hex`.

    Idempotent: guarded by `job_run` (same aoi + resolution + aoi version ⇒
    skip if already succeeded) and written via `upsert` (no duplicates on re-run).

    The AOI is resolved up front so the run is keyed on its declared `version`
    (registry source of truth) rather than a hardcoded constant: bump the AOI
    version in the registry when its geometry changes and the grid is recomputed.
    Validating the AOI before `job_run` also avoids recording a run for an
    unknown/broken AOI.

    `resolution=None` falls back to the AOI's `default_res` from the registry.
    """
    aoi: AOI = load_aoi(aoi_label)
    if resolution is None:
        resolution = aoi.default_res

    idempotency_key = compute_idempotency_key(
        job_name="generate_h3_grid",
        scope=aoi_label,
        resolution=resolution,
        source_version=aoi.version,
    )
    try:
        with job_run(
            job_name="generate_h3_grid",
            scope=aoi_label,
            idempotency_key=idempotency_key,
            engine=engine,
        ) as (_, session):
            logger.info(
                'generate_h3_grid : generating H3 grid for AOI "%s" at resolution %s',
                aoi_label,
                resolution,
            )
            cells = compute_h3_cells(aoi_label, resolution)

            logger.info(
                'generate_h3_grid : computed %s H3 cells for AOI "%s" at resolution %s',
                len(cells),
                aoi_label,
                resolution,
            )
            rows: list[dict[str, object]] = []
            for cell in cells:
                polygon, centroid, bbox = _cell_to_geometries(cell)
                rows.append(
                    {
                        "zone_id": cell,
                        "resolution": resolution,
                        "geom": from_shape(polygon, srid=4326),
                        "centroid": from_shape(centroid, srid=4326),
                        "bbox": from_shape(bbox, srid=4326),
                        "aoi_id": aoi_label,
                        "aoi_version": aoi.version,
                    }
                )

            logger.info(
                'generate_h3_grid : upserting %s rows into zones_hex for AOI "%s" at resolution %s',
                len(rows),
                aoi_label,
                resolution,
            )
            upsert(session=session, model=ZonesHex, rows=rows)

            logger.info(
                'generate_h3_grid : successfully upserted %s rows into zones_hex for AOI "%s" at resolution %s',
                len(rows),
                aoi_label,
                resolution,
            )
            # Artefact grille (liste des cellules) pour reproductibilité / debug.
            # Partitionné par version d'AOI : deux versions ne s'écrasent pas.
            out_dir = os.path.join(
                GRID_STORE_ROOT,
                f"aoi={aoi_label}",
                f"version={aoi.version}",
                f"res={resolution}",
            )
            os.makedirs(out_dir, exist_ok=True)
            write_parquet(pd.DataFrame({"h3_index": cells}), os.path.join(out_dir, "grid.parquet"))

    except JobAlreadySucceeded:
        logger.info("generate_h3_grid : déjà généré pour %s res=%s, skip", aoi_label, resolution)
        return
