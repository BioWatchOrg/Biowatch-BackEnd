import datetime
import logging
from typing import Any, List

import h3
import pandas as pd

from ..core import AOI, AoiLabel, UndefinedAOIError, aoi_registry, load_aoi, write_parquet

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


class H3GridGenerationError(Exception):
    """Exception raised when there is an error generating the H3 grid."""


def generate_h3_grid(aoi_label: AoiLabel, resolution: int) -> None:
    """
    Generate H3 grid for a given AOI label and resolution.
    """
    # TODO : add to job runs
    if not aoi_registry(aoi_label):
        logger.error(
            f"GEO-H3-generate_h3_grid : AOI with label '{aoi_label}' is not defined in the registry."
        )
        raise UndefinedAOIError(f"AOI with label '{aoi_label}' is not defined in the registry.")

    aoi: AOI
    try:
        aoi = load_aoi(aoi_label)
    except Exception as e:
        logger.error(
            f"GEO-H3-generate_h3_grid : Error loading AOI '{aoi_label}' from registry: {e}"
        )
        raise e

    h3_res: List[Any]

    try:
        h3_poly = h3.geo_to_h3shape(aoi.geom.__geo_interface__)  # type: ignore
        h3_res = h3.h3shape_to_cells(h3_poly, resolution)  # type: ignore
    except Exception as e:
        logger.error(
            f"GEO-H3-generate_h3_grid : Error generating H3 grid for AOI '{aoi_label}' at resolution {resolution}: {e}"
        )
        raise H3GridGenerationError(
            f"Error generating H3 grid for AOI '{aoi_label}' at resolution {resolution}: {e}"
        )

    try:
        data = {"h3_index": h3_res}
        df = pd.DataFrame(data)
        write_parquet(
            df, f"doc/{aoi_label}_h3_grid_res_{resolution}_{datetime.datetime.now()}.parquet"
        )
    except Exception as e:
        logger.error(
            f"GEO-H3-generate_h3_grid : Error writing H3 grid to Parquet file for AOI '{aoi_label}' at resolution {resolution}: {e}"
        )
        raise H3GridGenerationError(
            f"Error writing H3 grid to Parquet file for AOI '{aoi_label}' at resolution {resolution}: {e}"
        )

    # TODO : register  h3_res on zones_hex
