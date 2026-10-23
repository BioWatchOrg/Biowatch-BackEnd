"""Raster -> cellules H3 : rattacher les pixels d'une image à des cellules H3.

Fonctions pures (sans réseau ni DB), indépendantes de la source raster : elles
ne connaissent qu'une grille de pixels (CRS, géotransformation, dimensions) et
des cellules H3. Utilisées par l'extraction Sentinel-2 (ADR-004).
"""

import logging
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt
from affine import Affine
from rasterio.features import rasterize

from .h3 import H3Cell, cell_to_polygon
from .metrics import SRID_WGS84, _reproject

logger = logging.getLogger(__name__)

IntArray = npt.NDArray[np.integer]

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
