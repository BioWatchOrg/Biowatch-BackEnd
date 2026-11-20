"""Tests pour protected_areas.coverage — fusion des sites et couverture par zone."""

import datetime

import protected_areas.coverage as coverage_mod
import pytest
from geo import IntersectionError
from protected_areas import DatedSite, ZoneCoverage, compute_zone_coverage
from shapely.geometry import Polygon, box

# Zones carrées de 100 m de côté (Lambert-93 : unités en mètres), côte à côte.
ZONES = {"z1": box(0, 0, 100, 100), "z2": box(100, 0, 200, 100), "z3": box(200, 0, 300, 100)}

D1990 = datetime.date(1990, 1, 1)
D2020 = datetime.date(2020, 6, 30)


def _site(geom, valid_from=D1990, site_id="s"):
    return DatedSite(site_id=site_id, geom=geom, valid_from=valid_from)


def test_no_site_means_no_row():
    assert compute_zone_coverage(ZONES, []) == ({}, {})


def test_coverage_is_the_share_of_the_zone_covered():
    coverage, errors = compute_zone_coverage(ZONES, [_site(box(0, 0, 50, 100))])
    assert coverage == {"z1": ZoneCoverage(coverage_ratio=0.5, valid_from=D1990)}
    assert errors == {}


def test_a_site_spanning_several_zones_gives_one_row_per_zone():
    coverage, _ = compute_zone_coverage(ZONES, [_site(box(50, 0, 200, 100))])
    assert {zone: row.coverage_ratio for zone, row in coverage.items()} == {"z1": 0.5, "z2": 1.0}


def test_overlapping_sites_are_merged_not_added():
    """Deux sites superposés (ZPS + SIC du même périmètre) ne comptent qu'une fois."""
    sites = [_site(box(0, 0, 60, 100), D2020, "zps"), _site(box(40, 0, 100, 100), D1990, "sic")]
    coverage, _ = compute_zone_coverage(ZONES, sites)
    assert coverage["z1"] == ZoneCoverage(coverage_ratio=1.0, valid_from=D1990)


def test_valid_from_is_the_oldest_site_covering_the_zone():
    sites = [_site(box(0, 0, 10, 10), D2020, "recent"), _site(box(50, 50, 60, 60), D1990, "ancien")]
    coverage, _ = compute_zone_coverage(ZONES, sites)
    assert coverage["z1"].valid_from == D1990
    assert coverage["z1"].coverage_ratio == pytest.approx(0.02)


def test_a_site_only_touching_the_border_does_not_protect_the_zone():
    """z2 partage une arête avec le site : contact sans surface, donc ni ligne ni date."""
    sites = [_site(box(0, 0, 100, 100), D2020), _site(box(300, 0, 400, 100), D1990, "voisin")]
    coverage, _ = compute_zone_coverage(ZONES, sites)
    assert set(coverage) == {"z1"}


def test_a_sliver_below_the_stored_precision_gives_no_row():
    coverage, _ = compute_zone_coverage(ZONES, [_site(box(0, 0, 0.001, 0.001))])
    assert coverage == {}


def test_invalid_site_geometry_is_repaired_before_the_computation():
    """Polygone en nœud papillon (auto-intersection), courant dans les référentiels."""
    bowtie = Polygon([(0, 0), (100, 100), (100, 0), (0, 100), (0, 0)])
    assert not bowtie.is_valid
    coverage, errors = compute_zone_coverage(ZONES, [_site(bowtie)])
    assert coverage["z1"].coverage_ratio == pytest.approx(0.5)
    assert errors == {}


def test_a_failing_zone_is_reported_and_the_others_are_computed(monkeypatch):
    real = coverage_mod.coverage_ratio

    def flaky(geom, mask, srid):
        if geom.equals(ZONES["z2"]):
            raise IntersectionError("GEOS a refusé la géométrie")
        return real(geom, mask, srid=srid)

    monkeypatch.setattr(coverage_mod, "coverage_ratio", flaky)
    coverage, errors = compute_zone_coverage(ZONES, [_site(box(0, 0, 300, 100))])

    assert set(coverage) == {"z1", "z3"}
    assert errors == {"z2": "GEOS a refusé la géométrie"}
