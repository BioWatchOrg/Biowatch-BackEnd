"""Tests for sentinel.pixels — pure per-pixel Sentinel-2 computations."""

import numpy as np
import pytest

from sentinel.pixels import dn_to_reflectance, offset_for_baseline


@pytest.mark.parametrize(
    ("baseline", "expected"),
    [
        ("02.12", 0.0),
        ("03.00", 0.0),
        ("03.99", 0.0),
        ("04.00", -0.1),
        ("05.11", -0.1),
        ("10.00", -0.1),  # comparaison numérique, pas lexicographique
    ],
)
def test_offset_for_baseline(baseline, expected):
    assert offset_for_baseline(baseline) == expected


def test_dn_to_reflectance_applies_scale_and_offset():
    out = dn_to_reflectance(np.array([1000, 3000], dtype=np.uint16), offset=-0.1)
    np.testing.assert_allclose(out, [0.0, 0.2], atol=1e-6)
    assert out.dtype == np.float32


def test_dn_to_reflectance_without_offset():
    np.testing.assert_allclose(dn_to_reflectance([2500]), [0.25], atol=1e-6)


def test_dn_to_reflectance_nodata_becomes_nan():
    out = dn_to_reflectance(np.array([0.0, np.nan, 1500.0]))
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2] == pytest.approx(0.15)


def test_dn_to_reflectance_does_not_mutate_input():
    dn = np.array([0.0, 1500.0], dtype=np.float32)
    dn_to_reflectance(dn)
    np.testing.assert_array_equal(dn, [0.0, 1500.0])
