"""Tests for core.time_bucket — deterministic temporal bucket ids."""

from datetime import date, datetime

import pytest

from core import (
    BucketFormat,
    InvalidDateString,
    UnsupportedBucketFormat,
    UnsupportedDateType,
    biowatch_now,
    bucket_id,
    related_bucket_ids,
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


@pytest.mark.parametrize("bad", ["not-a-date", "2024-13-01", "2024/03/15", ""])
def test_invalid_iso_string_raises(bad):
    with pytest.raises(InvalidDateString):
        bucket_id(bad, BucketFormat.MONTHLY)


def test_invalid_iso_string_is_a_value_error():
    # InvalidDateString subclasses ValueError -> generic handlers still catch it
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


# --- related_bucket_ids ----------------------------------------------------


def test_related_of_year_contains_self_bimesters_and_months():
    related = related_bucket_ids("2024")
    assert related[0] == "2024"  # self always first
    for b in range(1, 7):
        assert f"2024-b{b:02d}" in related
    for m in range(1, 13):
        assert f"2024-{m:02d}" in related
    # self + 6 bimesters + 12 months (no ancestors for a year)
    assert len(related) == 1 + 6 + 12


def test_related_of_month_is_self_bimester_and_year():
    # month -> its enclosing bimester + its year
    assert related_bucket_ids("2024-03") == ["2024-03", "2024-b02", "2024"]


def test_related_of_month_maps_to_correct_bimester():
    assert related_bucket_ids("2024-01") == ["2024-01", "2024-b01", "2024"]
    assert related_bucket_ids("2024-02") == ["2024-02", "2024-b01", "2024"]
    assert related_bucket_ids("2024-12") == ["2024-12", "2024-b06", "2024"]


def test_related_of_bimester_is_self_two_months_and_year():
    # bimester -> its 2 months + its year
    assert related_bucket_ids("2024-b02") == ["2024-b02", "2024-03", "2024-04", "2024"]


def test_related_of_first_and_last_bimester():
    assert related_bucket_ids("2024-b01") == ["2024-b01", "2024-01", "2024-02", "2024"]
    assert related_bucket_ids("2024-b06") == ["2024-b06", "2024-11", "2024-12", "2024"]


def test_related_always_include_self_first():
    for b in ("2024", "2024-03", "2024-b02"):
        assert related_bucket_ids(b)[0] == b


def test_related_buckets_are_valid_bucket_ids():
    # every produced bucket must parse back (no garbage)
    for b in related_bucket_ids("2024"):
        assert related_bucket_ids(b)[0] == b


def test_related_is_consistent_across_formats():
    # a month, its bimester and its year all reference the same year bucket
    month = related_bucket_ids("2024-03")
    bimester = related_bucket_ids("2024-b02")
    assert "2024" in month
    assert "2024" in bimester
    assert "2024-b02" in month  # month knows its bimester
    assert "2024-03" in bimester  # bimester knows its month


@pytest.mark.parametrize(
    "bad",
    [
        "abcd",  # non-numeric year
        "2024-13",  # month out of range
        "2024-00",  # month out of range
        "2024-b07",  # bimester out of range
        "2024-b00",  # bimester out of range
        "2024/03",  # wrong separator
        "24-03",  # short year
        "2024-3",  # unpadded month
        "",
    ],
)
def test_related_bucket_ids_rejects_malformed(bad):
    with pytest.raises(UnsupportedBucketFormat):
        related_bucket_ids(bad)


def test_no_arg_defaults_to_current_date():
    assert bucket_id() == bucket_id(biowatch_now())
