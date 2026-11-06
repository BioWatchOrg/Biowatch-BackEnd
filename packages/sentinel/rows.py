"""Moyennes par cellule -> lignes de `satellite_features_by_zone`."""

import datetime
import math
from collections.abc import Sequence
from typing import Any

import pandas as pd

from core import BucketId
from geo import H3Cell
from sentinel.pixels import INDEX_NAMES
from sentinel.tile_processing import MASKED_RATIO

# Seuil de `is_valid_data` : en dessous de 10 % de pixels exploitables, la
# valeur est gardée mais marquée peu fiable (le scoring peut l'ignorer).
VALID_PIXEL_RATIO_THRESHOLD = 0.1


def _float_or_none(value: Any) -> float | None:
    number = float(value)
    return None if math.isnan(number) else number


def to_feature_rows(
    cells: Sequence[H3Cell],
    aggregates: pd.DataFrame,
    *,
    bucket_id: BucketId,
    source_version: str,
    computed_at: datetime.datetime,
) -> list[dict[str, object]]:
    """Une ligne par cellule de `cells`, prête pour `upsert`.

    Une cellule absente de `aggregates` (aucune image exploitable sur le mois)
    est écrite quand même, indices NULL, `valid_pixel_ratio = 0` et
    `is_valid_data = false` : le trou est explicite en base. Ne pas passer ici
    les cellules en erreur (tuile illisible) : elles relèvent de
    `record_zone_error`, pas d'une ligne vide.

    - `obs_count` : nombre moyen de dates exploitables par pixel, arrondi.
    - `valid_pixel_ratio` : part des pixels avec au moins une date exploitable.
    - `cloud_score` : part moyenne des dates masquées (nuages, ombres, neige).
    """
    by_cell = aggregates.to_dict("index")
    rows: list[dict[str, object]] = []
    for cell in cells:
        row: dict[str, object] = {
            "zone_id": cell,
            "bucket_id": bucket_id,
            "source_version": source_version,
            "computed_at": computed_at,
        }
        agg = by_cell.get(cell)
        if agg is not None:
            ratio = float(agg["n_valid_px"]) / float(agg["n_total_px"])
            mean_n_obs = _float_or_none(agg["mean_n_obs"])
            row |= {name: _float_or_none(agg[name]) for name in INDEX_NAMES}
            row |= {
                "obs_count": round(mean_n_obs) if mean_n_obs is not None else 0,
                "valid_pixel_ratio": ratio,
                "cloud_score": _float_or_none(agg[MASKED_RATIO]),
            }
        else:
            ratio = 0.0
            row |= {name: None for name in INDEX_NAMES}
            row |= {"obs_count": 0, "valid_pixel_ratio": ratio, "cloud_score": None}
        row["is_valid_data"] = ratio >= VALID_PIXEL_RATIO_THRESHOLD
        rows.append(row)
    return rows
