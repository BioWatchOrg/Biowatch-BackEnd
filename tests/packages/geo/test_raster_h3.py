"""Tests for geo.raster_h3 — attaching raster pixels to H3 cells."""

import h3
import numpy as np
import pandas as pd
import pytest
from affine import Affine
from pyproj import Transformer

from shapely.geometry import box

from geo.raster_h3 import NO_CELL, CellAccumulator, assign_owner_tiles, cells_to_label_grid

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


# Deux "tuiles" (en WGS84) qui se chevauchent sur 2.30 -> 2.40 de longitude.
TILE_WEST = box(2.00, 48.70, 2.40, 49.00)
TILE_EAST = box(2.30, 48.70, 2.70, 49.00)


def _cell(lng, lat):
    return h3.latlng_to_cell(lat, lng, RES)


def test_every_cell_gets_exactly_one_owner():
    cells = [_cell(lng, 48.85) for lng in np.linspace(2.05, 2.65, 30)]
    owners = assign_owner_tiles(cells, {"WEST": TILE_WEST, "EAST": TILE_EAST})
    assert set(owners) == set(cells)
    assert set(owners.values()) == {"WEST", "EAST"}


def test_cell_only_in_one_tile_belongs_to_it():
    owners = assign_owner_tiles([_cell(2.10, 48.85)], {"WEST": TILE_WEST, "EAST": TILE_EAST})
    assert owners == {_cell(2.10, 48.85): "WEST"}


def test_overlap_cell_goes_to_tile_whose_edge_is_farther():
    # 2.32 : à ~0,02° du bord ouest de EAST, à ~0,08° du bord est de WEST.
    cell = _cell(2.32, 48.85)
    assert assign_owner_tiles([cell], {"WEST": TILE_WEST, "EAST": TILE_EAST}) == {cell: "WEST"}
    cell = _cell(2.38, 48.85)
    assert assign_owner_tiles([cell], {"WEST": TILE_WEST, "EAST": TILE_EAST}) == {cell: "EAST"}


def test_tie_is_broken_by_tile_name():
    cell = _cell(2.20, 48.85)
    owners = assign_owner_tiles([cell], {"B": TILE_WEST, "A": TILE_WEST})
    assert owners == {cell: "A"}


def test_cell_outside_every_tile_is_absent():
    assert assign_owner_tiles([MARSEILLE_CELL], {"WEST": TILE_WEST}) == {}


def test_assign_owner_tiles_empty_inputs():
    assert assign_owner_tiles([], {"WEST": TILE_WEST}) == {}
    assert assign_owner_tiles([_cell(2.10, 48.85)], {}) == {}


CELLS = ["c0", "c1", "c2", "c3"]


def _random_image(seed=0, shape=(60, 70)):
    """Image synthétique : étiquettes, deux valeurs avec trous (NaN) et n_obs."""
    rng = np.random.default_rng(seed)
    labels = rng.integers(NO_CELL, len(CELLS), size=shape)  # inclut des pixels sans cellule
    n_obs = rng.integers(0, 5, size=shape)
    ndvi = rng.uniform(-1, 1, size=shape)
    swir = rng.uniform(0, 0.5, size=shape)
    ndvi[n_obs == 0] = np.nan
    swir[n_obs == 0] = np.nan
    ndvi[rng.random(shape) < 0.05] = np.nan  # valeur manquante sur un pixel valide
    return labels, {"ndvi": ndvi, "swir": swir}, n_obs


def _reference(labels, values, n_obs):
    """Calcul direct sur l'image entière, avec pandas."""
    m = labels != NO_CELL
    df = pd.DataFrame(
        {
            "cell": np.array(CELLS)[labels[m]],
            "ndvi": values["ndvi"][m],
            "swir": values["swir"][m],
            "n_obs": n_obs[m],
        }
    )
    valid = df[df.n_obs > 0]
    return pd.DataFrame(
        {
            "ndvi": df.groupby("cell").ndvi.mean(),
            "swir": df.groupby("cell").swir.mean(),
            "mean_n_obs": valid.groupby("cell").n_obs.mean(),
            "n_valid_px": valid.groupby("cell").size(),
            "n_total_px": df.groupby("cell").size(),
        }
    )


def test_blockwise_equals_whole_image():
    """Test clé : cumuler par blocs (de tailles inégales) = calculer sur l'image entière."""
    labels, values, n_obs = _random_image()
    acc = CellAccumulator(CELLS, ["ndvi", "swir"])
    for ys in (slice(0, 25), slice(25, 60)):
        for xs in (slice(0, 10), slice(10, 70)):
            acc.add(labels[ys, xs], {k: v[ys, xs] for k, v in values.items()}, n_obs[ys, xs])

    got = acc.result()
    expected = _reference(labels, values, n_obs)
    pd.testing.assert_frame_equal(
        got.sort_index()[expected.columns],
        expected.rename_axis("zone_id"),
        check_dtype=False,
    )


def test_single_block_equals_whole_image():
    labels, values, n_obs = _random_image(seed=1)
    acc = CellAccumulator(CELLS, ["ndvi", "swir"])
    acc.add(labels, values, n_obs)
    blocks = CellAccumulator(CELLS, ["ndvi", "swir"])
    for ys in (slice(0, 30), slice(30, 60)):
        blocks.add(labels[ys], {k: v[ys] for k, v in values.items()}, n_obs[ys])
    pd.testing.assert_frame_equal(acc.result(), blocks.result())


def test_cell_without_valid_pixel_has_nan_means():
    labels = np.array([[0, 0]])
    acc = CellAccumulator(["c0"], ["ndvi"])
    acc.add(labels, {"ndvi": np.full((1, 2), np.nan)}, np.zeros((1, 2), dtype=int))
    row = acc.result().loc["c0"]
    assert np.isnan(row.ndvi) and np.isnan(row.mean_n_obs)
    assert row.n_valid_px == 0
    assert row.n_total_px == 2


def test_unseen_cells_are_not_in_result():
    acc = CellAccumulator(["c0", "c1"], ["ndvi"])
    acc.add(np.array([[0, NO_CELL]]), {"ndvi": np.array([[0.5, 0.9]])}, np.array([[2, 3]]))
    result = acc.result()
    assert list(result.index) == ["c0"]
    assert result.loc["c0", "ndvi"] == pytest.approx(0.5)
    assert result.loc["c0", "mean_n_obs"] == pytest.approx(2)


def test_empty_accumulator_gives_empty_frame():
    result = CellAccumulator(["c0"], ["ndvi"]).result()
    assert result.empty
    assert result.index.name == "zone_id"
