"""Calcul des indices Sentinel-2 (ADR-004) : fonctions pures, sans I/O.

L'orchestration (lecture des images, écriture en base, job_run) vit dans
apps/jobs/sentinel, conformément au CLAUDE.md.
"""

from .rows import VALID_PIXEL_RATIO_THRESHOLD, to_feature_rows
from .tile_processing import TileGrid, Window, block_composite, process_tile

__all__ = [
    "to_feature_rows",
    "VALID_PIXEL_RATIO_THRESHOLD",
    "process_tile",
    "block_composite",
    "TileGrid",
    "Window",
]
