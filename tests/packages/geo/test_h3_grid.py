import hashlib

import h3
import pytest
from core import UndefinedAOIError
from geo.h3_grid import H3GridGenerationError, _cell_to_geometries, compute_h3_cells
from shapely.geometry import Polygon

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


def test_cell_to_geometries_wraps_degenerate_bbox_error(monkeypatch):
    # An empty polygon's .bounds is (nan, nan, nan, nan) — box() on that raises a raw
    # GEOSException, which must be caught and wrapped, not leak out of job_run uncaught.
    import geo.h3_grid as h3_grid_module

    monkeypatch.setattr(h3_grid_module, "cell_to_polygon", lambda cell: Polygon())

    cell = h3.latlng_to_cell(48.8566, 2.3522, GOLDEN_RES)
    with pytest.raises(H3GridGenerationError):
        _cell_to_geometries(cell)
