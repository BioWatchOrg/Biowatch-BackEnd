"""Tests for geo.raster_h3 — attaching raster pixels to H3 cells."""

import h3
import numpy as np
from affine import Affine
from pyproj import Transformer

from geo.raster_h3 import NO_CELL, cells_to_label_grid

UTM_31N = 32631
PIXEL_M = 20
# Grille de 200 x 200 pixels de 20 m (4 km de côté) dans Paris, en UTM 31N.
ORIGIN_X, ORIGIN_Y = 448_000, 5_414_000
SHAPE = (200, 200)
TRANSFORM = Affine(PIXEL_M, 0, ORIGIN_X, 0, -PIXEL_M, ORIGIN_Y)
RES = 8


def _pixel_center_cells(shape=SHAPE, transform=TRANSFORM):
    """Cellule H3 du centre de chaque pixel, calculée point par point (référence)."""
    rows, cols = np.indices(shape)
    xs, ys = transform @ (cols + 0.5, rows + 0.5)
    lngs, lats = Transformer.from_crs(UTM_31N, 4326, always_xy=True).transform(xs, ys)
    return np.array(
        [h3.latlng_to_cell(lat, lng, RES) for lat, lng in zip(lats.ravel(), lngs.ravel())]
    ).reshape(shape)


def test_label_grid_matches_pixel_center_rule():
    """Chaque pixel est étiqueté avec la cellule qui contient son centre."""
    reference = _pixel_center_cells()
    cells = sorted(set(reference.ravel()))
    labels = cells_to_label_grid(cells, UTM_31N, TRANSFORM, SHAPE)

    assert labels.shape == SHAPE
    labelled = labels != NO_CELL
    # Hexagones dessinés vs centre de pixel : écarts possibles uniquement pour
    # un pixel dont le centre tombe pile sur une arête (quasi jamais).
    agreement = np.mean(np.array(cells)[labels[labelled]] == reference[labelled])
    assert labelled.mean() > 0.999
    assert agreement > 0.999


def test_pixels_of_unlisted_cells_are_no_cell():
    reference = _pixel_center_cells()
    cells = sorted(set(reference.ravel()))
    kept = cells[: len(cells) // 2]
    labels = cells_to_label_grid(kept, UTM_31N, TRANSFORM, SHAPE)

    in_kept = np.isin(reference, kept)
    assert np.mean(labels[~in_kept] == NO_CELL) > 0.999
    assert np.mean(labels[in_kept] != NO_CELL) > 0.999


MARSEILLE_CELL = h3.latlng_to_cell(43.3, 5.4, RES)


def test_label_grid_indexes_follow_cells_order():
    reference = _pixel_center_cells()
    cell = reference[100, 100]
    labels = cells_to_label_grid([MARSEILLE_CELL, cell], UTM_31N, TRANSFORM, SHAPE)
    assert labels[100, 100] == 1


def test_cell_outside_grid_labels_nothing():
    labels = cells_to_label_grid([MARSEILLE_CELL], UTM_31N, TRANSFORM, SHAPE)
    assert np.all(labels == NO_CELL)


def test_empty_cells_gives_empty_grid():
    labels = cells_to_label_grid([], UTM_31N, TRANSFORM, (3, 4))
    assert labels.shape == (3, 4)
    assert np.all(labels == NO_CELL)
