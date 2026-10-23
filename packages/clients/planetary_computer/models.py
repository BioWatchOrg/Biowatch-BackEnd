import datetime
from dataclasses import dataclass, field

import pystac
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from .exceptions import PlanetaryComputerError


@dataclass(frozen=True)
class Scene:
    """
    Une image Sentinel-2 L2A du catalogue Planetary Computer : une tuile MGRS
    (carré de ~110 km) à une date d'acquisition donnée.

    `item` est l'item STAC d'origine, nécessaire à la lecture des pixels ; il
    n'entre pas dans l'égalité entre scènes.
    """

    scene_id: str
    tile: str  # tuile MGRS, ex. "31UDQ"
    acquired_at: datetime.datetime
    baseline: str  # version de traitement ESA, ex. "05.11"
    epsg: int  # CRS natif de la tuile (UTM), ex. 32631
    cloud_cover: float  # % de nuages sur la scène entière
    footprint: BaseGeometry  # emprise réelle des données, WGS84
    item: pystac.Item = field(repr=False, compare=False)

    @classmethod
    def from_item(cls, item: pystac.Item) -> "Scene":
        props = item.properties
        if item.geometry is None:
            raise PlanetaryComputerError(f"STAC item {item.id!r} has no geometry")
        try:
            proj_code = props.get("proj:code") or f"EPSG:{props['proj:epsg']}"
            return cls(
                scene_id=item.id,
                tile=props["s2:mgrs_tile"],
                acquired_at=datetime.datetime.fromisoformat(props["datetime"]),
                baseline=props["s2:processing_baseline"],
                epsg=int(str(proj_code).split(":")[1]),
                cloud_cover=float(props["eo:cloud_cover"]),
                footprint=shape(item.geometry),
                item=item,
            )
        except (KeyError, TypeError, ValueError, IndexError) as e:
            raise PlanetaryComputerError(
                f"Unexpected STAC metadata for item {item.id!r}: {e!r}"
            ) from e
