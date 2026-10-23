"""Tests for clients.planetary_computer_client.reader — sans réseau.

On écrit de vraies petites images GeoTIFF dans un dossier temporaire et des
items STAC qui pointent dessus : odc-stac et GDAL les lisent exactement comme
les COG distants, ce qui teste tout le chemin de lecture (grille, rééchantillonnage
10 m -> 20 m, types, baselines par date) sans dépendre d'Internet.
"""

import dataclasses
import datetime

import numpy as np
import pystac
import pytest
import rasterio
from affine import Affine
from pyproj import Transformer
from shapely.geometry import box, mapping
from shapely.ops import transform

from clients.planetary_computer_client import PlanetaryComputerError, Scene
from clients.planetary_computer_client.reader import BANDS, open_tile, read_block

EPSG = 32631
ORIGIN_X, ORIGIN_Y = 448_000.0, 5_414_000.0
SIZE_M = 1_200  # tuile de test : 1,2 km de côté
TO_WGS84 = Transformer.from_crs(EPSG, 4326, always_xy=True).transform

# DN constants par date et par bande (B03, B04, B08, B11, SCL).
DATES = [
    (datetime.datetime(2025, 5, 3, 10, 50, tzinfo=datetime.UTC), "05.11", 1000),
    (datetime.datetime(2025, 5, 8, 10, 50, tzinfo=datetime.UTC), "04.00", 3000),
]


def _write_band(path, value, resolution, dtype):
    size = SIZE_M // resolution
    profile = {
        "driver": "GTiff",
        "height": size,
        "width": size,
        "count": 1,
        "dtype": dtype,
        "crs": f"EPSG:{EPSG}",
        "transform": Affine(resolution, 0, ORIGIN_X, 0, -resolution, ORIGIN_Y),
        "nodata": 0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.full((1, size, size), value, dtype=dtype))
    return [size, size], [resolution, 0, ORIGIN_X, 0, -resolution, ORIGIN_Y]


def _make_scene(tmp_path, acquired_at, baseline, dn, tile="31UDQ"):
    tile_box = transform(TO_WGS84, box(ORIGIN_X, ORIGIN_Y - SIZE_M, ORIGIN_X + SIZE_M, ORIGIN_Y))
    item_id = f"S2_{tile}_{acquired_at:%Y%m%d}"
    item = pystac.Item(
        id=item_id,
        geometry=mapping(tile_box),
        bbox=list(tile_box.bounds),
        datetime=acquired_at,
        properties={
            "s2:mgrs_tile": tile,
            "s2:processing_baseline": baseline,
            "proj:code": f"EPSG:{EPSG}",
            "eo:cloud_cover": 5.0,
        },
        collection="sentinel-2-l2a",
    )
    specs = {"B03": (dn, 10), "B04": (dn, 10), "B08": (2 * dn, 10), "B11": (dn, 20), "SCL": (4, 20)}
    for asset, (value, resolution) in specs.items():
        path = tmp_path / f"{item_id}_{asset}.tif"
        dtype = "uint8" if asset == "SCL" else "uint16"
        shape, geotransform = _write_band(path, value, resolution, dtype)
        item.add_asset(
            asset,
            pystac.Asset(
                href=str(path),
                media_type=pystac.MediaType.GEOTIFF,
                roles=["data"],
                extra_fields={"proj:shape": shape, "proj:transform": geotransform},
            ),
        )
    return Scene.from_item(item)


@pytest.fixture
def scenes(tmp_path):
    return [_make_scene(tmp_path, *date) for date in DATES]


@pytest.fixture
def region():
    # Toute la tuile de test, en WGS84.
    return transform(TO_WGS84, box(ORIGIN_X, ORIGIN_Y - SIZE_M, ORIGIN_X + SIZE_M, ORIGIN_Y))


def _no_sign(url):
    return url


def test_open_tile_grid(scenes, region):
    stack = open_tile(scenes, region, chunk_px=16, sign_url=_no_sign)
    assert stack.tile == "31UDQ"
    assert stack.epsg == EPSG
    assert stack.transform.a == 20 and stack.transform.e == -20
    height, width = stack.shape
    assert 55 <= height <= 62 and 55 <= width <= 62  # ~1,2 km à 20 m (+ alignement)


def test_open_tile_baselines_follow_dates(scenes, region):
    stack = open_tile(list(reversed(scenes)), region, sign_url=_no_sign)
    assert stack.baselines == ("05.11", "04.00")  # ordre chronologique


def test_windows_cover_the_grid_once(scenes, region):
    stack = open_tile(scenes, region, chunk_px=16, sign_url=_no_sign)
    covered = np.zeros(stack.shape, dtype=int)
    for ys, xs in stack.windows():
        covered[ys, xs] += 1
    assert np.all(covered == 1)


def test_read_block_values_and_types(scenes, region):
    stack = open_tile(scenes, region, chunk_px=16, sign_url=_no_sign)
    block = read_block(stack, stack.windows()[0], io_threads=2)
    assert set(block) == set(BANDS)
    assert block["red"].shape[0] == 2  # une couche par date
    assert block["red"].dtype == np.uint16
    assert block["scl"].dtype == np.uint8
    # 10 m -> 20 m par moyenne : une bande constante reste constante.
    inside = block["red"][:, 2:-2, 2:-2]
    assert np.all(inside[0] == 1000) and np.all(inside[1] == 3000)
    assert np.all(block["nir"][:, 2:-2, 2:-2][1] == 6000)
    assert np.all(block["scl"][:, 2:-2, 2:-2] == 4)


def test_urls_are_signed_at_read_time(scenes, region):
    signed = []

    def sign(url):
        signed.append(url)
        return url

    stack = open_tile(scenes, region, chunk_px=16, sign_url=sign)
    read_block(stack, stack.windows()[0], io_threads=1)
    assert len(signed) >= len(BANDS)
    assert all(url.endswith(".tif") for url in signed)


def test_read_failure_is_wrapped(scenes, region, tmp_path):
    stack = open_tile(scenes, region, chunk_px=16, sign_url=_no_sign)
    for path in tmp_path.glob("*_B04.tif"):
        path.unlink()
    with pytest.raises(PlanetaryComputerError, match="31UDQ"):
        read_block(stack, stack.windows()[0], io_threads=1)


def test_open_tile_rejects_empty_and_mixed_tiles(tmp_path, region):
    with pytest.raises(ValueError, match="at least one scene"):
        open_tile([], region)
    mixed = [
        _make_scene(tmp_path, *DATES[0]),
        _make_scene(tmp_path, *DATES[1], tile="31UEQ"),
    ]
    with pytest.raises(ValueError, match="single tile"):
        open_tile(mixed, region, sign_url=_no_sign)


def test_dates_mismatch_is_detected(scenes, region):
    # Une scène dont la date annoncée ne correspond pas à l'item chargé.
    shifted = dataclasses.replace(
        scenes[0], acquired_at=scenes[0].acquired_at + datetime.timedelta(hours=1)
    )
    with pytest.raises(PlanetaryComputerError, match="do not match"):
        open_tile([shifted, scenes[1]], region, sign_url=_no_sign)
