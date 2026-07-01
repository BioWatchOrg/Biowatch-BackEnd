import json
import os
from dataclasses import dataclass


@dataclass
class AOI:
    label: str
    name: str
    geojson: str
    default_res: int


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = CURRENT_DIR.split("packages")[0]
with open(os.path.join(ROOT_DIR, "packages/core/aoi/aoi_registry.json"), "r") as f:
    aoi_data = json.load(f)


def aoi_registry(label: str) -> AOI | None:
    """
    Check if AOI exists for this label, then return AOI object or None
    """
    for aoi_info in aoi_data["aois"]:
        if aoi_info["label"] == label:
            return AOI(
                label=aoi_info["label"],
                name=aoi_info["name"],
                geojson=aoi_info["geojson_path"],
                default_res=aoi_info["default_res"],
            )
    return None
