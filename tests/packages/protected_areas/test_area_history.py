"""Tests pour protected_areas.history et protected_areas.rows — fermetures et lignes écrites."""

import datetime

import pytest
from protected_areas import (
    PreviousRow,
    TooManyClosuresError,
    ZoneCoverage,
    carry_over_closed_rows,
    to_area_rows,
)

D1990 = datetime.date(1990, 1, 1)
D2005 = datetime.date(2005, 3, 2)
EDITION_2 = datetime.date(2026, 11, 6)
EDITION_1 = datetime.date(2025, 10, 1)


def _open(zone_id, valid_from=D1990, ratio=0.4):
    return PreviousRow(zone_id=zone_id, valid_from=valid_from, valid_to=None, coverage_ratio=ratio)


def _covered(valid_from=D1990, ratio=0.4):
    return ZoneCoverage(coverage_ratio=ratio, valid_from=valid_from)


def test_first_import_closes_nothing():
    assert carry_over_closed_rows({"z1": _covered()}, [], closed_on=EDITION_2) == ([], 0)


def test_zone_still_covered_is_not_carried_over():
    carried = carry_over_closed_rows({"z1": _covered()}, [_open("z1")], closed_on=EDITION_2)
    assert carried == ([], 0)


def test_zone_no_longer_covered_is_closed_at_the_new_edition_date():
    carried, n_closed = carry_over_closed_rows(
        {"z1": _covered()}, [_open("z1"), _open("z2", D2005, 0.75)], closed_on=EDITION_2
    )
    assert carried == [
        PreviousRow(zone_id="z2", valid_from=D2005, valid_to=EDITION_2, coverage_ratio=0.75)
    ]
    assert n_closed == 1


def test_already_closed_rows_are_carried_from_edition_to_edition():
    closed = PreviousRow("z2", D2005, EDITION_1, 0.75)
    carried, n_closed = carry_over_closed_rows({}, [closed], closed_on=EDITION_2)
    assert carried == [closed]  # la date de fermeture d'origine est conservée
    assert n_closed == 0


def test_reopened_zone_with_the_same_start_replaces_its_closed_row():
    """Même clé primaire (zone, type, valid_from) : la ligne ouverte l'emporte, pas de doublon."""
    closed = PreviousRow("z2", D2005, EDITION_1, 0.75)
    assert carry_over_closed_rows({"z2": _covered(D2005)}, [closed], closed_on=EDITION_2) == (
        [],
        0,
    )


def test_reopened_zone_with_another_start_keeps_its_closed_period():
    closed = PreviousRow("z2", D2005, EDITION_1, 0.75)
    carried, _ = carry_over_closed_rows(
        {"z2": _covered(datetime.date(2026, 1, 1))}, [closed], closed_on=EDITION_2
    )
    assert carried == [closed]


def test_same_archive_never_closes_a_zone():
    """`closed_on=None` : seul le calcul a changé, une zone qui disparaît n'est pas un déclassement."""
    closed = PreviousRow("z3", D2005, EDITION_1, 0.2)
    carried, n_closed = carry_over_closed_rows({}, [_open("z1"), closed], closed_on=None)
    assert carried == [closed]
    assert n_closed == 0


def test_zone_in_error_is_not_closed():
    carried, n_closed = carry_over_closed_rows(
        {}, [_open("z1"), _open("z2")], closed_on=EDITION_2, failed_zones=["z1"]
    )
    assert [row.zone_id for row in carried] == ["z2"]
    assert n_closed == 1


def test_mass_closure_is_refused():
    previous = [_open(f"z{i}") for i in range(40)]
    current = {f"z{i}": _covered() for i in range(29)}  # 11 zones sur 40 disparaissent (27,5 %)
    with pytest.raises(TooManyClosuresError, match="11 zones sur 40"):
        carry_over_closed_rows(current, previous, closed_on=EDITION_2)


def test_mass_closure_guard_can_be_lifted():
    previous = [_open(f"z{i}") for i in range(40)]
    _, n_closed = carry_over_closed_rows({}, previous, closed_on=EDITION_2, max_closed_share=1)
    assert n_closed == 40


def test_few_closures_never_trigger_the_guard_even_with_a_high_share():
    """Petite AOI : 3 zones dont 2 déclassées, c'est un vrai déclassement, pas une archive tronquée."""
    previous = [_open("z1"), _open("z2"), _open("z3")]
    _, n_closed = carry_over_closed_rows({"z1": _covered()}, previous, closed_on=EDITION_2)
    assert n_closed == 2


def test_closures_within_the_share_are_accepted():
    previous = [_open(f"z{i}") for i in range(100)]
    current = {f"z{i}": _covered() for i in range(80)}  # 20 sur 100 : pile le seuil
    _, n_closed = carry_over_closed_rows(current, previous, closed_on=EDITION_2)
    assert n_closed == 20


# --- lignes écrites -------------------------------------------------------------


def test_rows_hold_open_zones_then_closed_ones():
    computed_at = datetime.datetime(2026, 11, 13, 3, 0, tzinfo=datetime.UTC)
    rows = to_area_rows(
        {"z1": _covered(D1990, 0.4)},
        [PreviousRow("z2", D2005, EDITION_2, 0.75)],
        area_type="reserve",
        source_version="inpn_reserve_2026-11-06_v1",
        computed_at=computed_at,
    )
    common = {
        "protected_area_type": "reserve",
        "source_version": "inpn_reserve_2026-11-06_v1",
        "computed_at": computed_at,
    }
    assert rows == [
        {"zone_id": "z1", "valid_from": D1990, "valid_to": None, "coverage_ratio": 0.4, **common},
        {
            "zone_id": "z2",
            "valid_from": D2005,
            "valid_to": EDITION_2,
            "coverage_ratio": 0.75,
            **common,
        },
    ]


def test_rows_match_the_table_columns():
    from clients import ProtectedAreasByZone

    [row] = to_area_rows(
        {"z1": _covered()},
        [],
        area_type="natura2000",
        source_version="v",
        computed_at=datetime.datetime.now(datetime.UTC),
    )
    assert set(row) == {column.name for column in ProtectedAreasByZone.__table__.columns}
