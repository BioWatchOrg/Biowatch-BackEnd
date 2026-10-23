"""Tests for sentinel.pixels — pure per-pixel Sentinel-2 computations."""

import warnings

import numpy as np
import pytest

from sentinel.pixels import (
    INDEX_NAMES,
    compute_indices,
    dn_to_reflectance,
    monthly_composite,
    normalized_difference,
    offset_for_baseline,
    valid_pixel_mask,
)


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


@pytest.mark.parametrize("scl_class", range(12))
def test_valid_pixel_mask_scl_classes(scl_class):
    expected = scl_class in (4, 5, 6)
    assert bool(valid_pixel_mask(np.array([scl_class]))[0]) is expected


def test_valid_pixel_mask_scl_nan_is_invalid():
    assert not valid_pixel_mask(np.array([np.nan], dtype=np.float32))[0]


def test_valid_pixel_mask_scl_as_float_is_accepted():
    assert valid_pixel_mask(np.array([4.0], dtype=np.float32))[0]


@pytest.mark.parametrize(
    ("reflectance", "expected"),
    [(np.nan, False), (-0.05, False), (0.0, False), (0.0001, True), (1.5, True), (1.51, False)],
)
def test_valid_pixel_mask_reflectance_range(reflectance, expected):
    scl = np.array([4])
    refl = np.array([reflectance], dtype=np.float32)
    assert bool(valid_pixel_mask(scl, refl)[0]) is expected


def test_valid_pixel_mask_requires_every_band_valid():
    scl = np.array([4, 4])
    red = np.array([0.1, 0.1], dtype=np.float32)
    nir = np.array([0.3, np.nan], dtype=np.float32)
    np.testing.assert_array_equal(valid_pixel_mask(scl, red, nir), [True, False])


def test_valid_pixel_mask_keeps_shape():
    scl = np.full((3, 2, 2), 5)
    assert valid_pixel_mask(scl).shape == (3, 2, 2)


def _f32(*values):
    return np.array(values, dtype=np.float32)


def test_normalized_difference_known_values():
    np.testing.assert_allclose(normalized_difference(_f32(0.4, 0.2), _f32(0.1, 0.2)), [0.6, 0.0])


def test_normalized_difference_zero_sum_is_nan():
    assert np.isnan(normalized_difference(_f32(0.0), _f32(0.0))[0])


def test_normalized_difference_propagates_nan():
    assert np.isnan(normalized_difference(_f32(np.nan), _f32(0.1))[0])


def test_compute_indices_formulas():
    green, red, nir, swir = _f32(0.06), _f32(0.04), _f32(0.36), _f32(0.18)
    out = compute_indices(green, red, nir, swir, np.array([True]))
    assert set(out) == set(INDEX_NAMES)
    assert out["ndvi"][0] == pytest.approx(0.8)  # (0.36-0.04)/(0.36+0.04)
    assert out["ndwi"][0] == pytest.approx(-0.714285, abs=1e-5)  # (0.06-0.36)/(0.42)
    assert out["ndbi"][0] == pytest.approx(-0.333333, abs=1e-5)  # (0.18-0.36)/(0.54)
    assert out["swir"][0] == pytest.approx(0.18)


def test_compute_indices_invalid_pixels_are_nan_and_inputs_untouched():
    swir = _f32(0.2, 0.2)
    out = compute_indices(
        _f32(0.1, 0.1), _f32(0.1, 0.1), _f32(0.3, 0.3), swir, np.array([True, False])
    )
    for name in INDEX_NAMES:
        assert not np.isnan(out[name][0])
        assert np.isnan(out[name][1])
    np.testing.assert_array_equal(swir, _f32(0.2, 0.2))


def test_monthly_composite_median_ignores_invalid_dates():
    # 3 dates x 1 pixel ; la date 2 est invalide (nuage) avec une valeur aberrante.
    valid = np.array([[True], [False], [True]])
    ndvi = _f32(0.6, np.nan, 0.8).reshape(3, 1)
    medians, n_obs = monthly_composite({"ndvi": ndvi}, valid)
    assert medians["ndvi"][0] == pytest.approx(0.7)
    assert n_obs[0] == 2


def test_monthly_composite_pixel_without_valid_date():
    valid = np.zeros((2, 1), dtype=bool)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # aucun RuntimeWarning ne doit fuiter
        medians, n_obs = monthly_composite({"ndvi": np.full((2, 1), np.nan, np.float32)}, valid)
    assert np.isnan(medians["ndvi"][0])
    assert n_obs[0] == 0


def test_monthly_composite_shapes():
    valid = np.ones((4, 3, 2), dtype=bool)
    medians, n_obs = monthly_composite({"ndvi": np.zeros((4, 3, 2), np.float32)}, valid)
    assert medians["ndvi"].shape == (3, 2)
    assert n_obs.shape == (3, 2)
    assert int(n_obs.max()) == 4
