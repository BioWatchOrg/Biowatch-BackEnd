import hashlib

import h3
import pytest
from core import UndefinedAOIError
from geo.h3_grid import compute_h3_cells

GOLDEN_AOI = "idf"
GOLDEN_RES = 5
GOLDEN_COUNT = 52
GOLDEN_FINGERPRINT = "d8bf3c8d198a6d5a61a17135842a422e042b1e6782c08c45394e5e176d67ae5a"


def _fingerprint(cells) -> str:
    """Empreinte indépendante de l'ordre (les cellules sont triées avant hash)."""
    return hashlib.sha256("\n".join(sorted(cells)).encode()).hexdigest()


def test_compute_h3_cells_count_matches_golden():
    cells = compute_h3_cells(GOLDEN_AOI, GOLDEN_RES)
    assert len(cells) == GOLDEN_COUNT


def test_compute_h3_cells_fingerprint_matches_golden():
    cells = compute_h3_cells(GOLDEN_AOI, GOLDEN_RES)
    assert _fingerprint(cells) == GOLDEN_FINGERPRINT


def test_compute_h3_cells_is_deterministic_across_calls():
    a = compute_h3_cells(GOLDEN_AOI, GOLDEN_RES)
    b = compute_h3_cells(GOLDEN_AOI, GOLDEN_RES)
    assert set(a) == set(b)


def test_compute_h3_cells_has_no_duplicates():
    cells = compute_h3_cells(GOLDEN_AOI, GOLDEN_RES)
    assert len(cells) == len(set(cells))


def test_compute_h3_cells_all_at_requested_resolution():
    cells = compute_h3_cells(GOLDEN_AOI, GOLDEN_RES)
    assert all(h3.get_resolution(c) == GOLDEN_RES for c in cells)


def test_compute_h3_cells_returns_valid_h3_indexes():
    cells = compute_h3_cells(GOLDEN_AOI, GOLDEN_RES)
    assert cells
    assert all(h3.is_valid_cell(c) for c in cells)


def test_compute_h3_cells_unknown_aoi_raises():
    with pytest.raises(UndefinedAOIError):
        compute_h3_cells("zzz", GOLDEN_RES)
