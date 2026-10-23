"""Tests for sentinel.tile_processing — sans réseau (lecture simulée)."""

import h3
import numpy as np
import pytest
from affine import Affine

from clients.planetary_computer_client.reader import TileStack
from sentinel.tile_processing import MASKED_RATIO, block_composite, process_tile

# Deux dates, deux versions de traitement : la même réflectance est encodée
# différemment (offset -1000 DN à partir de la baseline 04.00).
BASELINES = ("03.00", "04.00")


def _block(shape=(1, 1), *, scl=(4, 4), red=(1000, 2000), nir=(4000, 5000)):
    """Bloc (date, y, x) de DN constants par date."""

    def stack(values):
        return np.stack([np.full(shape, v, dtype=np.uint16) for v in values])

    return {
        "green": stack((500, 1500)),
        "red": stack(red),
        "nir": stack(nir),
        "swir": stack((2000, 3000)),
        "scl": np.stack([np.full(shape, v, dtype=np.uint8) for v in scl]),
    }


def test_offset_is_applied_per_date():
    # Date 1 : rouge 0.1 / pir 0.4 sans offset ; date 2 : idem après offset -0.1.
    medians, n_obs = block_composite(_block(), BASELINES)
    assert n_obs[0, 0] == 2
    assert medians["ndvi"][0, 0] == pytest.approx(0.6, abs=1e-5)  # (0.4-0.1)/(0.4+0.1)
    assert medians["swir"][0, 0] == pytest.approx(0.2, abs=1e-5)


def test_cloudy_date_is_excluded_and_counted_as_masked():
    medians, n_obs = block_composite(_block(scl=(4, 9), nir=(4000, 9000)), BASELINES)
    assert n_obs[0, 0] == 1
    assert medians["ndvi"][0, 0] == pytest.approx(0.6, abs=1e-5)  # date nuageuse ignorée
    assert medians[MASKED_RATIO][0, 0] == pytest.approx(0.5)


def test_pixel_never_photographed():
    medians, n_obs = block_composite(_block(scl=(0, 0)), BASELINES)
    assert n_obs[0, 0] == 0
    assert np.isnan(medians["ndvi"][0, 0])
    assert np.isnan(medians[MASKED_RATIO][0, 0])


def test_pixel_photographed_but_always_cloudy():
    medians, n_obs = block_composite(_block(scl=(8, 9)), BASELINES)
    assert n_obs[0, 0] == 0
    assert np.isnan(medians["ndvi"][0, 0])
    assert medians[MASKED_RATIO][0, 0] == pytest.approx(1.0)


# Grille de 100 x 100 pixels de 20 m dans Paris (UTM 31N), blocs de 32 px.
EPSG = 32631
TRANSFORM = Affine(20, 0, 448_000, 0, -20, 5_414_000)
SHAPE = (100, 100)


def _stack(chunk_px=32):
    return TileStack(
        tile="31UDQ",
        epsg=EPSG,
        transform=TRANSFORM,
        shape=SHAPE,
        baselines=BASELINES,
        chunk_px=chunk_px,
        dataset=None,  # type: ignore[arg-type]
    )


def _fake_read(calls):
    def read(stack, window):
        calls.append(window)
        ys, xs = window
        return _block(shape=(ys.stop - ys.start, xs.stop - xs.start))

    return read


def _center_cell():
    # Cellule qui contient le centre de la grille.
    from pyproj import Transformer

    lng, lat = Transformer.from_crs(EPSG, 4326, always_xy=True).transform(449_000, 5_413_000)
    return h3.latlng_to_cell(lat, lng, 8)


def test_process_tile_averages_owned_cells():
    cell = _center_cell()
    calls = []
    result = process_tile(_stack(), [cell], read=_fake_read(calls))
    assert list(result.index) == [cell]
    row = result.loc[cell]
    assert row.ndvi == pytest.approx(0.6, abs=1e-5)
    assert row.n_valid_px == row.n_total_px > 1000  # ~0,74 km2 / 400 m2
    assert row.mean_n_obs == pytest.approx(2)
    assert row[MASKED_RATIO] == pytest.approx(0)


def test_process_tile_skips_blocks_without_owned_cells():
    calls = []
    process_tile(_stack(), [_center_cell()], read=_fake_read(calls))
    assert 0 < len(calls) < len(_stack().windows())


def test_process_tile_without_owned_cells_reads_nothing():
    calls = []
    result = process_tile(_stack(), [], read=_fake_read(calls))
    assert calls == []
    assert result.empty


def test_reader_with_threads_binds_io_threads():
    from sentinel.tile_processing import read_block, reader_with_threads

    reader = reader_with_threads(4)
    assert reader.func is read_block
    assert reader.keywords == {"io_threads": 4}
