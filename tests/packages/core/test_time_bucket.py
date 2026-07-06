"""Tests for core.time_bucket — deterministic temporal bucket ids."""

from datetime import date, datetime

import pytest

from core import (
    BucketFormat,
    UnsupportedBucketFormat,
    UnsupportedDateType,
    biowatch_now,
    bucket_id,
)


# --- Monthly ---------------------------------------------------------------


def test_monthly_format():
    assert bucket_id(date(2024, 3, 15), BucketFormat.MONTHLY) == "2024-03"


def test_monthly_is_default_format():
    assert bucket_id(date(2024, 3, 15)) == "2024-03"


@pytest.mark.parametrize(
    "day",
    [28, 29, 30, 31],  # end-of-month lengths across Feb/Apr/Jan
)
def test_monthly_end_of_month_stays_in_month(day):
    # 2024 is a leap year -> Feb 29 exists
    assert bucket_id(date(2024, 1, day), BucketFormat.MONTHLY) == "2024-01"


def test_monthly_feb_non_leap_year():
    assert bucket_id(date(2023, 2, 28), BucketFormat.MONTHLY) == "2023-02"


def test_monthly_feb_leap_year():
    assert bucket_id(date(2024, 2, 29), BucketFormat.MONTHLY) == "2024-02"


# --- Yearly ----------------------------------------------------------------


def test_yearly_format():
    assert bucket_id(date(2024, 7, 6), BucketFormat.YEARLY) == "2024"


def test_year_change_boundary():
    dec = bucket_id(date(2023, 12, 31), BucketFormat.YEARLY)
    jan = bucket_id(date(2024, 1, 1), BucketFormat.YEARLY)
    assert dec == "2023"
    assert jan == "2024"
    assert dec != jan


# --- Bimonthly -------------------------------------------------------------


@pytest.mark.parametrize(
    "month,expected",
    [
        (1, "2024-b01"),
        (2, "2024-b01"),  # jan/feb -> same bucket
        (3, "2024-b02"),
        (4, "2024-b02"),
        (5, "2024-b03"),
        (6, "2024-b03"),
        (7, "2024-b04"),
        (8, "2024-b04"),
        (9, "2024-b05"),
        (10, "2024-b05"),
        (11, "2024-b06"),
        (12, "2024-b06"),
    ],
)
def test_bimonthly_pairs(month, expected):
    assert bucket_id(date(2024, month, 15), BucketFormat.BIMONTHLY) == expected


def test_bimonthly_jan_feb_same_bucket():
    assert bucket_id(date(2024, 1, 1), BucketFormat.BIMONTHLY) == bucket_id(
        date(2024, 2, 28), BucketFormat.BIMONTHLY
    )


def test_bimonthly_does_not_collide_with_monthly():
    # bimonthly is prefixed with 'b' so it never equals a monthly id
    monthly = bucket_id(date(2024, 3, 1), BucketFormat.MONTHLY)
    bimonthly = bucket_id(date(2024, 3, 1), BucketFormat.BIMONTHLY)
    assert monthly == "2024-03"
    assert bimonthly == "2024-b02"
    assert monthly != bimonthly


# --- Input coercion --------------------------------------------------------


def test_accepts_datetime():
    assert bucket_id(datetime(2024, 3, 15, 23, 59), BucketFormat.MONTHLY) == "2024-03"


def test_accepts_iso_string():
    assert bucket_id("2024-03-15", BucketFormat.MONTHLY) == "2024-03"


def test_accepts_iso_datetime_string():
    assert bucket_id("2024-03-15T23:59:00", BucketFormat.MONTHLY) == "2024-03"


def test_equivalent_inputs_produce_same_bucket():
    d = bucket_id(date(2024, 3, 15))
    dt = bucket_id(datetime(2024, 3, 15, 12, 0))
    s = bucket_id("2024-03-15")
    assert d == dt == s


# --- Determinism -----------------------------------------------------------


def test_is_deterministic():
    assert bucket_id(date(2024, 3, 15)) == bucket_id(date(2024, 3, 15))


# --- Errors ----------------------------------------------------------------


def test_invalid_iso_string_raises():
    with pytest.raises(ValueError):
        bucket_id("not-a-date", BucketFormat.MONTHLY)


@pytest.mark.parametrize("bad", [42, 3.14, ["2024-01-01"], {"y": 2024}])
def test_unsupported_type_raises(bad):
    with pytest.raises(UnsupportedDateType):
        bucket_id(bad, BucketFormat.MONTHLY)


def test_unsupported_format_raises():
    with pytest.raises(UnsupportedBucketFormat):
        bucket_id(date(2024, 3, 15), "weekly")  # type: ignore[arg-type]  # not a BucketFormat member


# --- biowatch_now ----------------------------------------------------------


def test_biowatch_now_returns_a_date():
    assert isinstance(biowatch_now(), date)


def test_none_falls_back_to_current_date():
    assert bucket_id(None) == bucket_id(biowatch_now())


def test_no_arg_defaults_to_current_date():
    assert bucket_id() == bucket_id(biowatch_now())
