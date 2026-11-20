"""Tests for sentinel.rows.to_feature_rows."""

import datetime

import numpy as np
import pandas as pd
import pytest

from sentinel.rows import to_feature_rows

NOW = datetime.datetime(2026, 10, 23, tzinfo=datetime.UTC)


def _aggregates(**overrides):
    row = {
        "ndvi": 0.7,
        "ndwi": -0.6,
        "ndbi": -0.3,
        "swir": 0.2,
        "masked_ratio": 0.25,
        "mean_n_obs": 4.4,
        "n_valid_px": 1600,
        "n_total_px": 1800,
    } | overrides
    return pd.DataFrame([row], index=pd.Index(["cellA"], name="zone_id"))


def _rows(cells, aggregates):
    return to_feature_rows(
        cells, aggregates, bucket_id="2025-05", source_version="2.0.0", computed_at=NOW
    )


def test_observed_cell():
    (row,) = _rows(["cellA"], _aggregates())
    assert row == {
        "zone_id": "cellA",
        "bucket_id": "2025-05",
        "source_version": "2.0.0",
        "computed_at": NOW,
        "ndvi": 0.7,
        "ndwi": -0.6,
        "ndbi": -0.3,
        "swir": 0.2,
        "obs_count": 4,
        "valid_pixel_ratio": pytest.approx(1600 / 1800),
        "cloud_score": 0.25,
        "is_valid_data": True,
    }


def test_cell_without_any_image_is_written_as_explicit_gap():
    (row,) = _rows(["cellZ"], _aggregates())
    assert row["ndvi"] is None and row["swir"] is None
    assert row["obs_count"] == 0
    assert row["valid_pixel_ratio"] == 0.0
    assert row["cloud_score"] is None
    assert row["is_valid_data"] is False


def test_cell_seen_but_always_cloudy():
    aggregates = _aggregates(
        ndvi=np.nan,
        ndwi=np.nan,
        ndbi=np.nan,
        swir=np.nan,
        mean_n_obs=np.nan,
        masked_ratio=1.0,
        n_valid_px=0,
    )
    (row,) = _rows(["cellA"], aggregates)
    assert row["ndvi"] is None
    assert row["obs_count"] == 0
    assert row["valid_pixel_ratio"] == 0.0
    assert row["cloud_score"] == 1.0
    assert row["is_valid_data"] is False


@pytest.mark.parametrize(("n_valid", "expected"), [(179, False), (180, True)])
def test_is_valid_data_threshold(n_valid, expected):
    (row,) = _rows(["cellA"], _aggregates(n_valid_px=n_valid))  # sur 1800 pixels
    assert row["is_valid_data"] is expected


def test_values_are_python_floats():
    (row,) = _rows(["cellA"], _aggregates(ndvi=np.float32(0.5)))
    assert type(row["ndvi"]) is float
