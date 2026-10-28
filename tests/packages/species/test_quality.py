"""Tests for species.quality.quality_filter — pure GBIF record validation."""

from datetime import date

from species.quality import quality_filter

DATE_MIN = date(2016, 1, 1)
DATE_MAX = date(2025, 12, 31)

VALID_RECORD = {
    "key": 123,
    "scientificName": "Vulpes vulpes",
    "taxonKey": 5219243,
    "decimalLatitude": 48.85,
    "decimalLongitude": 2.35,
    "eventDate": "2020-06-15T00:00:00",
}


def test_valid_record_is_kept():
    row = quality_filter(VALID_RECORD, DATE_MIN, DATE_MAX)
    assert row == {
        "gbif_occurrence_id": "123",
        "event_date": date(2020, 6, 15),
        "gbif_taxon_key": 5219243,
        "scientific_name": "Vulpes vulpes",
        "decimal_latitude": 48.85,
        "decimal_longitude": 2.35,
    }


def test_missing_key_is_rejected():
    record = {**VALID_RECORD}
    del record["key"]
    assert quality_filter(record, DATE_MIN, DATE_MAX) is None


def test_missing_scientific_name_is_rejected():
    record = {**VALID_RECORD, "scientificName": None}
    assert quality_filter(record, DATE_MIN, DATE_MAX) is None


def test_null_coordinates_are_rejected():
    record = {**VALID_RECORD, "decimalLatitude": None}
    assert quality_filter(record, DATE_MIN, DATE_MAX) is None


def test_null_island_is_rejected():
    record = {**VALID_RECORD, "decimalLatitude": 0, "decimalLongitude": 0}
    assert quality_filter(record, DATE_MIN, DATE_MAX) is None


def test_out_of_range_latitude_is_rejected():
    record = {**VALID_RECORD, "decimalLatitude": 91.0}
    assert quality_filter(record, DATE_MIN, DATE_MAX) is None


def test_out_of_range_longitude_is_rejected():
    record = {**VALID_RECORD, "decimalLongitude": -181.0}
    assert quality_filter(record, DATE_MIN, DATE_MAX) is None


def test_missing_event_date_is_rejected():
    record = {**VALID_RECORD}
    del record["eventDate"]
    assert quality_filter(record, DATE_MIN, DATE_MAX) is None


def test_unparseable_event_date_is_rejected():
    record = {**VALID_RECORD, "eventDate": "not-a-date"}
    assert quality_filter(record, DATE_MIN, DATE_MAX) is None


def test_event_date_before_window_is_rejected():
    record = {**VALID_RECORD, "eventDate": "2010-01-01T00:00:00"}
    assert quality_filter(record, DATE_MIN, DATE_MAX) is None


def test_event_date_after_window_is_rejected():
    record = {**VALID_RECORD, "eventDate": "2030-01-01T00:00:00"}
    assert quality_filter(record, DATE_MIN, DATE_MAX) is None


def test_event_date_at_window_boundaries_is_kept():
    at_min = {**VALID_RECORD, "eventDate": DATE_MIN.isoformat() + "T00:00:00"}
    at_max = {**VALID_RECORD, "eventDate": DATE_MAX.isoformat() + "T00:00:00"}
    assert quality_filter(at_min, DATE_MIN, DATE_MAX) is not None
    assert quality_filter(at_max, DATE_MIN, DATE_MAX) is not None


def test_missing_taxon_key_is_kept_as_none():
    record = {**VALID_RECORD}
    del record["taxonKey"]
    row = quality_filter(record, DATE_MIN, DATE_MAX)
    assert row is not None
    assert row["gbif_taxon_key"] is None
