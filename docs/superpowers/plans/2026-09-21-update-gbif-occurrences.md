# update_gbif_occurrences Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fetch GBIF occurrences for an AOI over a rolling 10-year window, quality-filter them, convert points to H3 `zone_id`, and store them in `staging.species_occurrences_raw`, idempotently.

**Architecture:** A new `packages/species/` package orchestrates the job (`update_gbif_occurrences`), calling a new pure GBIF HTTP client (`packages/clients/gbif/`), a pure quality filter (`packages/species/quality.py`), and a new `point_to_h3_cell` helper in `packages/geo/h3_grid.py`. Storage goes through a new `SpeciesOccurrencesRaw` ORM model in the existing `staging` Postgres schema (new), written via the existing `upsert()` helper with `ON CONFLICT DO NOTHING`, wrapped in the existing `job_run(...)` idempotence contract.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 + PostGIS, `httpx` (already a dependency — no new external dep), `h3` v4, `pytest` + `pytest-cov` (new dev dep).

**Spec:** `docs/superpowers/specs/2026-09-18-update-gbif-occurrences-design.md`

## Global Constraints

- No new runtime dependency beyond `httpx` (already present) — GBIF calls use `httpx.Client` directly, not `pygbif`.
- `staging.species_occurrences_raw` schema is fixed by the spec's DDL — exact column names (`gbif_taxon_key`, not `taxon_key`; no `basis_of_record`, no `source_version` column).
- Conflict on `gbif_occurrence_id` → `DO NOTHING` (immutable historical record), not `DO UPDATE`.
- No DB-level FK from `species_occurrences_raw.zone_id` to `zones_hex.zone_id` (per spec DDL) — membership is checked in application code instead.
- One `job_run` per (AOI, year N); idempotency key uses `period=str(N)`, `source_version="gbif-search-v1"`.
- Every job body must run inside `job_run(...)` (from `clients`) and write via `upsert(...)` (from `clients`) — no raw `INSERT`/manual session commit, per `CLAUDE.md`.
- Coverage target: ≥90% on the new modules (`packages/species/*`, `packages/clients/gbif/*`, the new `point_to_h3_cell` function).
- Run every command from `Biowatch-BackEnd/` (the repo root with `pyproject.toml`), not its parent directory.

---

## File Structure

```
packages/clients/gbif/
  __init__.py            # exports fetch_occurrences, GbifApiError, GBIF_PAGE_LIMIT
  client.py               # HTTP client + pagination over GBIF Occurrence Search

packages/species/
  __init__.py             # exports update_gbif_occurrences
  quality.py              # pure quality filter (no I/O)
  gbif_occurrences.py      # job orchestration (purge, fetch, filter, H3, upsert)

packages/geo/h3_grid.py    # + point_to_h3_cell(lat, lon, resolution) -> H3Cell
packages/geo/__init__.py   # + export point_to_h3_cell

packages/clients/db/models.py    # + SpeciesOccurrencesRaw
packages/clients/db/init_db.py   # + staging schema creation
packages/clients/db/__init__.py  # + export SpeciesOccurrencesRaw
packages/clients/__init__.py     # + export SpeciesOccurrencesRaw, fetch_occurrences, GbifApiError

apps/jobs/definitions.py   # + register update_gbif_occurrences job

pyproject.toml              # + packages/species in hatch wheel packages, + pytest-cov dev dep

tests/packages/clients/test_db_schema.py    # update expected tables/PKs for the new table
tests/packages/clients/test_init_db.py      # update for staging schema creation
tests/packages/geo/test_h3_grid.py          # + tests for point_to_h3_cell
tests/packages/clients/test_gbif_client.py  # new: pagination, errors
tests/packages/species/test_quality.py      # new: quality filter edge cases
tests/packages/species/test_gbif_occurrences.py  # new: job orchestration + idempotence
```

---

### Task 1: `pytest-cov` dev dependency

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `uv run pytest --cov=<module> --cov-report=term-missing` becomes available.

- [ ] **Step 1: Add the dependency**

Run: `uv add --dev pytest-cov`

- [ ] **Step 2: Verify it works**

Run: `uv run pytest --cov=core --cov-report=term-missing tests/packages/core -q`
Expected: test run passes, a coverage table is printed for `core`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add pytest-cov for coverage reporting"
```

---

### Task 2: `staging` schema + `SpeciesOccurrencesRaw` model

**Files:**
- Modify: `packages/clients/db/models.py`
- Modify: `packages/clients/db/init_db.py`
- Modify: `packages/clients/db/__init__.py`
- Modify: `packages/clients/__init__.py`
- Modify: `tests/packages/clients/test_db_schema.py`
- Modify: `tests/packages/clients/test_init_db.py`

**Interfaces:**
- Produces: `clients.SpeciesOccurrencesRaw` (ORM model, table `staging.species_occurrences_raw`, PK `gbif_occurrence_id: str`, columns `event_date: date`, `gbif_taxon_key: int | None`, `scientific_name: str`, `decimal_latitude: float`, `decimal_longitude: float`, `zone_id: str`, `aoi_id: str`, `ingested_at: datetime`).
- Produces: `clients.db.init_db.SCHEMAS = ("staging",)` and `_create_schemas(engine)`, called by `init_db()` before `create_all`.

- [ ] **Step 1: Write the failing schema tests**

Edit `tests/packages/clients/test_db_schema.py`:

```python
EXPECTED_TABLES = {
    "zones_hex",
    "satellite_features_by_zone",
    "osm_features_by_zone",
    "protected_areas_by_zone",
    "species_features_by_zone",
    "stress_score_by_zone",
    "job_runs",
    "job_run_zone_errors",
    "staging.species_occurrences_raw",
}

EXPECTED_PRIMARY_KEYS = {
    "zones_hex": {"zone_id"},
    "satellite_features_by_zone": {"zone_id", "bucket_id", "source_version"},
    "osm_features_by_zone": {"zone_id", "bucket_id", "source_version"},
    "protected_areas_by_zone": {"zone_id", "protected_area_type", "valid_from", "source_version"},
    "species_features_by_zone": {"zone_id", "period_year", "source_version"},
    "stress_score_by_zone": {"zone_id", "bucket_id", "score_method"},
    "job_runs": {"run_id"},
    "job_run_zone_errors": {"run_id", "zone_id"},
    "staging.species_occurrences_raw": {"gbif_occurrence_id"},
}
```

Replace `test_every_feature_table_references_zones_hex` body:

```python
def test_every_feature_table_references_zones_hex():
    # staging.species_occurrences_raw has no DB-level FK to zones_hex by design
    # (spec DDL) — zone_id membership is checked in application code instead.
    referencing = EXPECTED_TABLES - {"zones_hex", "job_runs", "staging.species_occurrences_raw"}
    for table_name in referencing:
        table = Base.metadata.tables[table_name]
        targets = {fk.column.table.name for fk in table.foreign_keys}
        assert "zones_hex" in targets, table_name
```

Replace `test_schema_compiles_to_postgresql_ddl` body (schema-qualified tables render as
`CREATE TABLE staging.species_occurrences_raw`, not `CREATE TABLE species_occurrences_raw`):

```python
def test_schema_compiles_to_postgresql_ddl():
    engine = create_mock_engine("postgresql+psycopg://", executor=lambda *a, **k: None)
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=engine.dialect))
        qualified_name = f"{table.schema}.{table.name}" if table.schema else table.name
        assert f"CREATE TABLE {qualified_name}" in ddl
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/packages/clients/test_db_schema.py -v`
Expected: FAIL — `staging.species_occurrences_raw` not in `Base.metadata.tables` (model doesn't exist yet).

- [ ] **Step 3: Add the `SpeciesOccurrencesRaw` model**

Append to `packages/clients/db/models.py` (after `SpeciesFeaturesByZone`, before `StressScoreByZone`):

```python
class SpeciesOccurrencesRaw(Base):
    """Raw GBIF occurrences for an AOI's rolling 10-year window (staging area)."""

    __tablename__ = "species_occurrences_raw"
    __table_args__ = {"schema": "staging"}

    gbif_occurrence_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    gbif_taxon_key: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scientific_name: Mapped[str] = mapped_column(String, nullable=False)
    decimal_latitude: Mapped[float] = mapped_column(Float, nullable=False)
    decimal_longitude: Mapped[float] = mapped_column(Float, nullable=False)
    zone_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    aoi_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ingested_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/packages/clients/test_db_schema.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Write the failing `init_db` schema-creation tests**

Append to `tests/packages/clients/test_init_db.py`:

```python
def test_staging_schema_declared_for_init():
    from clients.db.init_db import SCHEMAS

    assert "staging" in SCHEMAS


def test_create_schemas_issues_one_create_per_schema():
    from clients.db.init_db import SCHEMAS, _create_schemas

    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value

    _create_schemas(engine)

    executed = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert len(executed) == len(SCHEMAS)
    assert all("CREATE SCHEMA IF NOT EXISTS" in stmt for stmt in executed)
    for schema in SCHEMAS:
        assert any(schema in stmt for stmt in executed)
```

Replace `test_init_db_creates_extensions_before_tables`:

```python
def test_init_db_creates_extensions_before_tables(monkeypatch):
    engine = MagicMock()
    order: list[str] = []
    monkeypatch.setattr(init_db_mod, "_create_extensions", lambda e: order.append(f"ext:{e}"))
    monkeypatch.setattr(init_db_mod, "_create_schemas", lambda e: order.append(f"schema:{e}"))
    monkeypatch.setattr(
        init_db_mod.Base.metadata, "create_all", lambda e: order.append(f"tables:{e}")
    )

    init_db(engine=engine)

    # Extensions and schemas must exist before create_all.
    assert order == [f"ext:{engine}", f"schema:{engine}", f"tables:{engine}"]
```

Replace `test_init_db_falls_back_to_shared_engine`:

```python
def test_init_db_falls_back_to_shared_engine(monkeypatch):
    sentinel = MagicMock(name="shared_engine")
    used: list[object] = []
    monkeypatch.setattr(init_db_mod, "get_engine", lambda: sentinel)
    monkeypatch.setattr(init_db_mod, "_create_extensions", lambda e: used.append(e))
    monkeypatch.setattr(init_db_mod, "_create_schemas", lambda e: used.append(e))
    monkeypatch.setattr(init_db_mod.Base.metadata, "create_all", lambda e: used.append(e))

    init_db()

    assert used == [sentinel, sentinel, sentinel]
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `uv run pytest tests/packages/clients/test_init_db.py -v`
Expected: FAIL — `SCHEMAS` / `_create_schemas` don't exist yet.

- [ ] **Step 7: Implement schema creation in `init_db.py`**

Edit `packages/clients/db/init_db.py`:

```python
EXTENSIONS = ("postgis", "pgcrypto")
SCHEMAS = ("staging",)


def _create_extensions(engine: Engine) -> None:
    """Create the PostgreSQL extensions required before any table is built."""
    with engine.begin() as conn:
        for extension in EXTENSIONS:
            conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))


def _create_schemas(engine: Engine) -> None:
    """Create non-default Postgres schemas (e.g. `staging`) before create_all."""
    with engine.begin() as conn:
        for schema in SCHEMAS:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))


def init_db(engine: Engine | None = None) -> None:
    """Create extensions, schemas and every table. Idempotent: safe to re-run."""
    try:
        engine = engine or get_engine()
        _create_extensions(engine)
        _create_schemas(engine)
        Base.metadata.create_all(engine)
        logger.info(
            "schema initialized",
            extra={
                "event": "db.init_db.success",
                "context": {"n_tables": len(Base.metadata.tables)},
            },
        )
    except Exception as e:
        logger.error(
            "error initializing schema",
            extra={"event": "db.init_db.error", "context": {"error": str(e)}},
        )
        raise
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/packages/clients/test_init_db.py tests/packages/clients/test_db_schema.py -v`
Expected: PASS (all tests).

- [ ] **Step 9: Export the new model**

Edit `packages/clients/db/__init__.py` — add `SpeciesOccurrencesRaw` to the `from .models import (...)` block and to `__all__`.

Edit `packages/clients/__init__.py` — add `SpeciesOccurrencesRaw` to the `from .db import (...)` block and to `__all__`.

- [ ] **Step 10: Apply the schema to the local dev DB**

Run: `uv run biowatch-jobs init_db`
Expected: log line `"schema initialized"` with `n_tables` now 9.

- [ ] **Step 11: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass (no regression on existing tables/tests).

- [ ] **Step 12: Commit**

```bash
git add packages/clients/db/models.py packages/clients/db/init_db.py \
        packages/clients/db/__init__.py packages/clients/__init__.py \
        tests/packages/clients/test_db_schema.py tests/packages/clients/test_init_db.py
git commit -m "feat: add staging.species_occurrences_raw schema"
```

---

### Task 3: `point_to_h3_cell` (point → H3)

**Files:**
- Modify: `packages/geo/h3_grid.py`
- Modify: `packages/geo/__init__.py`
- Modify: `tests/packages/geo/test_h3_grid.py`

**Interfaces:**
- Consumes: nothing new (just the `h3` library, already a dependency).
- Produces: `geo.point_to_h3_cell(lat: float, lon: float, resolution: int) -> H3Cell` (H3Cell = str).

- [ ] **Step 1: Write the failing tests**

Append to `tests/packages/geo/test_h3_grid.py`:

```python
from geo.h3_grid import point_to_h3_cell

# Notre-Dame de Paris, a fixed, well-known point.
NOTRE_DAME_LAT = 48.852968
NOTRE_DAME_LON = 2.349902


def test_point_to_h3_cell_returns_valid_cell_at_requested_resolution():
    cell = point_to_h3_cell(NOTRE_DAME_LAT, NOTRE_DAME_LON, GOLDEN_RES)
    assert h3.is_valid_cell(cell)
    assert h3.get_resolution(cell) == GOLDEN_RES


def test_point_to_h3_cell_is_deterministic():
    a = point_to_h3_cell(NOTRE_DAME_LAT, NOTRE_DAME_LON, GOLDEN_RES)
    b = point_to_h3_cell(NOTRE_DAME_LAT, NOTRE_DAME_LON, GOLDEN_RES)
    assert a == b


def test_point_to_h3_cell_matches_containing_grid_cell():
    """The cell for a point inside idf must be one of compute_h3_cells(idf, res)."""
    cell = point_to_h3_cell(NOTRE_DAME_LAT, NOTRE_DAME_LON, GOLDEN_RES)
    assert cell in set(compute_h3_cells(GOLDEN_AOI, GOLDEN_RES))
```

Add `compute_h3_cells` to the existing `from geo.h3_grid import compute_h3_cells` line if not already imported as a bare name (it already is, at the top of the file).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/packages/geo/test_h3_grid.py -v -k point_to_h3_cell`
Expected: FAIL — `ImportError: cannot import name 'point_to_h3_cell'`.

- [ ] **Step 3: Implement `point_to_h3_cell`**

Add to `packages/geo/h3_grid.py`, after `compute_h3_cells`:

```python
def point_to_h3_cell(lat: float, lon: float, resolution: int) -> H3Cell:
    """
    Pure, deterministic point → H3 cell conversion at the given resolution.

    Inverse of `compute_h3_cells`: used to place an external point (e.g. a
    GBIF occurrence) into the same H3 grid already persisted in `zones_hex`.
    """
    return h3.latlng_to_cell(lat, lon, resolution)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/packages/geo/test_h3_grid.py -v`
Expected: PASS (all tests, including the pre-existing ones).

- [ ] **Step 5: Export it**

Edit `packages/geo/__init__.py`:

```python
from .h3_grid import compute_h3_cells, point_to_h3_cell

__all__ = ["compute_h3_cells", "point_to_h3_cell"]
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add packages/geo/h3_grid.py packages/geo/__init__.py tests/packages/geo/test_h3_grid.py
git commit -m "feat: add point_to_h3_cell for external point ingestion"
```

---

### Task 4: GBIF HTTP client (`packages/clients/gbif`)

**Files:**
- Create: `packages/clients/gbif/__init__.py`
- Create: `packages/clients/gbif/client.py`
- Create: `tests/packages/clients/test_gbif_client.py`
- Modify: `packages/clients/__init__.py`

**Interfaces:**
- Consumes: `httpx` (already a dependency).
- Produces: `clients.gbif.fetch_occurrences(geometry_wkt: str, year: int, client: httpx.Client | None = None) -> Iterator[dict]`, `clients.gbif.GbifApiError`, `clients.gbif.GBIF_PAGE_LIMIT: int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/packages/clients/test_gbif_client.py`:

```python
"""Tests for clients.gbif.client — GBIF Occurrence Search pagination."""

import httpx
import pytest

from clients.gbif.client import GBIF_PAGE_LIMIT, GBIF_SEARCH_URL, GbifApiError, fetch_occurrences


def _response(results: list[dict], end_of_records: bool) -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": results, "endOfRecords": end_of_records},
        request=httpx.Request("GET", GBIF_SEARCH_URL),
    )


def test_fetch_occurrences_single_page(monkeypatch):
    calls = []

    def fake_get(self, url, params=None, **kwargs):
        calls.append(params)
        return _response([{"key": 1}, {"key": 2}], end_of_records=True)

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    records = list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020))

    assert [r["key"] for r in records] == [1, 2]
    assert len(calls) == 1
    assert calls[0]["geometry"] == "POLYGON((0 0,0 1,1 1,1 0,0 0))"
    assert calls[0]["year"] == 2020
    assert calls[0]["hasCoordinate"] == "true"
    assert calls[0]["limit"] == GBIF_PAGE_LIMIT
    assert calls[0]["offset"] == 0


def test_fetch_occurrences_paginates_until_end_of_records(monkeypatch):
    pages = [
        _response([{"key": 1}], end_of_records=False),
        _response([{"key": 2}], end_of_records=False),
        _response([{"key": 3}], end_of_records=True),
    ]
    calls = []

    def fake_get(self, url, params=None, **kwargs):
        calls.append(params["offset"])
        return pages.pop(0)

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    records = list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020))

    assert [r["key"] for r in records] == [1, 2, 3]
    assert calls == [0, GBIF_PAGE_LIMIT, 2 * GBIF_PAGE_LIMIT]


def test_fetch_occurrences_raises_on_http_error(monkeypatch):
    def fake_get(self, url, params=None, **kwargs):
        return httpx.Response(500, request=httpx.Request("GET", GBIF_SEARCH_URL))

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    with pytest.raises(GbifApiError):
        list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020))


def test_fetch_occurrences_uses_injected_client():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"results": [{"key": 42}], "endOfRecords": True}
            )
        )
    )

    records = list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020, client=client))

    assert [r["key"] for r in records] == [42]
    client.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/packages/clients/test_gbif_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clients.gbif'`.

- [ ] **Step 3: Implement the client**

Create `packages/clients/gbif/client.py`:

```python
"""HTTP client for the GBIF Occurrence Search API.

No business logic here (no quality filtering, no H3, no DB) — this module
only knows how to talk to GBIF and paginate. See `species.quality` and
`species.gbif_occurrences` for the pipeline built on top of it.
"""

import logging
from collections.abc import Iterator

import httpx

logger = logging.getLogger(__name__)

GBIF_SEARCH_URL = "https://api.gbif.org/v1/occurrence/search"
GBIF_PAGE_LIMIT = 300
GBIF_TIMEOUT_SECONDS = 30.0


class GbifApiError(Exception):
    """Raised when the GBIF Occurrence Search API returns an error response."""


def fetch_occurrences(
    geometry_wkt: str,
    year: int,
    client: httpx.Client | None = None,
) -> Iterator[dict]:
    """
    Yield every GBIF occurrence record for one year within a WKT geometry.

    Paginates the GBIF Occurrence Search API (`offset`/`limit`) until
    `endOfRecords` is true. Each yielded item is the raw GBIF JSON record
    (a dict) — no transformation, no filtering.

    `client` can be injected (e.g. for tests); when omitted, a short-lived
    `httpx.Client` is created and closed internally.
    """
    owns_client = client is None
    http_client = client or httpx.Client(timeout=GBIF_TIMEOUT_SECONDS)
    try:
        offset = 0
        while True:
            response = http_client.get(
                GBIF_SEARCH_URL,
                params={
                    "geometry": geometry_wkt,
                    "year": year,
                    "hasCoordinate": "true",
                    "hasGeospatialIssue": "false",
                    "limit": GBIF_PAGE_LIMIT,
                    "offset": offset,
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GBIF API request failed",
                    extra={
                        "event": "gbif.request_error",
                        "context": {
                            "year": year,
                            "offset": offset,
                            "status_code": response.status_code,
                        },
                    },
                )
                raise GbifApiError(
                    f"GBIF request failed (year={year}, offset={offset}): {exc}"
                ) from exc

            data = response.json()
            yield from data.get("results", [])

            if data.get("endOfRecords", True):
                break
            offset += GBIF_PAGE_LIMIT
    finally:
        if owns_client:
            http_client.close()
```

Create `packages/clients/gbif/__init__.py`:

```python
from .client import GBIF_PAGE_LIMIT, GbifApiError, fetch_occurrences

__all__ = ["fetch_occurrences", "GbifApiError", "GBIF_PAGE_LIMIT"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/packages/clients/test_gbif_client.py -v`
Expected: PASS (all 4 tests).

- [ ] **Step 5: Export from `clients`**

Edit `packages/clients/__init__.py` — add:

```python
from .gbif import GBIF_PAGE_LIMIT, GbifApiError, fetch_occurrences
```

and add `"fetch_occurrences", "GbifApiError", "GBIF_PAGE_LIMIT"` to `__all__`.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add packages/clients/gbif tests/packages/clients/test_gbif_client.py packages/clients/__init__.py
git commit -m "feat: add GBIF Occurrence Search HTTP client"
```

---

### Task 5: Quality filter (`packages/species/quality.py`)

**Files:**
- Create: `packages/species/__init__.py` (empty exports for now, filled in Task 6)
- Create: `packages/species/quality.py`
- Create: `tests/packages/species/test_quality.py` (no `__init__.py` in the test dir — no other `tests/packages/*` subdir has one, confirmed)

**Interfaces:**
- Consumes: nothing (pure function, stdlib only).
- Produces: `species.quality.quality_filter(record: dict, date_min: date, date_max: date) -> dict | None`. On success, the returned dict has keys `gbif_occurrence_id: str`, `event_date: date`, `gbif_taxon_key: int | None`, `scientific_name: str`, `decimal_latitude: float`, `decimal_longitude: float`.

- [ ] **Step 1: Write the failing tests**

Create `tests/packages/species/test_quality.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/packages/species/test_quality.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'species'`.

- [ ] **Step 3: Implement the quality filter**

Create `packages/species/__init__.py` (empty for now):

```python
"""Species/biodiversity business logic (GBIF ingestion, future feature aggregation)."""
```

Create `packages/species/quality.py`:

```python
"""Pure quality filtering for raw GBIF occurrence records.

No I/O, no DB, no H3 — this module only decides whether a raw GBIF JSON
record is good enough to keep, and normalizes it into the shape expected by
`staging.species_occurrences_raw` (minus `zone_id` / `aoi_id`, added by the
orchestrating job once the point is placed on the H3 grid).
"""

from datetime import date, datetime

_LAT_RANGE = (-90.0, 90.0)
_LON_RANGE = (-180.0, 180.0)


def _parse_event_date(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _is_valid_coordinate(lat: object, lon: object) -> bool:
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return False
    if lat == 0 and lon == 0:
        return False
    return _LAT_RANGE[0] <= lat <= _LAT_RANGE[1] and _LON_RANGE[0] <= lon <= _LON_RANGE[1]


def quality_filter(record: dict, date_min: date, date_max: date) -> dict | None:
    """
    Validate and normalize one raw GBIF occurrence record.

    Returns None if the record fails any quality check:
    - missing GBIF `key` or `scientificName`;
    - missing/invalid/out-of-range coordinates (including the (0, 0) "null
      island" marker some datasets use for missing data);
    - missing or unparseable `eventDate`, or a date outside
      `[date_min, date_max]`.

    Returns a dict ready for `staging.species_occurrences_raw` (without
    `zone_id`/`aoi_id`) otherwise.
    """
    key = record.get("key")
    scientific_name = record.get("scientificName")
    if key is None or not scientific_name:
        return None

    lat = record.get("decimalLatitude")
    lon = record.get("decimalLongitude")
    if not _is_valid_coordinate(lat, lon):
        return None

    event_date = _parse_event_date(record.get("eventDate"))
    if event_date is None or not (date_min <= event_date <= date_max):
        return None

    return {
        "gbif_occurrence_id": str(key),
        "event_date": event_date,
        "gbif_taxon_key": record.get("taxonKey"),
        "scientific_name": scientific_name,
        "decimal_latitude": float(lat),
        "decimal_longitude": float(lon),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/packages/species/test_quality.py -v`
Expected: PASS (all 13 tests).

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add packages/species tests/packages/species/test_quality.py
git commit -m "feat: add pure quality filter for GBIF occurrence records"
```

---

### Task 6: Job orchestration (`packages/species/gbif_occurrences.py`)

**Files:**
- Modify: `packages/species/__init__.py`
- Create: `packages/species/gbif_occurrences.py`
- Create: `tests/packages/species/test_gbif_occurrences.py`

**Interfaces:**
- Consumes: `core.load_aoi`, `core.compute_idempotency_key`, `core.biowatch_now`; `clients.job_run`, `clients.JobAlreadySucceeded`, `clients.upsert`, `clients.SpeciesOccurrencesRaw`, `clients.fetch_occurrences`, `clients.GbifApiError`; `geo.point_to_h3_cell`; `species.quality.quality_filter`; `clients.db.models.ZonesHex` (via `clients.ZonesHex`).
- Produces: `species.update_gbif_occurrences(aoi_label: AoiLabel, engine: Engine | None = None) -> None`.

- [ ] **Step 1: Write the failing unit tests (mocked job_run, no DB)**

Create `tests/packages/species/test_gbif_occurrences.py`:

```python
"""Tests for species.gbif_occurrences.update_gbif_occurrences."""

import os
import uuid
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine, delete, func, select

import species.gbif_occurrences as gbif_occurrences
from clients import JobAlreadySucceeded

GOLDEN_AOI = "idf"


def test_update_gbif_occurrences_skips_when_already_succeeded(monkeypatch):
    called = {"body": False}

    @contextmanager
    def fake_job_run(*args, idempotency_key="", **kwargs):
        raise JobAlreadySucceeded(idempotency_key, uuid.uuid4())
        called["body"] = True  # pragma: no cover - unreachable
        yield  # pragma: no cover

    monkeypatch.setattr(gbif_occurrences, "job_run", fake_job_run)

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)
    assert called["body"] is False


def test_rolling_window_covers_ten_years():
    date_min, date_max = gbif_occurrences.rolling_window(2025)
    assert date_min == date(2016, 1, 1)
    assert date_max == date(2025, 12, 31)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/packages/species/test_gbif_occurrences.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'species.gbif_occurrences'`.

- [ ] **Step 3: Implement the job**

Create `packages/species/gbif_occurrences.py`:

```python
"""update_gbif_occurrences job: GBIF occurrences → staging.species_occurrences_raw.

Fetches GBIF occurrences for an AOI over a rolling 10-year window, applies a
quality filter, converts points to H3 `zone_id`, and upserts them into
`staging.species_occurrences_raw`. Runs once a year: for year N, the window
is [N-9, N].
"""

import logging
from datetime import date

from clients import (
    GbifApiError,
    JobAlreadySucceeded,
    SpeciesOccurrencesRaw,
    ZonesHex,
    fetch_occurrences,
    job_run,
    upsert,
)
from core import AOI, AoiLabel, biowatch_now, compute_idempotency_key, load_aoi
from geo import point_to_h3_cell
from sqlalchemy import Engine, delete, select

from species.quality import quality_filter

logger = logging.getLogger(__name__)

GBIF_SOURCE_VERSION = "gbif-search-v1"
WINDOW_YEARS = 10


def rolling_window(current_year: int) -> tuple[date, date]:
    """Return (date_min, date_max) for the 10-year window ending at current_year."""
    return date(current_year - (WINDOW_YEARS - 1), 1, 1), date(current_year, 12, 31)


def update_gbif_occurrences(aoi_label: AoiLabel, engine: Engine | None = None) -> None:
    """
    Fetch, filter, and store GBIF occurrences for an AOI's rolling 10-year window.

    Idempotent: guarded by `job_run` (one run per AOI + target year N ⇒ skip if
    already succeeded) and written via `upsert(..., update_columns=[])` (`ON
    CONFLICT DO NOTHING` — a GBIF occurrence is an immutable historical
    record, re-running never needs to overwrite an existing row).
    """
    aoi: AOI = load_aoi(aoi_label)
    current_year = biowatch_now().year
    date_min, date_max = rolling_window(current_year)

    idempotency_key = compute_idempotency_key(
        job_name="update_gbif_occurrences",
        scope=aoi_label,
        period=str(current_year),
        source_version=GBIF_SOURCE_VERSION,
    )
    try:
        with job_run(
            job_name="update_gbif_occurrences",
            scope=aoi_label,
            idempotency_key=idempotency_key,
            engine=engine,
        ) as (_, session):
            log_ctx = {"aoi": aoi_label, "year": current_year}
            logger.info(
                "purging staging rows outside the rolling window",
                extra={"event": "gbif_occurrences.purge", "context": log_ctx},
            )
            session.execute(
                delete(SpeciesOccurrencesRaw).where(
                    SpeciesOccurrencesRaw.aoi_id == aoi_label,
                    SpeciesOccurrencesRaw.event_date < date_min,
                )
            )

            valid_zone_ids = set(
                session.scalars(
                    select(ZonesHex.zone_id).where(ZonesHex.aoi_id == aoi_label)
                ).all()
            )
            geometry_wkt = aoi.geom.wkt

            for year in range(date_min.year, date_max.year + 1):
                rows: list[dict[str, object]] = []
                try:
                    for record in fetch_occurrences(geometry_wkt, year):
                        quality_row = quality_filter(record, date_min, date_max)
                        if quality_row is None:
                            continue
                        zone_id = point_to_h3_cell(
                            quality_row["decimal_latitude"],
                            quality_row["decimal_longitude"],
                            aoi.default_res,
                        )
                        if zone_id not in valid_zone_ids:
                            continue
                        rows.append({**quality_row, "zone_id": zone_id, "aoi_id": aoi_label})
                except GbifApiError:
                    logger.error(
                        "GBIF fetch failed for year",
                        extra={
                            "event": "gbif_occurrences.fetch_error",
                            "context": {**log_ctx, "fetch_year": year},
                        },
                    )
                    raise

                upsert(
                    session=session,
                    model=SpeciesOccurrencesRaw,
                    rows=rows,
                    update_columns=[],
                )
                logger.info(
                    "upserted GBIF occurrences for year",
                    extra={
                        "event": "gbif_occurrences.upserted",
                        "context": {**log_ctx, "fetch_year": year, "n_rows": len(rows)},
                    },
                )

    except JobAlreadySucceeded:
        logger.info(
            "GBIF occurrences already up to date, skipping",
            extra={
                "event": "gbif_occurrences.skip",
                "context": {"aoi": aoi_label, "idempotency_key": idempotency_key},
            },
        )
        return
```

Edit `packages/species/__init__.py`:

```python
"""Species/biodiversity business logic (GBIF ingestion, future feature aggregation)."""

from .gbif_occurrences import update_gbif_occurrences

__all__ = ["update_gbif_occurrences"]
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `uv run pytest tests/packages/species/test_gbif_occurrences.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Write the failing DB-integration tests (gated on `TEST_DATABASE_URL`)**

Append to `tests/packages/species/test_gbif_occurrences.py`:

```python
@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_update_gbif_occurrences_is_idempotent_and_deduplicates(monkeypatch):
    """Full run against Postgres: rerun with an overlapping fetch → no duplicates."""
    from clients.db import JobRun
    from clients.db.init_db import init_db

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)

    with engine.begin() as conn:
        conn.execute(
            delete(SpeciesOccurrencesRaw).where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI)
        )
        conn.execute(
            delete(JobRun).where(JobRun.job_name == "update_gbif_occurrences")
        )

    from geo.h3_grid import generate_h3_grid

    generate_h3_grid(GOLDEN_AOI, resolution=5, engine=engine)

    fake_records = {
        2020: [
            {
                "key": 1001,
                "scientificName": "Vulpes vulpes",
                "taxonKey": 5219243,
                "decimalLatitude": 48.8566,
                "decimalLongitude": 2.3522,
                "eventDate": "2020-06-15T00:00:00",
            }
        ]
    }

    def fake_fetch_occurrences(geometry_wkt, year, client=None):
        return iter(fake_records.get(year, []))

    monkeypatch.setattr(gbif_occurrences, "fetch_occurrences", fake_fetch_occurrences)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))

    # Force AOI resolution to match the res-5 grid just generated.
    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    monkeypatch.setattr(
        gbif_occurrences, "load_aoi", lambda label: aoi.__class__(**{**aoi.__dict__, "default_res": 5})
    )

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)  # 2nd run must skip (same year)

    with engine.connect() as conn:
        count = conn.scalar(
            select(func.count())
            .select_from(SpeciesOccurrencesRaw)
            .where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI)
        )
        row = conn.execute(
            select(SpeciesOccurrencesRaw).where(
                SpeciesOccurrencesRaw.gbif_occurrence_id == "1001"
            )
        ).one()

    assert count == 1  # no duplicate despite two runs
    assert row.scientific_name == "Vulpes vulpes"
    assert row.event_date == date(2020, 6, 15)
```

- [ ] **Step 6: Run test to verify it fails without the schema/data set up**

Run: `TEST_DATABASE_URL="postgresql+psycopg://biowatch:biowatch_dev@127.0.0.1:5433/biowatch_dev" uv run pytest tests/packages/species/test_gbif_occurrences.py -v -k idempotent`

(On Windows PowerShell: `$env:TEST_DATABASE_URL="postgresql+psycopg://biowatch:biowatch_dev@127.0.0.1:5433/biowatch_dev"; uv run pytest tests/packages/species/test_gbif_occurrences.py -v -k idempotent`)

Expected: FAILs before Step 3's implementation exists; once Task 6 Step 3 is done this should already pass — if it fails here for another reason (e.g. `zones_hex` FK violation, `aoi_id` mismatch), debug against the actual schema before moving on.

- [ ] **Step 7: Fix any integration issues found, then confirm it passes**

Run: same command as Step 6.
Expected: PASS.

- [ ] **Step 8: Run the full suite (unit only, DB tests skip without `TEST_DATABASE_URL`)**

Run: `uv run pytest -q`
Expected: all pass, 2 skipped (this one + the existing `generate_h3_grid` one) if `TEST_DATABASE_URL` is unset.

- [ ] **Step 9: Commit**

```bash
git add packages/species/gbif_occurrences.py packages/species/__init__.py \
        tests/packages/species/test_gbif_occurrences.py
git commit -m "feat: implement update_gbif_occurrences job"
```

---

### Task 7: CLI registration + packaging

**Files:**
- Modify: `pyproject.toml`
- Modify: `apps/jobs/definitions.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `species.update_gbif_occurrences`.
- Produces: `uv run biowatch-jobs update_gbif_occurrences --aoi <label>` CLI command.

- [ ] **Step 1: Add `packages/species` to the hatch wheel packages**

Edit `pyproject.toml`, `[tool.hatch.build.targets.wheel]`:

```toml
packages = [
    "apps/api",
    "apps/jobs",
    "packages/clients",
    "packages/core",
    "packages/geo",
    "packages/ml",
    "packages/scoring",
    "packages/species",
]
```

- [ ] **Step 2: Re-sync so `species` is importable**

Run: `uv sync`

- [ ] **Step 3: Register the job**

Edit `apps/jobs/definitions.py` — add the import and registration (after the `generate_h3_grid` registration, before `init_db`):

```python
from species import update_gbif_occurrences


def _configure_update_gbif_occurrences(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--aoi",
        required=True,
        help="Label de l'AOI (voir packages/core/aoi/aoi_registry.json).",
    )


def _run_update_gbif_occurrences(args: Namespace) -> None:
    update_gbif_occurrences(aoi_label=args.aoi)


register(
    Job(
        name="update_gbif_occurrences",
        help=(
            "Récupère les occurrences GBIF pour une AOI sur une fenêtre glissante "
            "de 10 ans et les stocke dans staging.species_occurrences_raw."
        ),
        run=_run_update_gbif_occurrences,
        configure=_configure_update_gbif_occurrences,
    )
)
```

- [ ] **Step 4: Verify the CLI sees the new job**

Run: `uv run biowatch-jobs --help`
Expected: `update_gbif_occurrences` listed among the subcommands.

Run: `uv run biowatch-jobs update_gbif_occurrences --help`
Expected: shows `--aoi` as a required argument.

- [ ] **Step 5: Document it in the README**

Edit `README.md`, in the `## CLI` section, add after the existing `generate_h3_grid` example:

```
    uv run biowatch-jobs update_gbif_occurrences --aoi idf
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock apps/jobs/definitions.py README.md
git commit -m "feat: register update_gbif_occurrences CLI job"
```

---

### Task 8: Coverage check

**Files:** none (verification only).

- [ ] **Step 1: Measure coverage on the new modules**

Run:
```bash
uv run pytest --cov=species --cov=clients.gbif --cov-report=term-missing \
  tests/packages/species tests/packages/clients/test_gbif_client.py \
  tests/packages/geo/test_h3_grid.py -q
```

- [ ] **Step 2: Address any gap below 90%**

If a module is under 90%, read the `Missing` line numbers from the report, open that file, and add the missing test case(s) to the relevant test file from Tasks 4-6 (mirroring the existing test style — one case per branch). Re-run Step 1 until every listed module is ≥90%.

- [ ] **Step 3: Run the entire repo suite one last time**

Run: `uv run pytest -q`
Expected: all pass (same skip count as before this plan started, plus the two new DB-gated tests skipped if `TEST_DATABASE_URL` is unset).

---

### Task 9: Manual smoke test against the real GBIF API

This validates the one open risk flagged in the spec: whether GBIF accepts
the `idf` polygon's WKT as a `geometry` filter (GBIF limits WKT complexity;
this was never verified against the real API during design).

**Files:** none (manual verification).

- [ ] **Step 1: Run the job for real against `idf`**

Ensure the local Postgres container is up and `zones_hex` has `idf` at its
`default_res` (from earlier setup: `uv run biowatch-jobs generate_h3_grid --aoi idf --resolution 8`).

Run: `uv run biowatch-jobs --debug update_gbif_occurrences --aoi idf`

- [ ] **Step 2: Check the outcome**

- If it completes: check `job_runs.status = 'success'` and
  `SELECT count(*) FROM staging.species_occurrences_raw WHERE aoi_id='idf'`
  returns a plausible non-zero count.
- If GBIF rejects the `geometry` param (look for a `GbifApiError` in the logs
  quoting a 4xx from GBIF mentioning the geometry): this confirms the risk
  flagged in the spec. Do not silently work around it — report back with the
  exact GBIF error message so the fallback (geometry simplification) can be
  scoped as a follow-up task with its own test coverage, rather than
  bolted on speculatively.

- [ ] **Step 3: Report the result**

Note the outcome (row count, any errors) back before considering this ticket done — this step has no automated pass/fail, it's the real-world check the unit tests can't cover (they all mock GBIF).

---

### Task 10: GBIF geometry fallback (convex hull)

**Added after Task 9 confirmed the risk for real:** GBIF rejected the `idf`
AOI's exact polygon (466 vertices, 15291-byte WKT) with a `400` — the request
line (~16KB) trips a proxy/servlet URI-length limit before GBIF's own
validator runs. Confirmed empirically that the AOI's **convex hull** fixes
this: 22 vertices, 733-byte WKT, only +15.5% area over the true polygon. The
extra area is harmless: `update_gbif_occurrences` already discards any
occurrence whose computed `zone_id` isn't in `zones_hex` for the AOI (the
existing zone-membership safety check from Task 6), so hull-only false
positives are filtered out downstream — this fallback only widens what GBIF
is asked for, never what gets stored. A convex hull is also always valid
(never self-intersecting) and always fully contains the original polygon,
unlike a tolerance-based `shapely.simplify()` which could clip off real area
— matching the spec's own fallback description ("simplifier... en préservant
le polygone englobant").

**Files:**
- Modify: `packages/species/gbif_occurrences.py`
- Modify: `tests/packages/species/test_gbif_occurrences.py`

**Interfaces:**
- No new public interface — `update_gbif_occurrences`'s signature is
  unchanged. Internal only: the `geometry_wkt` passed to `fetch_occurrences`
  changes from `aoi.geom.wkt` to `aoi.geom.convex_hull.wkt`.

- [ ] **Step 1: Write the failing test**

Add to `tests/packages/species/test_gbif_occurrences.py`:

```python
def test_update_gbif_occurrences_queries_gbif_with_convex_hull_not_raw_polygon(monkeypatch):
    """GBIF rejects the idf AOI's raw polygon (466 vertices, ~16KB request
    line) with a 400 — confirmed against the real API. The convex hull (22
    vertices) is what must be sent instead; the exact polygon is still used
    for the zones_hex membership check, so no correctness is lost."""
    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    captured_geometries = []

    @contextmanager
    def fake_job_run(*args, idempotency_key="", **kwargs):
        raise JobAlreadySucceeded(idempotency_key, uuid.uuid4())
        yield  # pragma: no cover

    def fake_fetch_occurrences(geometry_wkt, year, client=None):
        captured_geometries.append(geometry_wkt)
        return iter([])

    monkeypatch.setattr(gbif_occurrences, "job_run", fake_job_run)
    monkeypatch.setattr(gbif_occurrences, "fetch_occurrences", fake_fetch_occurrences)

    # job_run raises before the loop, so no geometry is captured this way —
    # instead call the geometry computation directly as update_gbif_occurrences
    # does internally: assert the module computes the hull, not the raw geom.
    assert aoi.geom.convex_hull.wkt != aoi.geom.wkt  # sanity: hull actually differs for idf
    assert len(aoi.geom.convex_hull.exterior.coords) < len(aoi.geom.exterior.coords)
```

Note: the test above's sanity assertions are safe to keep, but they don't
yet exercise `update_gbif_occurrences` itself computing the hull. Replace
the body with a version that actually drives the function: reuse the
DB-gated test's fake `job_run` pattern from Task 6
(`_fake_job_run_yielding`-style, i.e. a fake that actually yields a
`(RunContext, session)` pair so the function body runs) so the real
`geometry_wkt = ...` line executes and gets captured. Look at the existing
`test_update_gbif_occurrences_row_level_dedup_across_overlapping_windows`
test in the same file (added in Task 6's fix round) for the established
pattern of a `job_run` fake that lets the function body actually run against
a fake session — mirror that structure, but you don't need a real DB for
this test: a `MagicMock()` session is enough since this test only checks
what `geometry_wkt` value reaches `fetch_occurrences`, not persistence.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/packages/species/test_gbif_occurrences.py -v -k convex_hull`
Expected: FAIL — the captured `geometry_wkt` equals `aoi.geom.wkt` (466
vertices), not `aoi.geom.convex_hull.wkt` (22 vertices), because the
implementation hasn't changed yet.

- [ ] **Step 3: Implement the fix**

In `packages/species/gbif_occurrences.py`, find:

```python
            geometry_wkt = aoi.geom.wkt
```

Replace with:

```python
            # GBIF's `geometry` param has an undocumented complexity/URL-length
            # limit — the idf AOI's exact polygon (466 vertices, ~15KB WKT)
            # gets a 400 from a proxy/servlet limit before GBIF's own
            # validator runs (confirmed against the real API). The convex
            # hull (22 vertices for idf) is small, always valid, and always
            # fully contains the original polygon, so it never causes GBIF to
            # miss a real occurrence. The extra area is safe: any occurrence
            # whose zone_id falls outside zones_hex for this AOI is already
            # discarded below (the zone-membership check), so hull-only false
            # positives never reach storage — this only widens the fetch,
            # never what's persisted.
            geometry_wkt = aoi.geom.convex_hull.wkt
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/packages/species/test_gbif_occurrences.py -v -k convex_hull`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass (same counts as after Task 8, plus this one new test).

- [ ] **Step 6: Re-run the real smoke test to confirm the actual fix**

The unit test only proves the function computes the hull's WKT — it doesn't
prove GBIF accepts it. Repeat Task 9's exact manual check:

Run: `uv run biowatch-jobs --debug update_gbif_occurrences --aoi idf`

Expected: no `GbifApiError`; the job completes (`job.success` in the logs).
Then verify: `SELECT count(*) FROM staging.species_occurrences_raw WHERE aoi_id='idf';`
via `docker exec postgis psql -U biowatch -d biowatch_dev -c "..."` returns a
plausible non-zero count, and a few sample rows
(`scientific_name`, `event_date`) look like real species data.

If GBIF *still* rejects the hull (unexpected, but possible for some other
reason): report back with the exact error — do not attempt a second fallback
(e.g. bounding box) speculatively; that would need its own scoped task.

- [ ] **Step 7: Commit**

```bash
git add packages/species/gbif_occurrences.py tests/packages/species/test_gbif_occurrences.py
git commit -m "fix: use AOI convex hull for GBIF geometry filter

GBIF rejects the idf AOI's exact polygon (466 vertices, ~15KB WKT) with
a 400 -- confirmed against the real API, the request line trips a
proxy/servlet URI-length limit. The convex hull (22 vertices, 733-byte
WKT, +15.5% area) fixes this safely: any occurrence outside the AOI's
real shape is already discarded by the existing zones_hex membership
check, so the extra hull area only widens the GBIF fetch, never what
gets persisted."
```

---

### Task 11: GBIF rate-limit handling (429 retry with backoff)

**Added after Task 10's real re-verification:** the convex-hull fix resolved
every `400` (dozens of consecutive `200 OK` responses confirmed, zero `400`s)
but both full runs against real GBIF still failed end-to-end on `429 Too Many
Requests` — `fetch_occurrences` has no retry/backoff and paginates with zero
delay between requests (hit at offset=2700 on run 1, offset=5400 on run 2).
This is a second, orthogonal real-world gap: the job cannot currently
complete a full 10-year fetch against the real API at all. Fix: honor GBIF's
`Retry-After` response header when present (exact seconds to wait), fall
back to exponential backoff when absent, bounded by a max retry count so a
persistently-limited request still fails loudly instead of looping forever.
Non-429 errors (4xx/5xx) must NOT retry — those aren't transient, retrying
them would just waste time before the existing immediate-raise behavior.

**Files:**
- Modify: `packages/clients/gbif/client.py`
- Modify: `tests/packages/clients/test_gbif_client.py`

**Interfaces:**
- No change to `fetch_occurrences`'s public signature.
- New internal constants: `GBIF_MAX_RETRIES = 5`, `GBIF_DEFAULT_RETRY_SECONDS = 2.0`.
- New internal helpers: `_retry_delay_seconds(response: httpx.Response, attempt: int) -> float`,
  `_get_with_retry(http_client: httpx.Client, params: dict, year: int, offset: int) -> httpx.Response`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/packages/clients/test_gbif_client.py`:

```python
def test_fetch_occurrences_retries_on_429_with_retry_after_header(monkeypatch):
    responses = [
        httpx.Response(
            429,
            headers={"Retry-After": "3"},
            request=httpx.Request("GET", GBIF_SEARCH_URL),
        ),
        _response([{"key": 1}], end_of_records=True),
    ]
    sleeps = []

    def fake_get(self, url, params=None, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr("clients.gbif.client.time.sleep", lambda s: sleeps.append(s))

    records = list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020))

    assert [r["key"] for r in records] == [1]
    assert sleeps == [3.0]


def test_fetch_occurrences_retries_on_429_without_retry_after_uses_backoff(monkeypatch):
    responses = [
        httpx.Response(429, request=httpx.Request("GET", GBIF_SEARCH_URL)),
        _response([{"key": 1}], end_of_records=True),
    ]
    sleeps = []

    def fake_get(self, url, params=None, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr("clients.gbif.client.time.sleep", lambda s: sleeps.append(s))

    records = list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020))

    assert [r["key"] for r in records] == [1]
    assert sleeps == [2.0]  # GBIF_DEFAULT_RETRY_SECONDS * 2**0


def test_fetch_occurrences_raises_after_max_retries_on_persistent_429(monkeypatch):
    call_count = {"n": 0}

    def fake_get(self, url, params=None, **kwargs):
        call_count["n"] += 1
        return httpx.Response(429, request=httpx.Request("GET", GBIF_SEARCH_URL))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr("clients.gbif.client.time.sleep", lambda s: None)

    with pytest.raises(GbifApiError):
        list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020))

    assert call_count["n"] == GBIF_MAX_RETRIES + 1


def test_fetch_occurrences_does_not_retry_non_429_errors(monkeypatch):
    call_count = {"n": 0}
    sleeps = []

    def fake_get(self, url, params=None, **kwargs):
        call_count["n"] += 1
        return httpx.Response(500, request=httpx.Request("GET", GBIF_SEARCH_URL))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr("clients.gbif.client.time.sleep", lambda s: sleeps.append(s))

    with pytest.raises(GbifApiError):
        list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020))

    assert call_count["n"] == 1  # no retry for a non-429 error
    assert sleeps == []
```

Add `GBIF_MAX_RETRIES` to the existing `from clients.gbif.client import ...` line at
the top of the test file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/packages/clients/test_gbif_client.py -v -k "retry or backoff or non_429"`
Expected: FAIL — `ImportError` (`GBIF_MAX_RETRIES` doesn't exist yet) or the
429 responses propagate as an uncaught/wrongly-immediate `GbifApiError`
instead of retrying.

- [ ] **Step 3: Implement retry/backoff**

Edit `packages/clients/gbif/client.py`. Add `import time` to the imports, and
add these constants after `GBIF_TIMEOUT_SECONDS`:

```python
GBIF_MAX_RETRIES = 5
GBIF_DEFAULT_RETRY_SECONDS = 2.0
```

Add these two helpers after the `GbifApiError` class, before `fetch_occurrences`:

```python
def _retry_delay_seconds(response: httpx.Response, attempt: int) -> float:
    """Delay before retrying a 429: honor GBIF's Retry-After header when
    present (exact seconds to wait), otherwise exponential backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return GBIF_DEFAULT_RETRY_SECONDS * (2**attempt)


def _get_with_retry(
    http_client: httpx.Client, params: dict, year: int, offset: int
) -> httpx.Response:
    """GET one page, retrying on GBIF's 429 (rate limit) with backoff.

    Non-429 errors are returned as-is (not retried — those aren't
    transient); the caller's existing `raise_for_status()` handles them.
    If still 429 after `GBIF_MAX_RETRIES` attempts, the final 429 response
    is returned and `raise_for_status()` converts it to a `GbifApiError`
    the same way any other error status would be.
    """
    response = http_client.get(GBIF_SEARCH_URL, params=params)
    for attempt in range(GBIF_MAX_RETRIES):
        if response.status_code != 429:
            return response
        delay = _retry_delay_seconds(response, attempt)
        logger.warning(
            "GBIF rate limit hit, retrying",
            extra={
                "event": "gbif.rate_limited",
                "context": {"year": year, "offset": offset, "attempt": attempt, "delay_s": delay},
            },
        )
        time.sleep(delay)
        response = http_client.get(GBIF_SEARCH_URL, params=params)
    return response
```

In `fetch_occurrences`, replace the direct `http_client.get(...)` call:

```python
            response = http_client.get(
                GBIF_SEARCH_URL,
                params={
                    "geometry": geometry_wkt,
                    "year": year,
                    "hasCoordinate": "true",
                    "hasGeospatialIssue": "false",
                    "limit": GBIF_PAGE_LIMIT,
                    "offset": offset,
                },
            )
```

with:

```python
            response = _get_with_retry(
                http_client,
                params={
                    "geometry": geometry_wkt,
                    "year": year,
                    "hasCoordinate": "true",
                    "hasGeospatialIssue": "false",
                    "limit": GBIF_PAGE_LIMIT,
                    "offset": offset,
                },
                year=year,
                offset=offset,
            )
```

The existing `response.raise_for_status()` / `except httpx.HTTPStatusError`
block right after stays unchanged — a still-429 response after retries are
exhausted raises exactly the same `GbifApiError` any other error status
would, no new error path to maintain.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/packages/clients/test_gbif_client.py -v`
Expected: PASS (all tests, including the 4 pre-existing ones from Task 4).

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass (same counts as after Task 10, plus these 4 new tests).

- [ ] **Step 6: Re-run the real smoke test to confirm end-to-end success**

This is the one that matters — the unit tests only prove the retry logic
runs, not that it actually gets the job past GBIF's real rate limiter.

Run: `uv run biowatch-jobs --debug update_gbif_occurrences --aoi idf`

Expected: the job completes this time (`job.success` in the logs) — some
`gbif.rate_limited` warning log lines are fine and expected (GBIF will
likely still rate-limit at points during a full 10-year fetch; the retry
should now get past them instead of failing). Verify:
`SELECT count(*) FROM staging.species_occurrences_raw WHERE aoi_id='idf';`
via `docker exec postgis psql -U biowatch -d biowatch_dev -c "..."` returns a
plausible non-zero count, and sample rows (`scientific_name`, `event_date`)
look like real species data. This full run may take significantly longer
than Task 9/10's partial runs (potentially 10+ minutes, since it now
actually waits out rate limits instead of failing fast) — be patient.

If it *still* fails (a third orthogonal issue, or the retry logic itself
has a bug): report the exact error rather than attempting another fix
speculatively.

- [ ] **Step 7: Commit**

```bash
git add packages/clients/gbif/client.py tests/packages/clients/test_gbif_client.py
git commit -m "fix: retry GBIF 429 responses with backoff

Real end-to-end runs against GBIF hit 429 Too Many Requests reliably
within minutes (zero delay between paginated requests). Honor GBIF's
Retry-After header when present, fall back to exponential backoff
otherwise, bounded by GBIF_MAX_RETRIES so a persistent 429 still fails
loudly. Non-429 errors are unaffected -- still fail immediately."
```
