import json
import logging
import os
from dataclasses import dataclass
from typing import TypeAlias

from shapely.geometry import MultiPolygon, Polygon, shape

logger = logging.getLogger(__name__)


class UndefinedAOIError(Exception):
    """Exception raised when an AOI is not found in the registry."""


class LoadingAOIError(Exception):
    """Exception raised when there is an error loading an AOI from the registry."""


class GeoJsonValueError(Exception):
    """Exception raised when there is an error with the GeoJSON value."""


AoiLabel: TypeAlias = str


@dataclass
class AOI:
    label: AoiLabel
    name: str
    geom: Polygon | MultiPolygon
    bbox: tuple[float, float, float, float]
    version: str
    default_res: int


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = CURRENT_DIR.split("packages")[0]
with open(os.path.join(ROOT_DIR, "packages/core/aoi/aoi_registry.json"), "r") as f:
    aoi_data = json.load(f)


def aoi_registry(label: str) -> bool:
    """
    Check if AOI exists for this label, then return True if it does, False otherwise.
    """
    for aoi_info in aoi_data["aois"]:
        if aoi_info["label"] == label:
            return True
    return False


def load_aoi(aoi_label: AoiLabel) -> AOI:
    """
    Load AOI from the registry and return an AOI object.
    """
    for aoi_info in aoi_data["aois"]:
        if aoi_info["label"] == aoi_label:
            missing = [f for f in ("version", "default_res") if f not in aoi_info]
            if missing:
                logger.error(
                    "AOI missing registry field(s)",
                    extra={
                        "event": "aoi.missing_field",
                        "context": {"aoi": aoi_label, "missing": missing},
                    },
                )
                raise LoadingAOIError(
                    f"AOI '{aoi_label}' missing registry field(s) {missing} in "
                    "aoi_registry.json. Add them to its entry."
                )
            try:
                geojson_path = os.path.join(ROOT_DIR, aoi_info["geojson_path"])
                polygon = _load_polygon(geojson_path=geojson_path)
                return AOI(
                    label=aoi_info["label"],
                    name=aoi_info["name"],
                    geom=polygon,
                    bbox=polygon.bounds,
                    version=aoi_info["version"],
                    default_res=aoi_info["default_res"],
                )
            except Exception as e:
                logger.error(
                    "error loading AOI",
                    extra={
                        "event": "aoi.load_error",
                        "context": {"aoi": aoi_label, "error": str(e)},
                    },
                )
                raise LoadingAOIError(
                    f"Error loading AOI with label '{aoi_label}': {e}"
                ) from e
    logger.error(
        "AOI not found in registry",
        extra={"event": "aoi.not_found", "context": {"aoi": aoi_label}},
    )
    raise UndefinedAOIError(f"AOI with label '{aoi_label}' not found in the registry.")


def _load_polygon(geojson_path: str) -> Polygon | MultiPolygon:
    """
    Load a GeoJSON file and return its content as a Polygon or MultiPolygon.
    """
    with open(geojson_path, "r") as f:
        geojson = json.load(f)
    geom = shape(geojson["geometry"])
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    else:
        logger.error(
            "geometry is not a Polygon or MultiPolygon",
            extra={"event": "aoi.invalid_geometry", "context": {"path": geojson_path}},
        )
        raise GeoJsonValueError(f"Geometry in {geojson_path} is not a Polygon or MultiPolygon.")
