"""Lecture des pixels d'une tuile Sentinel-2, bloc par bloc, depuis les COG.

Rien n'est téléchargé à l'ouverture (`open_tile`) : seuls les blocs demandés à
`read_block` sont lus, en lecture partielle HTTP des fichiers COG.
"""

import datetime
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import dask
import numpy as np
import numpy.typing as npt
import odc.stac
import planetary_computer
import xarray as xr
from affine import Affine
from rasterio.errors import RasterioError
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from .exceptions import PlanetaryComputerError
from .models import Scene

logger = logging.getLogger(__name__)

# Rôle de la bande dans le calcul -> nom de l'asset Planetary Computer.
BANDS = {"green": "B03", "red": "B04", "nir": "B08", "swir": "B11", "scl": "SCL"}

# Résolution de travail : celle de la bande la plus grossière (B11, SCL). Les
# bandes à 10 m sont moyennées, la classification SCL n'est jamais interpolée.
RESOLUTION_M = 20
DEFAULT_CHUNK_PX = 1024
# Lectures COG parallèles à l'intérieur d'un bloc : le temps est dominé par la
# latence réseau, pas par le CPU (spike ADR-004 : 18 min / 1,8 Go pour l'IDF).
DEFAULT_IO_THREADS = 16

_RESAMPLING = {
    **{asset: "average" for role, asset in BANDS.items() if role != "scl"},
    BANDS["scl"]: "nearest",
}
# Planetary Computer ne déclare pas le type des pixels (`raster:bands`) : sans
# ça, odc-stac chargerait du float32 (2x plus de mémoire). DN = 0 est le nodata.
_STAC_CFG = {
    "sentinel-2-l2a": {
        "assets": {
            **{asset: {"data_type": "uint16", "nodata": 0} for asset in BANDS.values()},
            BANDS["scl"]: {"data_type": "uint8", "nodata": 0},
        }
    }
}

Window = tuple[slice, slice]


@dataclass(frozen=True)
class TileStack:
    """Une tuile ouverte : sa grille de pixels et ses dates, sans données lues.

    `transform`, `shape` et `epsg` décrivent la grille commune à toutes les
    dates (entrée de `geo.raster_h3.cells_to_label_grid`). `baselines[i]` est
    la version de traitement de la date `i`, pour appliquer le bon offset.
    """

    tile: str
    epsg: int
    transform: Affine
    shape: tuple[int, int]  # (hauteur, largeur) en pixels
    baselines: tuple[str, ...]
    chunk_px: int
    dataset: xr.Dataset = field(repr=False, compare=False)

    def windows(self) -> list[Window]:
        """Découpage de la grille en blocs de `chunk_px` x `chunk_px` pixels."""
        height, width = self.shape
        return [
            (slice(y, min(y + self.chunk_px, height)), slice(x, min(x + self.chunk_px, width)))
            for y in range(0, height, self.chunk_px)
            for x in range(0, width, self.chunk_px)
        ]


def open_tile(
    scenes: Sequence[Scene],
    region: BaseGeometry,
    *,
    chunk_px: int = DEFAULT_CHUNK_PX,
    sign_url: Callable[[str], str] = planetary_computer.sign_url,
) -> TileStack:
    """Prépare la lecture d'une tuile limitée à `region` (WGS84), sans rien télécharger.

    `scenes` : les scènes d'une **seule** tuile, déjà dédoublonnées
    (`deduplicate_scenes`) — une date = une scène. La grille reste dans le CRS
    natif de la tuile (UTM), sans reprojection. Les URL sont signées au moment
    de chaque lecture (`sign_url`), jamais à l'avance : un jeton ne peut pas
    expirer pendant un long run.
    """
    if not scenes:
        raise ValueError("open_tile needs at least one scene")
    tiles = {scene.tile for scene in scenes}
    if len(tiles) != 1:
        raise ValueError(f"open_tile expects scenes of a single tile, got {sorted(tiles)}")
    tile, epsg = scenes[0].tile, scenes[0].epsg
    # Ordre chronologique imposé : avec groupby="id", odc-stac garde l'ordre
    # d'entrée. Les baselines sont reprises des scènes (odc-stac convertirait
    # "05.10" en 5.1 via with_properties) puis vérifiées contre les dates chargées.
    ordered = sorted(scenes, key=lambda scene: scene.acquired_at)

    # Retry des lectures HTTP confié à GDAL (pas de mécanisme maison).
    odc.stac.configure_rio(cloud_defaults=True, GDAL_HTTP_MAX_RETRY=5, GDAL_HTTP_RETRY_DELAY=2)
    dataset = odc.stac.load(
        [scene.item for scene in ordered],
        bands=list(BANDS.values()),
        groupby="id",  # une date = une scène, déjà dédoublonnée
        crs=f"EPSG:{epsg}",
        resolution=RESOLUTION_M,
        geopolygon=mapping(region),
        chunks={"x": chunk_px, "y": chunk_px},
        resampling=_RESAMPLING,
        stac_cfg=_STAC_CFG,
        patch_url=sign_url,
    )
    _check_dates_match(tile, dataset, ordered)
    geobox = dataset.odc.geobox
    stack = TileStack(
        tile=tile,
        epsg=epsg,
        transform=geobox.affine,
        shape=(int(geobox.shape.y), int(geobox.shape.x)),
        baselines=tuple(scene.baseline for scene in ordered),
        chunk_px=chunk_px,
        dataset=dataset,
    )
    logger.info(
        "planetary computer tile opened",
        extra={
            "event": "planetary_computer.tile_opened",
            "context": {
                "tile": tile,
                "n_dates": len(stack.baselines),
                "shape": stack.shape,
                "n_blocks": len(stack.windows()),
            },
        },
    )
    return stack


def _check_dates_match(tile: str, dataset: xr.Dataset, ordered: Sequence[Scene]) -> None:
    """Garde-fou : la date i chargée doit être la scène i, sinon l'offset de
    réflectance serait appliqué à la mauvaise date."""
    loaded = [np.datetime64(t, "s") for t in dataset.time.values]
    expected = [
        np.datetime64(scene.acquired_at.astimezone(datetime.UTC).replace(tzinfo=None), "s")
        for scene in ordered
    ]
    if loaded != expected:
        raise PlanetaryComputerError(
            f"Loaded dates of tile {tile} do not match its scenes: {loaded} != {expected}"
        )


def read_block(
    stack: TileStack, window: Window, *, io_threads: int = DEFAULT_IO_THREADS
) -> dict[str, npt.NDArray[Any]]:
    """Télécharge un bloc : {rôle: tableau (date, y, x)} pour green/red/nir/swir/scl.

    Les valeurs sont les DN bruts (uint16, 0 = nodata ; SCL en uint8) : la
    conversion en réflectance se fait côté calcul, date par date, avec
    `stack.baselines`.
    """
    ys, xs = window
    try:
        with dask.config.set(scheduler="threads", num_workers=io_threads):
            block = stack.dataset.isel(y=ys, x=xs).compute()
    except (RasterioError, OSError) as e:
        logger.error(
            "planetary computer block read failed",
            extra={
                "event": "planetary_computer.read_error",
                "context": {"tile": stack.tile, "window": str(window), "error": str(e)},
            },
        )
        raise PlanetaryComputerError(f"Failed to read tile {stack.tile} {window}: {e}") from e
    return {role: np.asarray(block[asset].values) for role, asset in BANDS.items()}
