"""Raster -> cellules H3 : rattacher les pixels d'une image à des cellules H3.

Fonctions pures (sans réseau ni DB), indépendantes de la source raster : elles
ne connaissent qu'une grille de pixels (CRS, géotransformation, dimensions) et
des cellules H3. Utilisées par l'extraction Sentinel-2 (ADR-004).
"""

import logging
from collections.abc import Mapping, Sequence

import numpy as np
import numpy.typing as npt
import pandas as pd
import shapely
from affine import Affine
from rasterio.features import rasterize
from shapely.geometry.base import BaseGeometry

from .h3 import H3Cell, cell_to_centroid, cell_to_polygon
from .metrics import SRID_LAMBERT93, SRID_WGS84, _reproject

logger = logging.getLogger(__name__)

IntArray = npt.NDArray[np.integer]
FloatArray = npt.NDArray[np.floating]

# Valeur des pixels qui n'appartiennent à aucune des cellules demandées.
NO_CELL = -1


def cells_to_label_grid(
    cells: Sequence[H3Cell], epsg: int, transform: Affine, shape: tuple[int, int]
) -> IntArray:
    """Grille d'étiquettes : pour chaque pixel, l'index de sa cellule dans `cells`.

    Les hexagones sont reprojetés dans le CRS de l'image (`epsg`) puis
    "dessinés" sur la grille : un pixel appartient à une cellule si son
    **centre** est dans l'hexagone (règle par défaut de rasterio). Les pixels
    hors de toutes les cellules valent `NO_CELL`.

    Ne passer que les cellules dont cette image est propriétaire
    (`assign_owner_tiles`) : les pixels des autres cellules restent à
    `NO_CELL` et sont ainsi ignorés sans traitement supplémentaire.

    `transform` est la géotransformation affine de la grille (coin haut-gauche,
    taille de pixel) et `shape` ses dimensions `(hauteur, largeur)`.
    """
    if not cells:
        return np.full(shape, NO_CELL, dtype=np.int32)
    shapes = (
        (_reproject(cell_to_polygon(cell), from_srid=SRID_WGS84, to_srid=epsg), index)
        for index, cell in enumerate(cells)
    )
    labels: IntArray = rasterize(
        shapes, out_shape=shape, transform=transform, fill=NO_CELL, dtype="int32"
    )
    return labels


def assign_owner_tiles(
    cells: Sequence[H3Cell], footprints: Mapping[str, BaseGeometry]
) -> dict[H3Cell, str]:
    """Attribue à chaque cellule une seule image ("tuile") propriétaire.

    Des images voisines se chevauchent (~10 km pour les tuiles Sentinel-2) :
    sans règle, une cellule de la zone commune serait comptée deux fois. La
    propriétaire est l'image qui contient le centre de la cellule et dont le
    bord en est le plus éloigné — les bords d'image sont souvent mal couverts.
    À distance égale, la tuile au nom le plus petit gagne (résultat stable).

    `footprints` : emprise de chaque image, en WGS84. Les distances sont
    calculées en Lambert-93 (mètres). Une cellule hors de toute emprise est
    absente du résultat.
    """
    if not cells or not footprints:
        return {}
    names = sorted(footprints)
    areas = [_reproject(footprints[name], SRID_WGS84, SRID_LAMBERT93) for name in names]
    boundaries = shapely.boundary(areas)
    centers = np.array(
        [_reproject(cell_to_centroid(cell), SRID_WGS84, SRID_LAMBERT93) for cell in cells]
    )

    # Paires (cellule, tuile) où le centre est dans la tuile, via un index spatial.
    cell_idx, tile_idx = shapely.STRtree(areas).query(centers, predicate="within")
    distances = shapely.distance(centers[cell_idx], boundaries[tile_idx])

    # Tri par cellule, puis distance décroissante, puis nom de tuile : la
    # première ligne de chaque cellule est sa propriétaire.
    order = np.lexsort((tile_idx, -distances, cell_idx))
    owners: dict[H3Cell, str] = {}
    for i in order:
        owners.setdefault(cells[cell_idx[i]], names[tile_idx[i]])
    return owners


class CellAccumulator:
    """Moyennes par cellule H3, calculées bloc de pixels par bloc.

    Une image entière ne tient pas en mémoire : on la traite par blocs. Une
    cellule pouvant être à cheval sur plusieurs blocs, on ne calcule pas sa
    moyenne bloc par bloc ; on cumule des sommes et des comptes (`add`), puis
    on divise une seule fois à la fin (`result`). Le résultat est identique à
    un calcul sur l'image entière.

    Par cellule :
    - `n_total_px` : pixels de la cellule vus dans l'image ;
    - `n_valid_px` : pixels avec au moins une observation (`n_obs > 0`) ;
    - `<valeur>` : moyenne de chaque valeur sur ses pixels non NaN ;
    - `mean_n_obs` : nombre moyen d'observations par pixel valide.
    """

    def __init__(self, cells: Sequence[H3Cell], value_names: Sequence[str]) -> None:
        self._cells = list(cells)
        self._value_names = list(value_names)
        size = len(self._cells)
        self._n_total = np.zeros(size, dtype=np.int64)
        self._n_valid = np.zeros(size, dtype=np.int64)
        self._sum_n_obs = np.zeros(size, dtype=np.int64)
        self._sums = {name: np.zeros(size, dtype=np.float64) for name in self._value_names}
        self._counts = {name: np.zeros(size, dtype=np.int64) for name in self._value_names}

    def add(self, labels: IntArray, values: Mapping[str, FloatArray], n_obs: IntArray) -> None:
        """Cumule un bloc.

        `labels` : index de la cellule de chaque pixel dans `cells`
        (`NO_CELL` = pixel ignoré), typiquement une fenêtre de
        `cells_to_label_grid`. `values` et `n_obs` ont la même forme que
        `labels` ; une valeur NaN ne compte pas dans la moyenne.
        """
        in_cell = labels != NO_CELL
        cell_of_pixel = labels[in_cell]
        size = len(self._cells)
        pixel_n_obs = n_obs[in_cell]
        self._n_total += np.bincount(cell_of_pixel, minlength=size)
        self._n_valid += np.bincount(cell_of_pixel[pixel_n_obs > 0], minlength=size)
        self._sum_n_obs += np.bincount(cell_of_pixel, weights=pixel_n_obs, minlength=size).astype(
            np.int64
        )
        for name in self._value_names:
            pixel_values = values[name][in_cell]
            has_value = ~np.isnan(pixel_values)
            self._sums[name] += np.bincount(
                cell_of_pixel[has_value], weights=pixel_values[has_value], minlength=size
            )
            self._counts[name] += np.bincount(cell_of_pixel[has_value], minlength=size)

    def result(self) -> pd.DataFrame:
        """Une ligne par cellule vue au moins une fois, indexée par `zone_id`.

        Les moyennes sont NaN pour une cellule sans aucun pixel valide.
        """
        seen = self._n_total > 0
        with np.errstate(divide="ignore", invalid="ignore"):
            columns: dict[str, npt.NDArray[np.generic]] = {
                name: (self._sums[name] / self._counts[name])[seen] for name in self._value_names
            }
            columns["mean_n_obs"] = (self._sum_n_obs / self._n_valid)[seen]
        columns["n_valid_px"] = self._n_valid[seen]
        columns["n_total_px"] = self._n_total[seen]
        index = pd.Index(np.array(self._cells, dtype=object)[seen], name="zone_id")
        return pd.DataFrame(columns, index=index)
