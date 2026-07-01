import logging
import os

import h3
import pandas as pd
from core import AOI, AoiLabel, UndefinedAOIError, aoi_registry, load_aoi, write_parquet

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

GRID_STORE_ROOT = "processed/grids"


class H3GridGenerationError(Exception):
    """Exception raised when there is an error generating the H3 grid."""


def compute_h3_cells(aoi_label: AoiLabel, resolution: int) -> list[str]:
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
        return h3.h3shape_to_cells(h3_poly, resolution)  # type: ignore
    except Exception as e:
        logger.error(
            f"GEO-H3-compute_h3_cells : Error generating H3 grid for AOI '{aoi_label}' at resolution {resolution}: {e}"
        )
        raise H3GridGenerationError(
            f"Error generating H3 grid for AOI '{aoi_label}' at resolution {resolution}: {e}"
        )


def generate_h3_grid(aoi_label: AoiLabel, resolution: int) -> None:
    """
    Generate H3 grid for a given AOI label and resolution, then persist it.
    """
    # TODO : add to job runs
    cells = compute_h3_cells(aoi_label, resolution)

    try:
        df = pd.DataFrame({"h3_index": cells})
        out_dir = os.path.join(GRID_STORE_ROOT, f"aoi={aoi_label}", f"res={resolution}")
        os.makedirs(out_dir, exist_ok=True)
        write_parquet(df, os.path.join(out_dir, "grid.parquet"))
    except Exception as e:
        logger.error(
            f"GEO-H3-generate_h3_grid : Error writing H3 grid to Parquet file for AOI '{aoi_label}' at resolution {resolution}: {e}"
        )
        raise H3GridGenerationError(
            f"Error writing H3 grid to Parquet file for AOI '{aoi_label}' at resolution {resolution}: {e}"
        )

    # TODO : register  h3_res on zones_hex
