"""D'une tuile Sentinel-2 aux moyennes mensuelles par cellule H3 (ADR-004).

Assemble les briques pures (`sentinel.pixels`, `geo.raster_h3`) autour de la
lecture bloc par bloc (`clients.planetary_computer_client.reader`). La lecture
est injectable : tout ce module se teste sans réseau.
"""

import functools
import warnings
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from clients.planetary_computer_client.reader import TileStack, Window, read_block
from geo import H3Cell
from geo.raster_h3 import CellAccumulator, cells_to_label_grid
from sentinel.pixels import (
    INDEX_NAMES,
    FloatArray,
    IntArray,
    compute_indices,
    dn_to_reflectance,
    monthly_composite,
    offset_for_baseline,
    valid_pixel_mask,
)

# Valeur agrégée en plus des indices : part des dates masquées (nuages, ombres,
# neige...) parmi les dates où le pixel a été photographié -> `cloud_score`.
MASKED_RATIO = "masked_ratio"
VALUE_NAMES = (*INDEX_NAMES, MASKED_RATIO)
# Bandes de réflectance lues, dans l'ordre attendu par `compute_indices`.
INDEX_BANDS = ("green", "red", "nir", "swir")

Block = Mapping[str, npt.NDArray[Any]]
BlockReader = Callable[[TileStack, Window], Block]


def _reflectance(dn: npt.NDArray[Any], baselines: Sequence[str]) -> FloatArray:
    """DN (date, y, x) -> réflectance, avec l'offset propre à chaque date.

    Un même mois peut mélanger deux versions de traitement (ex. janvier 2022) :
    l'offset ne peut pas être unique pour toute la pile.
    """
    return np.stack(
        [
            dn_to_reflectance(dn[i], offset=offset_for_baseline(baseline))
            for i, baseline in enumerate(baselines)
        ]
    )


def block_composite(
    block: Block, baselines: Sequence[str]
) -> tuple[dict[str, FloatArray], IntArray]:
    """Un bloc lu (DN par date) -> composite mensuel par pixel.

    Renvoie les médianes des indices + `masked_ratio` (pixel par pixel), et
    `n_obs`, le nombre de dates exploitables par pixel.
    """
    green, red, nir, swir = (_reflectance(block[band], baselines) for band in INDEX_BANDS)
    scl = block["scl"]
    valid = valid_pixel_mask(scl, green, red, nir, swir)
    medians, n_obs = monthly_composite(compute_indices(green, red, nir, swir, valid), valid)

    # Dates où le pixel a été photographié (SCL 0 = hors fauchée / nodata).
    n_seen = (np.nan_to_num(np.asarray(scl, dtype=np.float32)) > 0).sum(axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # n_seen = 0 -> NaN, voulu
        medians[MASKED_RATIO] = np.where(n_seen > 0, 1.0 - n_obs / n_seen, np.nan)
    return medians, n_obs


def process_tile(
    stack: TileStack,
    owned_cells: Sequence[H3Cell],
    *,
    read: BlockReader = read_block,
) -> pd.DataFrame:
    """Moyennes mensuelles des cellules dont cette tuile est propriétaire.

    Seules `owned_cells` sont dessinées sur la grille : les pixels des autres
    cellules (zones de recouvrement avec les tuiles voisines) sont ignorés.
    Une ligne par cellule vue au moins une fois, indexée par `zone_id` (voir
    `geo.raster_h3.CellAccumulator.result`).
    """
    labels = cells_to_label_grid(owned_cells, stack.epsg, stack.transform, stack.shape)
    accumulator = CellAccumulator(owned_cells, VALUE_NAMES)
    for window in stack.windows():
        block_labels = labels[window]
        if not np.any(block_labels >= 0):
            continue  # bloc sans cellule propriétaire : rien à télécharger
        values, n_obs = block_composite(read(stack, window), stack.baselines)
        accumulator.add(block_labels, values, n_obs)
    return accumulator.result()


def reader_with_threads(io_threads: int) -> BlockReader:
    """`read_block` avec un nombre de lectures parallèles fixé."""
    return functools.partial(read_block, io_threads=io_threads)
