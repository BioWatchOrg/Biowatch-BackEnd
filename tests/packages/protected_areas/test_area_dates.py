"""Tests pour protected_areas.dates et protected_areas.versions — règles de date et de version."""

import datetime

import pytest
from protected_areas import (
    PIPELINE_VERSION,
    PROTECTED_AREA_TYPES,
    ZNIEFF_DEFAULT_VALID_FROM,
    build_source_version,
    edition_of,
    site_valid_from,
)

# --- valid_from ----------------------------------------------------------------


def test_reserve_uses_its_creation_date():
    assert site_valid_from("reserve", {"DateCreaENP": "2009-10-22"}) == datetime.date(2009, 10, 22)


@pytest.mark.parametrize("value", ["", "  ", "NULL", "null", None, "pas une date", "2009-13-45"])
def test_reserve_without_a_readable_date_has_no_valid_from(value):
    assert site_valid_from("reserve", {"DateCreaENP": value}) is None
    assert site_valid_from("reserve", {}) is None


def test_natura_zps_uses_the_first_designation_not_the_latest_decree():
    """`spa_date` porte le dernier arrêté : seule `spa_date_init` date le début de protection."""
    attrs = {"type": "A", "spa_date_init": "2006-04-06", "spa_date": "2018-03-23"}
    assert site_valid_from("natura2000", attrs) == datetime.date(2006, 4, 6)


def test_natura_zps_ignores_the_habitat_dates():
    attrs = {"type": "A", "spa_date_init": "", "sac_date_init": "2010-05-18"}
    assert site_valid_from("natura2000", attrs) is None


def test_natura_sic_falls_back_on_confirmation_then_proposal():
    dates = {
        "sac_date_init": "2010-05-18",
        "date_first_con": "2004-12-07",
        "date_prop": "1999-03-31",
    }
    assert site_valid_from("natura2000", {"type": "b", **dates}) == datetime.date(2010, 5, 18)
    dates["sac_date_init"] = ""
    assert site_valid_from("natura2000", {"type": "B", **dates}) == datetime.date(2004, 12, 7)
    dates["date_first_con"] = "NULL"
    assert site_valid_from("natura2000", {"type": "B", **dates}) == datetime.date(1999, 3, 31)
    dates["date_prop"] = ""
    assert site_valid_from("natura2000", {"type": "B", **dates}) is None


def test_natura_site_of_unknown_type_takes_the_first_date_available():
    attrs = {"type": "C", "spa_date_init": "", "sac_date_init": "2012-01-02"}
    assert site_valid_from("natura2000", attrs) == datetime.date(2012, 1, 2)
    assert site_valid_from("natura2000", {}) is None


def test_znieff_reads_the_date_part_of_the_database_timestamp():
    attrs = {"DATE_CREA": "20100608150317"}
    assert site_valid_from("znieff_type1", attrs) == datetime.date(2010, 6, 8)


def test_znieff_without_record_gets_the_default_date_instead_of_being_dropped():
    assert site_valid_from("znieff_type1", {}) == ZNIEFF_DEFAULT_VALID_FROM
    assert site_valid_from("znieff_type1", {"DATE_CREA": ""}) == ZNIEFF_DEFAULT_VALID_FROM


# --- source_version --------------------------------------------------------------


def test_source_version_carries_type_archive_date_and_pipeline_version():
    version = build_source_version("reserve", datetime.date(2026, 11, 6))
    assert version == f"inpn_reserve_2026-11-06_v{PIPELINE_VERSION}"


@pytest.mark.parametrize("area_type", PROTECTED_AREA_TYPES)
def test_edition_is_read_back_from_a_source_version(area_type):
    edition = datetime.date(2025, 11, 28)
    assert edition_of(build_source_version(area_type, edition)) == edition


@pytest.mark.parametrize(
    "source_version",
    ["", "geofabrik_2016-01-01", "inpn_reserve_2026-11-06", "inpn_x_2026-13-40_v1"],
)
def test_edition_of_a_foreign_or_broken_version_is_none(source_version):
    assert edition_of(source_version) is None
