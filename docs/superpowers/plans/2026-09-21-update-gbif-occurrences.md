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

---

## Pivot: GBIF Download API (v3)

**Everything below supersedes the Search-API approach built in Tasks 4-11's
`fetch_occurrences`/pagination/retry logic.** The final whole-branch review
found GBIF hard-caps `offset + limit` at 100,000 per query (confirmed in
GBIF's own OpenAPI spec: *"To retrieve all records beyond 100,000 you should
use our asynchronous download service instead"*). The `idf` AOI has
200k-650k occurrences/year for 9 of 10 window years — the Search API can
structurally never finish for the only registered AOI. See
`docs/superpowers/specs/2026-09-18-update-gbif-occurrences-design.md`
("v3 — Pivot Search API → Download API") for full rationale.

**What stays as-is, untouched by this pivot:** the DB schema (Task 2), the
`point_to_h3_cell` helper (Task 3), the quality filter (Task 5) — its
`quality_filter(record, date_min, date_max)` signature and expected input
dict shape are unchanged, only *what feeds it* changes (SIMPLE_CSV rows
normalized to the same shape instead of Search API JSON records). CLI
registration (Task 7) needs no change in shape, just verification it still
works after the orchestrator rewrite.

**What's replaced:** `packages/clients/gbif/client.py` (and its tests) —
wholesale rewrite from Search-API pagination to Download-API
request/poll/stream. `packages/species/gbif_occurrences.py` — the per-year
loop becomes a single request covering the whole 10-year window.

**New requirement:** a GBIF.org account (username + password) is needed to
create downloads. Set `GBIF_USERNAME` / `GBIF_PASSWORD` in `.env` (never
committed) before Task 17's real verification.

### Task 12: GBIF Download client (request, poll, predicate)

**Files:**
- Modify (full rewrite): `packages/clients/gbif/client.py`
- Modify (full rewrite): `packages/clients/gbif/__init__.py`
- Modify (full rewrite): `tests/packages/clients/test_gbif_client.py`
- Modify: `packages/clients/__init__.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `clients.build_predicate(geometry_wkt: str, date_min: date, date_max: date) -> dict`
  (pure).
- Produces: `clients.GbifCredentials` (frozen dataclass: `username: str`,
  `password: str`, property `auth -> httpx.BasicAuth`) and
  `clients.load_gbif_credentials() -> GbifCredentials` (reads
  `GBIF_USERNAME`/`GBIF_PASSWORD` from env, raises `RuntimeError` with a
  clear message if either is missing — mirror
  `packages/clients/db/session.py:_build_database_url`'s error style
  exactly).
- Produces: `clients.request_download(predicate: dict, creator: str, auth: httpx.BasicAuth, client: httpx.Client | None = None) -> str`
  (returns the GBIF download key).
- Produces: `clients.wait_for_download(key: str, auth: httpx.BasicAuth, client: httpx.Client | None = None) -> None`
  (blocks until `SUCCEEDED`, raises on failure/timeout).
- Produces: `clients.GbifApiError`, `clients.GbifDownloadTimeoutError` (subclass of `GbifApiError`).
- Task 13 consumes all of the above plus `clients.stream_download` (Task 13's own deliverable, kept in the same file for cohesion but written in that task).

- [ ] **Step 1: Write the failing tests**

Replace the entire contents of `tests/packages/clients/test_gbif_client.py` with:

```python
"""Tests for clients.gbif.client — GBIF Occurrence Download API."""

from datetime import date

import httpx
import pytest

from clients.gbif.client import (
    GBIF_DOWNLOAD_REQUEST_URL,
    GBIF_DOWNLOAD_STATUS_URL,
    GbifApiError,
    GbifCredentials,
    GbifDownloadTimeoutError,
    build_predicate,
    load_gbif_credentials,
    request_download,
    wait_for_download,
)


def test_build_predicate_combines_geometry_and_date_range():
    predicate = build_predicate(
        "POLYGON((0 0,0 1,1 1,1 0,0 0))", date(2016, 1, 1), date(2025, 12, 31)
    )

    assert predicate["type"] == "and"
    sub = predicate["predicates"]
    assert {"type": "within", "geometry": "POLYGON((0 0,0 1,1 1,1 0,0 0))"} in sub
    assert {"type": "greaterThanOrEquals", "key": "EVENT_DATE", "value": "2016-01-01"} in sub
    assert {"type": "lessThanOrEquals", "key": "EVENT_DATE", "value": "2025-12-31"} in sub
    assert {"type": "equals", "key": "HAS_COORDINATE", "value": "true"} in sub
    assert {"type": "equals", "key": "HAS_GEOSPATIAL_ISSUE", "value": "false"} in sub


def test_load_gbif_credentials_reads_env_vars(monkeypatch):
    monkeypatch.setenv("GBIF_USERNAME", "alice")
    monkeypatch.setenv("GBIF_PASSWORD", "secret")

    creds = load_gbif_credentials()

    assert creds.username == "alice"
    assert creds.password == "secret"
    assert isinstance(creds.auth, httpx.BasicAuth)


def test_load_gbif_credentials_raises_on_missing_username(monkeypatch):
    monkeypatch.delenv("GBIF_USERNAME", raising=False)
    monkeypatch.setenv("GBIF_PASSWORD", "secret")

    with pytest.raises(RuntimeError, match="GBIF_USERNAME"):
        load_gbif_credentials()


def test_request_download_returns_key(monkeypatch):
    captured = {}

    def fake_post(self, url, json=None, auth=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        captured["auth"] = auth
        return httpx.Response(
            201, text="0012345-123456789012345", request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    key = request_download(
        {"type": "and", "predicates": []}, creator="alice", auth=httpx.BasicAuth("alice", "secret")
    )

    assert key == "0012345-123456789012345"
    assert captured["url"] == GBIF_DOWNLOAD_REQUEST_URL
    assert captured["json"]["creator"] == "alice"
    assert captured["json"]["format"] == "SIMPLE_CSV"


def test_request_download_raises_on_http_error(monkeypatch):
    def fake_post(self, url, json=None, auth=None, **kwargs):
        return httpx.Response(401, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    with pytest.raises(GbifApiError):
        request_download({"type": "and", "predicates": []}, creator="alice", auth=httpx.BasicAuth("a", "b"))


def test_wait_for_download_returns_on_succeeded(monkeypatch):
    responses = [
        httpx.Response(200, json={"status": "RUNNING"}, request=httpx.Request("GET", GBIF_DOWNLOAD_STATUS_URL)),
        httpx.Response(200, json={"status": "SUCCEEDED"}, request=httpx.Request("GET", GBIF_DOWNLOAD_STATUS_URL)),
    ]
    sleeps = []

    def fake_get(self, url, auth=None, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr("clients.gbif.client.time.sleep", lambda s: sleeps.append(s))

    wait_for_download("key123", auth=httpx.BasicAuth("a", "b"))  # must not raise

    assert len(sleeps) == 1


def test_wait_for_download_raises_on_failed_status(monkeypatch):
    def fake_get(self, url, auth=None, **kwargs):
        return httpx.Response(200, json={"status": "FAILED"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr("clients.gbif.client.time.sleep", lambda s: None)

    with pytest.raises(GbifApiError):
        wait_for_download("key123", auth=httpx.BasicAuth("a", "b"))


def test_wait_for_download_raises_on_timeout(monkeypatch):
    def fake_get(self, url, auth=None, **kwargs):
        return httpx.Response(200, json={"status": "RUNNING"}, request=httpx.Request("GET", url))

    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 10000.0  # jump well past any timeout on every call
        return clock["t"]

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr("clients.gbif.client.time.sleep", lambda s: None)
    monkeypatch.setattr("clients.gbif.client.time.monotonic", fake_monotonic)

    with pytest.raises(GbifDownloadTimeoutError):
        wait_for_download("key123", auth=httpx.BasicAuth("a", "b"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/packages/clients/test_gbif_client.py -v`
Expected: FAIL — `ImportError` (none of these names exist in the current
Search-API-era `client.py` yet).

- [ ] **Step 3: Implement**

Replace the entire contents of `packages/clients/gbif/client.py` with:

```python
"""HTTP client for the GBIF Occurrence Download API.

No business logic here (no quality filtering, no H3, no DB) -- this module
only knows how to build a download predicate, request a download, poll its
status, and (see Task 13) stream-parse the resulting file. See
`species.quality` and `species.gbif_occurrences` for the pipeline built on
top of it.
"""

import logging
import os
import time
from dataclasses import dataclass
from datetime import date

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

GBIF_API_BASE = "https://api.gbif.org/v1"
GBIF_DOWNLOAD_REQUEST_URL = f"{GBIF_API_BASE}/occurrence/download/request"
GBIF_DOWNLOAD_STATUS_URL = f"{GBIF_API_BASE}/occurrence/download"
GBIF_DOWNLOAD_FILE_URL = f"{GBIF_API_BASE}/occurrence/download/request"

GBIF_TIMEOUT_SECONDS = 30.0
GBIF_POLL_INTERVAL_SECONDS = 30.0
GBIF_POLL_TIMEOUT_SECONDS = 3600.0  # 1h ceiling on waiting for a download to finish
GBIF_FAILED_STATUSES = frozenset({"FAILED", "CANCELLED", "KILLED"})


class GbifApiError(Exception):
    """Raised when the GBIF Download API returns an error response."""


class GbifDownloadTimeoutError(GbifApiError):
    """Raised when a download doesn't reach SUCCEEDED within the poll timeout."""


@dataclass(frozen=True)
class GbifCredentials:
    """GBIF Basic Auth credentials. Registration required at gbif.org --
    Basic Auth uses the *username* (not the email) and password."""

    username: str
    password: str

    @property
    def auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.username, self.password)


def load_gbif_credentials() -> GbifCredentials:
    """Load GBIF credentials from the environment (GBIF_USERNAME/GBIF_PASSWORD)."""
    load_dotenv(override=False)
    try:
        username = os.environ["GBIF_USERNAME"]
        password = os.environ["GBIF_PASSWORD"]
    except KeyError as e:
        raise RuntimeError(
            f"Variable d'environnement manquante : {e.args[0]} (voir .env.example)"
        ) from e
    return GbifCredentials(username=username, password=password)


def build_predicate(geometry_wkt: str, date_min: date, date_max: date) -> dict:
    """
    Pure: build the GBIF download predicate for one AOI + date window.

    A single predicate covers the *entire* rolling window in one request --
    no per-year looping needed (unlike the old Search API pagination this
    replaces), since the Download API has no offset cap.
    """
    return {
        "type": "and",
        "predicates": [
            {"type": "within", "geometry": geometry_wkt},
            {"type": "greaterThanOrEquals", "key": "EVENT_DATE", "value": date_min.isoformat()},
            {"type": "lessThanOrEquals", "key": "EVENT_DATE", "value": date_max.isoformat()},
            {"type": "equals", "key": "HAS_COORDINATE", "value": "true"},
            {"type": "equals", "key": "HAS_GEOSPATIAL_ISSUE", "value": "false"},
        ],
    }


def request_download(
    predicate: dict,
    creator: str,
    auth: httpx.BasicAuth,
    client: httpx.Client | None = None,
) -> str:
    """POST a download request, return the GBIF download key."""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=GBIF_TIMEOUT_SECONDS)
    try:
        response = http_client.post(
            GBIF_DOWNLOAD_REQUEST_URL,
            json={"creator": creator, "format": "SIMPLE_CSV", "predicate": predicate},
            auth=auth,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "GBIF download request failed",
                extra={
                    "event": "gbif.download_request_error",
                    "context": {"status_code": response.status_code},
                },
            )
            raise GbifApiError(f"GBIF download request failed: {exc}") from exc
        return response.text.strip()
    finally:
        if owns_client:
            http_client.close()


def wait_for_download(
    key: str,
    auth: httpx.BasicAuth,
    client: httpx.Client | None = None,
) -> None:
    """Poll download status until SUCCEEDED, or raise on failure/timeout."""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=GBIF_TIMEOUT_SECONDS)
    started = time.monotonic()
    try:
        while True:
            response = http_client.get(f"{GBIF_DOWNLOAD_STATUS_URL}/{key}", auth=auth)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise GbifApiError(
                    f"GBIF download status check failed (key={key}): {exc}"
                ) from exc

            status = response.json().get("status")
            if status == "SUCCEEDED":
                return
            if status in GBIF_FAILED_STATUSES:
                raise GbifApiError(f"GBIF download {key} ended in status {status}")
            if time.monotonic() - started > GBIF_POLL_TIMEOUT_SECONDS:
                raise GbifDownloadTimeoutError(
                    f"GBIF download {key} did not succeed within "
                    f"{GBIF_POLL_TIMEOUT_SECONDS}s (last status: {status})"
                )
            logger.info(
                "GBIF download in progress",
                extra={"event": "gbif.download_polling", "context": {"key": key, "status": status}},
            )
            time.sleep(GBIF_POLL_INTERVAL_SECONDS)
    finally:
        if owns_client:
            http_client.close()
```

Replace `packages/clients/gbif/__init__.py` with:

```python
from .client import (
    GbifApiError,
    GbifCredentials,
    GbifDownloadTimeoutError,
    build_predicate,
    load_gbif_credentials,
    request_download,
    wait_for_download,
)

__all__ = [
    "build_predicate",
    "load_gbif_credentials",
    "request_download",
    "wait_for_download",
    "GbifApiError",
    "GbifCredentials",
    "GbifDownloadTimeoutError",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/packages/clients/test_gbif_client.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 5: Update exports and `.env.example`**

Edit `packages/clients/__init__.py`: remove the old
`from .gbif import GBIF_PAGE_LIMIT, GbifApiError, fetch_occurrences` line and
its `__all__` entries (`"fetch_occurrences"`, `"GbifApiError"`,
`"GBIF_PAGE_LIMIT"`); replace with:

```python
from .gbif import (
    GbifApiError,
    GbifCredentials,
    GbifDownloadTimeoutError,
    build_predicate,
    load_gbif_credentials,
    request_download,
    wait_for_download,
)
```

and add `"build_predicate"`, `"load_gbif_credentials"`, `"request_download"`,
`"wait_for_download"`, `"GbifCredentials"`, `"GbifDownloadTimeoutError"` to
`__all__` (keep `"GbifApiError"`, it's unchanged).

Edit `.env.example`, add after the Notion token section:

```
# GBIF account credentials, required to create Occurrence Downloads
# (register at https://www.gbif.org — use your username, not your email).
GBIF_USERNAME=...
GBIF_PASSWORD=...
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: `packages/species/gbif_occurrences.py` will now fail to import
(`fetch_occurrences` no longer exists in `clients`) — this is expected and
fixed in Task 14. For this step, run only the client and unaffected tests:
`uv run pytest tests/packages/clients/test_gbif_client.py tests/packages/core tests/packages/geo tests/packages/species/test_quality.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add packages/clients/gbif tests/packages/clients/test_gbif_client.py \
        packages/clients/__init__.py .env.example
git commit -m "feat: rewrite GBIF client for the Download API

Occurrence Search API caps offset+limit at 100,000 -- structurally
incompatible with idf's real volume (200k-650k occurrences/year for
9 of 10 window years). Occurrence Download API has no such cap and is
what GBIF's own docs recommend past that threshold: one predicate
(geometry + full date range) per run instead of per-year search
pagination. This commit lands the request/poll half; Task 13 adds the
streamed file download + parse."
```

---

### Task 13: GBIF Download client — streaming file download

**Files:**
- Modify: `packages/clients/gbif/client.py`
- Modify: `packages/clients/gbif/__init__.py`
- Modify: `tests/packages/clients/test_gbif_client.py`
- Modify: `packages/clients/__init__.py`

**Interfaces:**
- Consumes: nothing new from Task 12 directly (this is the remaining piece
  of the same client module).
- Produces: `clients.stream_download(key: str, auth: httpx.BasicAuth, client: httpx.Client | None = None) -> Iterator[dict]`
  — downloads the finished SIMPLE_CSV(.gz) file to a temp path, streams it
  to disk (never the whole file in memory), then yields one `dict` per CSV
  row (parsed via `csv.DictReader`, tab-delimited, header-driven). Deletes
  the temp file once the generator is exhausted.

- [ ] **Step 1: Write the failing tests**

Append to `tests/packages/clients/test_gbif_client.py` (add `csv`, `gzip`,
`io` to imports, and add `stream_download`, `GBIF_DOWNLOAD_FILE_URL` to the
`from clients.gbif.client import (...)` line):

```python
def _gzipped_tsv(rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]), delimiter="\t")
    writer.writeheader()
    writer.writerows(rows)
    return gzip.compress(buf.getvalue().encode("utf-8"))


def test_stream_download_yields_parsed_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    body = _gzipped_tsv(
        [
            {"gbifID": "1001", "scientificName": "Vulpes vulpes", "taxonKey": "5219243",
             "decimalLatitude": "48.8566", "decimalLongitude": "2.3522", "eventDate": "2020-06-15"},
            {"gbifID": "1002", "scientificName": "Bubo bubo", "taxonKey": "2497252",
             "decimalLatitude": "45.0", "decimalLongitude": "3.0", "eventDate": "2019-03-01"},
        ]
    )

    class _FakeStreamResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield body

    def fake_stream(self, method, url, auth=None, **kwargs):
        return _FakeStreamResponse()

    monkeypatch.setattr(httpx.Client, "stream", fake_stream)

    rows = list(stream_download("key123", auth=httpx.BasicAuth("a", "b")))

    assert [r["gbifID"] for r in rows] == ["1001", "1002"]
    assert rows[0]["scientificName"] == "Vulpes vulpes"


def test_stream_download_raises_on_http_error(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    class _FakeErrorResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", GBIF_DOWNLOAD_FILE_URL), response=httpx.Response(404)
            )

        def iter_bytes(self):
            return iter([])

    def fake_stream(self, method, url, auth=None, **kwargs):
        return _FakeErrorResponse()

    monkeypatch.setattr(httpx.Client, "stream", fake_stream)

    with pytest.raises(GbifApiError):
        list(stream_download("key123", auth=httpx.BasicAuth("a", "b")))
```

Add `import csv`, `import gzip`, `import io`, `import tempfile` to the top of
the test file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/packages/clients/test_gbif_client.py -v -k stream_download`
Expected: FAIL — `ImportError: cannot import name 'stream_download'`.

- [ ] **Step 3: Implement**

Add to `packages/clients/gbif/client.py` (add `import csv`, `import gzip`,
`import tempfile` to the top imports, `from collections.abc import Iterator`,
`from pathlib import Path`):

```python
def stream_download(
    key: str,
    auth: httpx.BasicAuth,
    client: httpx.Client | None = None,
) -> Iterator[dict]:
    """
    Download the finished SIMPLE_CSV(.gz) file and yield one dict per row.

    Streams to a temp file on disk (the file can be hundreds of MB for a
    large AOI/window), then parses it as gzip-compressed, tab-separated CSV
    with a header row -- the whole file is never held in memory at once.
    The temp file is deleted once the generator is exhausted (or closed).
    """
    owns_client = client is None
    http_client = client or httpx.Client(timeout=GBIF_TIMEOUT_SECONDS, follow_redirects=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".csv.gz", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            with http_client.stream(
                "GET", f"{GBIF_DOWNLOAD_FILE_URL}/{key}", auth=auth
            ) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise GbifApiError(
                        f"GBIF download file fetch failed (key={key}): {exc}"
                    ) from exc
                for chunk in response.iter_bytes():
                    tmp.write(chunk)

        with gzip.open(tmp_path, mode="rt", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            yield from reader
    finally:
        if owns_client:
            http_client.close()
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/packages/clients/test_gbif_client.py -v`
Expected: PASS (all 9 tests).

- [ ] **Step 5: Export it**

Edit `packages/clients/gbif/__init__.py` — add `stream_download` to the
import from `.client` and to `__all__`.

Edit `packages/clients/__init__.py` — add `stream_download` to the import
from `.gbif` and to `__all__`.

- [ ] **Step 6: Run the full suite (client + unaffected packages)**

Run: `uv run pytest tests/packages/clients/test_gbif_client.py tests/packages/core tests/packages/geo tests/packages/species/test_quality.py -q`
Expected: all pass. (`species.gbif_occurrences` is still broken until Task 14 — expected.)

- [ ] **Step 7: Commit**

```bash
git add packages/clients/gbif/client.py packages/clients/gbif/__init__.py \
        tests/packages/clients/test_gbif_client.py packages/clients/__init__.py
git commit -m "feat: stream-parse GBIF Download API SIMPLE_CSV files

Downloads can be hundreds of MB for a real AOI's 10-year window --
stream to a temp file and parse it as a generator (csv.DictReader over
a gzip stream) instead of loading it into memory."
```

---

### Task 14: Rewrite the job orchestrator for the Download API

**Files:**
- Modify (full rewrite): `packages/species/gbif_occurrences.py`
- Modify (full rewrite): `tests/packages/species/test_gbif_occurrences.py`

**Interfaces:**
- Consumes: `clients.build_predicate`, `clients.load_gbif_credentials`,
  `clients.request_download`, `clients.wait_for_download`,
  `clients.stream_download`, `clients.GbifApiError` (all from Tasks 12-13);
  `clients.SpeciesOccurrencesRaw`, `clients.ZonesHex`, `clients.job_run`,
  `clients.JobAlreadySucceeded`, `clients.upsert` (pre-existing);
  `geo.point_to_h3_cell` (Task 3); `species.quality.quality_filter`
  (Task 5, unchanged); `core.compute_idempotency_key` (pre-existing, already
  has a `resolution` param — see spec's Idempotence section).
- Produces: `species.update_gbif_occurrences(aoi_label: AoiLabel, engine: Engine | None = None) -> None`
  (same public signature as before this pivot — nothing calling this
  function needs to change).

- [ ] **Step 1: Write the failing tests**

Replace the entire contents of `tests/packages/species/test_gbif_occurrences.py`:

```python
"""Tests for species.gbif_occurrences.update_gbif_occurrences (Download API)."""

import gzip
import io
import os
import uuid
from contextlib import contextmanager
from datetime import date

import httpx
import pytest
from sqlalchemy import create_engine, delete, func, select

import species.gbif_occurrences as gbif_occurrences
from clients import GbifApiError, JobAlreadySucceeded, SpeciesOccurrencesRaw

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


def test_normalize_csv_row_maps_gbif_id_to_key():
    row = {
        "gbifID": "1001",
        "scientificName": "Vulpes vulpes",
        "taxonKey": "5219243",
        "decimalLatitude": "48.8566",
        "decimalLongitude": "2.3522",
        "eventDate": "2020-06-15",
    }
    normalized = gbif_occurrences._normalize_csv_row(row)
    assert normalized == {
        "key": "1001",
        "scientificName": "Vulpes vulpes",
        "taxonKey": 5219243,
        "decimalLatitude": 48.8566,
        "decimalLongitude": 2.3522,
        "eventDate": "2020-06-15",
    }


def test_normalize_csv_row_handles_missing_optional_fields():
    row = {"gbifID": "", "scientificName": "", "taxonKey": "", "decimalLatitude": "",
           "decimalLongitude": "", "eventDate": ""}
    normalized = gbif_occurrences._normalize_csv_row(row)
    assert normalized == {
        "key": None, "scientificName": None, "taxonKey": None,
        "decimalLatitude": None, "decimalLongitude": None, "eventDate": None,
    }


def _force_default_res(aoi, resolution):
    return aoi.__class__(**{**aoi.__dict__, "default_res": resolution})


class _FakeScalarsResult:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _FakeSession:
    def __init__(self, zone_ids):
        self._zone_ids = zone_ids
        self.executed_statements: list[object] = []

    def execute(self, stmt):
        self.executed_statements.append(stmt)

    def scalars(self, stmt):
        return _FakeScalarsResult(self._zone_ids)


def _fake_job_run_yielding(session):
    @contextmanager
    def fake_job_run(*args, idempotency_key="", **kwargs):
        yield None, session

    return fake_job_run


_GOOD_CSV_ROW = {
    "gbifID": "42",
    "scientificName": "Vulpes vulpes",
    "taxonKey": "5219243",
    "decimalLatitude": "48.8566",
    "decimalLongitude": "2.3522",
    "eventDate": "2020-06-15",
}

_FAKE_CREDS = type("_Creds", (), {"username": "alice", "auth": httpx.BasicAuth("alice", "s")})()


def _patch_download_pipeline(monkeypatch, rows, download_key="key123"):
    """Patch the request/poll/stream trio so update_gbif_occurrences's HTTP
    calls never happen -- `rows` is the list of raw CSV-row dicts `stream_download`
    should yield."""
    monkeypatch.setattr(gbif_occurrences, "load_gbif_credentials", lambda: _FAKE_CREDS)
    monkeypatch.setattr(gbif_occurrences, "request_download", lambda *a, **k: download_key)
    monkeypatch.setattr(gbif_occurrences, "wait_for_download", lambda *a, **k: None)
    monkeypatch.setattr(gbif_occurrences, "stream_download", lambda *a, **k: iter(rows))


def test_update_gbif_occurrences_skips_record_failing_quality_filter(monkeypatch):
    captured_rows: list[dict[str, object]] = []

    def fake_upsert(session, model, rows, update_columns=None):
        captured_rows.extend(rows)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    session = _FakeSession(zone_ids={"expected-zone"})
    monkeypatch.setattr(gbif_occurrences, "job_run", _fake_job_run_yielding(session))
    monkeypatch.setattr(gbif_occurrences, "upsert", fake_upsert)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: aoi)
    monkeypatch.setattr(gbif_occurrences, "point_to_h3_cell", lambda lat, lon, res: "expected-zone")

    bad_row = {"gbifID": "", "scientificName": "no id"}  # missing gbifID -> quality_filter -> None
    _patch_download_pipeline(monkeypatch, [bad_row, _GOOD_CSV_ROW])

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)

    assert len(captured_rows) == 1
    assert captured_rows[0]["gbif_occurrence_id"] == "42"


def test_update_gbif_occurrences_skips_record_outside_valid_zone_ids(monkeypatch):
    captured_rows: list[dict[str, object]] = []

    def fake_upsert(session, model, rows, update_columns=None):
        captured_rows.extend(rows)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    session = _FakeSession(zone_ids=set())  # nothing is a valid zone
    monkeypatch.setattr(gbif_occurrences, "job_run", _fake_job_run_yielding(session))
    monkeypatch.setattr(gbif_occurrences, "upsert", fake_upsert)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: aoi)
    monkeypatch.setattr(gbif_occurrences, "point_to_h3_cell", lambda lat, lon, res: "outside-zone")
    _patch_download_pipeline(monkeypatch, [_GOOD_CSV_ROW])

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)

    assert captured_rows == []


def test_update_gbif_occurrences_builds_predicate_with_convex_hull(monkeypatch):
    """GBIF rejects the idf AOI's raw polygon (466 vertices) -- the convex
    hull (22 vertices) must be what's sent, same reasoning as the old Search
    API fix, now applied to the `within` predicate instead of the `geometry`
    query param."""
    captured = {}

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    session = _FakeSession(zone_ids=set())
    monkeypatch.setattr(gbif_occurrences, "job_run", _fake_job_run_yielding(session))
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: aoi)
    monkeypatch.setattr(gbif_occurrences, "load_gbif_credentials", lambda: _FAKE_CREDS)

    def fake_request_download(predicate, creator, auth, client=None):
        captured["predicate"] = predicate
        return "key123"

    monkeypatch.setattr(gbif_occurrences, "request_download", fake_request_download)
    monkeypatch.setattr(gbif_occurrences, "wait_for_download", lambda *a, **k: None)
    monkeypatch.setattr(gbif_occurrences, "stream_download", lambda *a, **k: iter([]))

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)

    within = next(p for p in captured["predicate"]["predicates"] if p["type"] == "within")
    assert within["geometry"] == aoi.geom.convex_hull.wkt
    assert within["geometry"] != aoi.geom.wkt


def test_update_gbif_occurrences_logs_and_reraises_gbif_api_error(monkeypatch):
    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    session = _FakeSession(zone_ids=set())
    monkeypatch.setattr(gbif_occurrences, "job_run", _fake_job_run_yielding(session))
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: aoi)
    monkeypatch.setattr(gbif_occurrences, "load_gbif_credentials", lambda: _FAKE_CREDS)

    def fake_request_download(*a, **k):
        raise GbifApiError("simulated GBIF outage")

    monkeypatch.setattr(gbif_occurrences, "request_download", fake_request_download)

    with pytest.raises(GbifApiError):
        gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)


def test_update_gbif_occurrences_upserts_in_batches(monkeypatch):
    """Rows are flushed to `upsert` in bounded batches, not one call for the
    whole stream -- covers the memory-safety fix from the final review."""
    upsert_calls: list[int] = []

    def fake_upsert(session, model, rows, update_columns=None):
        upsert_calls.append(len(rows))

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    session = _FakeSession(zone_ids={"expected-zone"})
    monkeypatch.setattr(gbif_occurrences, "job_run", _fake_job_run_yielding(session))
    monkeypatch.setattr(gbif_occurrences, "upsert", fake_upsert)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: aoi)
    monkeypatch.setattr(gbif_occurrences, "point_to_h3_cell", lambda lat, lon, res: "expected-zone")
    monkeypatch.setattr(gbif_occurrences, "UPSERT_BATCH_SIZE", 2)

    rows = [{**_GOOD_CSV_ROW, "gbifID": str(i)} for i in range(5)]
    _patch_download_pipeline(monkeypatch, rows)

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI)

    assert upsert_calls == [2, 2, 1]  # 5 rows, batch size 2 -> 2+2+1


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_purges_rows_outside_window_scoped_to_aoi(monkeypatch):
    """Spec-mandated purge test (missing in the previous iteration, flagged
    by final review): out-of-window rows for this AOI are deleted, in-window
    rows survive, and another AOI's rows are never touched."""
    from clients.db import ZonesHex
    from clients.db.init_db import init_db

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)

    with engine.begin() as conn:
        conn.execute(delete(SpeciesOccurrencesRaw))
        conn.execute(delete(ZonesHex).where(ZonesHex.zone_id.in_(["z-in", "z-out", "z-other"])))

    from geo.h3_grid import generate_h3_grid

    generate_h3_grid(GOLDEN_AOI, resolution=5, engine=engine)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: _force_default_res(aoi, 5))
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    _patch_download_pipeline(monkeypatch, [])  # no new fetch needed for this test

    with engine.begin() as conn:
        conn.execute(
            SpeciesOccurrencesRaw.__table__.insert(),
            [
                {  # in-window, this AOI -> must survive
                    "gbif_occurrence_id": "in-window", "event_date": date(2020, 1, 1),
                    "gbif_taxon_key": None, "scientific_name": "A",
                    "decimal_latitude": 1.0, "decimal_longitude": 1.0,
                    "zone_id": "z-in", "aoi_id": GOLDEN_AOI,
                },
                {  # out-of-window, this AOI -> must be purged
                    "gbif_occurrence_id": "out-of-window", "event_date": date(2000, 1, 1),
                    "gbif_taxon_key": None, "scientific_name": "B",
                    "decimal_latitude": 1.0, "decimal_longitude": 1.0,
                    "zone_id": "z-out", "aoi_id": GOLDEN_AOI,
                },
                {  # out-of-window, OTHER AOI -> must survive (not this AOI's purge)
                    "gbif_occurrence_id": "other-aoi-old", "event_date": date(2000, 1, 1),
                    "gbif_taxon_key": None, "scientific_name": "C",
                    "decimal_latitude": 1.0, "decimal_longitude": 1.0,
                    "zone_id": "z-other", "aoi_id": "not-idf",
                },
            ],
        )

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)

    with engine.connect() as conn:
        remaining_ids = set(
            conn.scalars(select(SpeciesOccurrencesRaw.gbif_occurrence_id)).all()
        )

    assert remaining_ids == {"in-window", "other-aoi-old"}


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_update_gbif_occurrences_skips_rerun_same_year(monkeypatch):
    """Job-level idempotence: rerunning for the same target year is a no-op skip."""
    from clients.db import JobRun
    from clients.db.init_db import init_db

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)

    with engine.begin() as conn:
        conn.execute(delete(SpeciesOccurrencesRaw).where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI))
        conn.execute(delete(JobRun).where(JobRun.job_name == "update_gbif_occurrences"))

    from geo.h3_grid import generate_h3_grid

    generate_h3_grid(GOLDEN_AOI, resolution=5, engine=engine)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: _force_default_res(aoi, 5))
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    _patch_download_pipeline(monkeypatch, [{**_GOOD_CSV_ROW, "gbifID": "1001"}])

    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)  # same year -> skip

    with engine.connect() as conn:
        count = conn.scalar(
            select(func.count()).select_from(SpeciesOccurrencesRaw)
            .where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI)
        )
        runs = conn.scalar(
            select(func.count()).select_from(JobRun)
            .where(JobRun.job_name == "update_gbif_occurrences")
        )

    assert count == 1
    assert runs == 1


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_update_gbif_occurrences_row_level_dedup_across_overlapping_windows(monkeypatch):
    """Row-level dedup: two different job runs (different target years,
    overlapping windows) both attempt to upsert the same occurrence -- must
    not raise, must not duplicate, must not overwrite (DO NOTHING)."""
    from clients.db import JobRun
    from clients.db.init_db import init_db

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)

    with engine.begin() as conn:
        conn.execute(delete(SpeciesOccurrencesRaw).where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI))
        conn.execute(delete(JobRun).where(JobRun.job_name == "update_gbif_occurrences"))

    from geo.h3_grid import generate_h3_grid

    generate_h3_grid(GOLDEN_AOI, resolution=5, engine=engine)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: _force_default_res(aoi, 5))

    original_row = {**_GOOD_CSV_ROW, "gbifID": "1001"}
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2024, 1, 1))
    _patch_download_pipeline(monkeypatch, [original_row])
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)

    conflicting_row = {**original_row, "scientificName": "SHOULD NOT OVERWRITE"}
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    _patch_download_pipeline(monkeypatch, [conflicting_row])
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)  # must not raise

    with engine.connect() as conn:
        count = conn.scalar(
            select(func.count()).select_from(SpeciesOccurrencesRaw)
            .where(SpeciesOccurrencesRaw.gbif_occurrence_id == "1001")
        )
        successful_runs = conn.scalar(
            select(func.count()).select_from(JobRun)
            .where(JobRun.job_name == "update_gbif_occurrences", JobRun.status == "success")
        )
        row = conn.execute(
            select(SpeciesOccurrencesRaw).where(SpeciesOccurrencesRaw.gbif_occurrence_id == "1001")
        ).one()

    assert successful_runs == 2
    assert count == 1
    assert row.scientific_name == "Vulpes vulpes"


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="requires a live PostGIS database (set TEST_DATABASE_URL)",
)
def test_update_gbif_occurrences_new_run_when_resolution_changes(monkeypatch):
    """Idempotency-key finding from final review: changing the AOI's
    resolution for the same target year must NOT be skipped as a rerun."""
    from clients.db import JobRun
    from clients.db.init_db import init_db

    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    init_db(engine)

    with engine.begin() as conn:
        conn.execute(delete(SpeciesOccurrencesRaw).where(SpeciesOccurrencesRaw.aoi_id == GOLDEN_AOI))
        conn.execute(delete(JobRun).where(JobRun.job_name == "update_gbif_occurrences"))

    from geo.h3_grid import generate_h3_grid

    generate_h3_grid(GOLDEN_AOI, resolution=5, engine=engine)
    generate_h3_grid(GOLDEN_AOI, resolution=6, engine=engine)

    aoi = gbif_occurrences.load_aoi(GOLDEN_AOI)
    monkeypatch.setattr(gbif_occurrences, "biowatch_now", lambda: date(2025, 1, 1))
    _patch_download_pipeline(monkeypatch, [])

    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: _force_default_res(aoi, 5))
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)

    monkeypatch.setattr(gbif_occurrences, "load_aoi", lambda label: _force_default_res(aoi, 6))
    gbif_occurrences.update_gbif_occurrences(GOLDEN_AOI, engine=engine)  # must NOT skip

    with engine.connect() as conn:
        runs = conn.scalar(
            select(func.count()).select_from(JobRun)
            .where(JobRun.job_name == "update_gbif_occurrences", JobRun.status == "success")
        )

    assert runs == 2  # different resolution -> different idempotency_key -> both ran
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/packages/species/test_gbif_occurrences.py -v`
Expected: FAIL — `ImportError` (`gbif_occurrences.py` still imports
`fetch_occurrences` from `clients`, which no longer exists after Task 12).

- [ ] **Step 3: Implement**

Replace the entire contents of `packages/species/gbif_occurrences.py`:

```python
"""update_gbif_occurrences job: GBIF Download API -> staging.species_occurrences_raw.

Fetches GBIF occurrences for an AOI over a rolling 10-year window via one
Occurrence Download request (predicate = AOI geometry + full date range --
no per-year pagination, since the Download API has no offset cap, unlike
the Search API this replaced), applies a quality filter, converts points to
H3 `zone_id`, and upserts them into `staging.species_occurrences_raw`. Runs
once a year: for year N, the window is [N-9, N].
"""

import logging
from datetime import date

from clients import (
    GbifApiError,
    JobAlreadySucceeded,
    SpeciesOccurrencesRaw,
    ZonesHex,
    build_predicate,
    job_run,
    load_gbif_credentials,
    request_download,
    stream_download,
    upsert,
    wait_for_download,
)
from core import AOI, AoiLabel, biowatch_now, compute_idempotency_key, load_aoi
from geo import point_to_h3_cell
from sqlalchemy import Engine, delete, select

from species.quality import quality_filter

logger = logging.getLogger(__name__)

GBIF_SOURCE_VERSION_PREFIX = "gbif-download-v1"
WINDOW_YEARS = 10
UPSERT_BATCH_SIZE = 5000


def rolling_window(current_year: int) -> tuple[date, date]:
    """Return (date_min, date_max) for the 10-year window ending at current_year."""
    return date(current_year - (WINDOW_YEARS - 1), 1, 1), date(current_year, 12, 31)


def _to_float(value: str | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _normalize_csv_row(row: dict) -> dict:
    """
    Normalize one SIMPLE_CSV row into the dict shape `quality_filter` expects
    (the same field names the old Search API JSON records used) -- keeps
    `quality_filter` itself untouched by this pivot.
    """
    return {
        "key": row.get("gbifID") or None,
        "scientificName": row.get("scientificName") or None,
        "taxonKey": _to_int(row.get("taxonKey")),
        "decimalLatitude": _to_float(row.get("decimalLatitude")),
        "decimalLongitude": _to_float(row.get("decimalLongitude")),
        "eventDate": row.get("eventDate") or None,
    }


def update_gbif_occurrences(aoi_label: AoiLabel, engine: Engine | None = None) -> None:
    """
    Fetch, filter, and store GBIF occurrences for an AOI's rolling 10-year window.

    Idempotent: guarded by `job_run` (one run per AOI + target year N + AOI
    version + resolution -- skip if already succeeded) and written via
    `upsert(..., update_columns=[])` (`ON CONFLICT DO NOTHING` -- a GBIF
    occurrence is an immutable historical record, re-running never needs to
    overwrite an existing row).
    """
    aoi: AOI = load_aoi(aoi_label)
    current_year = biowatch_now().year
    date_min, date_max = rolling_window(current_year)

    idempotency_key = compute_idempotency_key(
        job_name="update_gbif_occurrences",
        scope=aoi_label,
        period=str(current_year),
        source_version=f"{GBIF_SOURCE_VERSION_PREFIX}+aoi{aoi.version}",
        resolution=aoi.default_res,
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
            # Same reasoning as the old Search API's `geometry` param: the
            # exact polygon is too complex for GBIF, the convex hull is
            # small and always contains it. Safe here too -- occurrences
            # outside the AOI's real shape are still filtered by the
            # zone-membership check below.
            geometry_wkt = aoi.geom.convex_hull.wkt
            predicate = build_predicate(geometry_wkt, date_min, date_max)
            creds = load_gbif_credentials()

            try:
                download_key = request_download(predicate, creator=creds.username, auth=creds.auth)
                logger.info(
                    "GBIF download requested",
                    extra={
                        "event": "gbif_occurrences.download_requested",
                        "context": {**log_ctx, "download_key": download_key},
                    },
                )
                wait_for_download(download_key, auth=creds.auth)
                logger.info(
                    "GBIF download ready",
                    extra={
                        "event": "gbif_occurrences.download_ready",
                        "context": {**log_ctx, "download_key": download_key},
                    },
                )

                rows: list[dict[str, object]] = []
                n_total = 0
                for raw_row in stream_download(download_key, auth=creds.auth):
                    record = _normalize_csv_row(raw_row)
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
                    if len(rows) >= UPSERT_BATCH_SIZE:
                        upsert(session=session, model=SpeciesOccurrencesRaw, rows=rows, update_columns=[])
                        n_total += len(rows)
                        rows = []
                if rows:
                    upsert(session=session, model=SpeciesOccurrencesRaw, rows=rows, update_columns=[])
                    n_total += len(rows)

                logger.info(
                    "upserted GBIF occurrences",
                    extra={
                        "event": "gbif_occurrences.upserted",
                        "context": {**log_ctx, "download_key": download_key, "n_rows": n_total},
                    },
                )
            except GbifApiError:
                logger.error(
                    "GBIF download failed",
                    extra={"event": "gbif_occurrences.download_error", "context": log_ctx},
                )
                raise

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/packages/species/test_gbif_occurrences.py -v`
Expected: PASS (all non-DB-gated tests; DB-gated ones skip without
`TEST_DATABASE_URL`).

- [ ] **Step 5: Run the DB-gated tests too**

Set `TEST_DATABASE_URL` (PowerShell:
`$env:TEST_DATABASE_URL="postgresql+psycopg://biowatch:biowatch_dev@127.0.0.1:5433/biowatch_dev"`)
and re-run: `uv run pytest tests/packages/species/test_gbif_occurrences.py -v`
Expected: PASS (all tests, including the 4 DB-gated ones — the new purge
test and the new resolution-change test are the ones added in this task).

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q` (with `TEST_DATABASE_URL` still set, to exercise
everything)
Expected: all pass, aside from the already-known, out-of-scope
`test_generate_h3_grid_is_idempotent` pollution issue if it's still present
in the shared dev DB (unrelated to this task — see the ledger).

- [ ] **Step 7: Commit**

```bash
git add packages/species/gbif_occurrences.py tests/packages/species/test_gbif_occurrences.py
git commit -m "feat: rewrite update_gbif_occurrences for the Download API

Replaces the per-year Search API pagination loop with a single
predicate-based download request covering the whole 10-year window.
Also fixes two final-review findings: the purge now has a dedicated
test (previously untested despite being the only destructive
operation in the codebase), and the idempotency key now includes the
AOI's resolution and version, so a grid/geometry change forces a
fresh run instead of silently mixing stale zone_ids."
```

---

### Task 15: Docs — README, CLAUDE.md, credentials setup

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Update README**

In `README.md`'s `## CLI` section, next to the existing
`update_gbif_occurrences` example line, add a short note:

```
Nécessite un compte GBIF (https://www.gbif.org) : ajoute `GBIF_USERNAME`
et `GBIF_PASSWORD` dans `.env` avant de lancer ce job (voir
`.env.example`). Ce job appelle l'API Download de GBIF (asynchrone) : il
peut prendre plusieurs minutes selon le volume de l'AOI. Si la DB existante
n'a pas encore le schéma `staging`, relancer `uv run biowatch-jobs init_db`
avant.
```

- [ ] **Step 2: Update CLAUDE.md**

In the "Layout" list (the `packages/*` bullet list under "Stack & structure
logicielle"), add a line for `packages/species/` right after
`packages/geo/`:

```
- `packages/species/` — logique métier biodiversité (ingestion GBIF,
  agrégation par zone)
```

In the "Tables" markdown table (under "Modèle de données & vocabulaire
métier"), add a row for the new staging table, right after the `zones_hex`
row (or wherever fits the table's existing ordering):

```
| `staging.species_occurrences_raw` | Occurrences GBIF brutes par AOI (fenêtre glissante 10 ans) | PK `gbif_occurrence_id`, `zone_id`, `aoi_id`, `event_date` |
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document GBIF credentials setup and new species package/table"
```

---

### Task 16: Coverage check (re-run after the pivot)

**Files:** none (verification only; may add tests to existing files if a gap is found).

- [ ] **Step 1: Measure coverage on the new/changed modules**

Run (with `TEST_DATABASE_URL` set, so the DB-gated tests are included):
```bash
uv run pytest --cov=packages/species --cov=packages/clients/gbif --cov-report=term-missing \
  tests/packages/species tests/packages/clients/test_gbif_client.py \
  tests/packages/geo/test_h3_grid.py -q
```

- [ ] **Step 2: Close any gap below 90%**

If a module is under 90%, read the `Missing` line numbers, add a targeted
test case to the relevant file from Tasks 12-14 (mirror the existing test
style — one case per branch). Re-run Step 1 until every listed module is
≥90%. Pay particular attention to `wait_for_download`'s timeout branch and
`stream_download`'s cleanup path — these are easy to under-test.

- [ ] **Step 3: Run the entire repo suite one last time**

Run: `uv run pytest -q` (both with and without `TEST_DATABASE_URL` set)
Expected: all pass in both modes (DB-gated tests skip cleanly without it).

---

### Task 17: Real smoke test against the real GBIF Download API

This validates the actual fix: does a full run of `update_gbif_occurrences
--aoi idf` succeed end-to-end now, against real GBIF, with real credentials?
Nothing in Tasks 12-16 makes a real network call — this is the only step
that proves the Download API pivot actually resolves the original blocker.

**Files:** none (manual verification — requires a real GBIF account).

- [x] **Step 1: Confirm credentials are set**

Ensure `GBIF_USERNAME` and `GBIF_PASSWORD` are set in `.env` in this
worktree (the user has a GBIF.org account for this — ask if the values
aren't already there; do not proceed by inventing placeholder credentials,
the request will simply fail auth).

- [x] **Step 2: Run the job for real against `idf`**

Ensure the local Postgres container is up and `zones_hex` has `idf` at its
`default_res` (already true from earlier setup in this session).

Run: `uv run biowatch-jobs --debug update_gbif_occurrences --aoi idf`

This will submit a real GBIF download for ~4.86M candidate occurrences
(before quality filtering and zone clipping) — expect this to take a while
(GBIF's own processing time for the download, which is outside this job's
control, plus the transfer/parse of a large file). Do not treat a
long-running command as stuck; check on it rather than assuming failure. If
`GBIF_POLL_TIMEOUT_SECONDS` (1 hour) is exceeded, that itself is useful
information to report, not necessarily a bug — GBIF processing time for a
region this size at this word window is unknown until measured for real.

- [x] **Step 3: Check the outcome**

- **Success:** `job.success` in the logs (or a completed `gbif_occurrences.upserted`
  log line with a plausible `n_rows`). Verify:
  `SELECT count(*) FROM staging.species_occurrences_raw WHERE aoi_id='idf';`
  via `docker exec postgis psql -U biowatch -d biowatch_dev -c "..."` and
  sanity-check a few rows (`scientific_name`, `event_date`) look real.
- **Predicate rejected (400 naming a bad key):** the spec flagged this as a
  real risk — the exact key spelling for the date-range predicate
  (`EVENT_DATE`) was inferred from GBIF's naming convention, not confirmed
  in the (JS-rendered, non-scrapable) Swagger UI. If this happens, read the
  exact error, fix the key name in `build_predicate`, add a regression test
  locking in the corrected value, and re-run this step — do not guess a
  second time without re-verifying.
- **Auth failure (401/403):** check the credentials are for the right
  account and correctly loaded (`GBIF_USERNAME`, not the email address, per
  GBIF's own documentation).
- **Any other failure:** report the exact error — do not attempt a
  speculative fix without understanding it first, same discipline as
  Tasks 9-11.

- [x] **Step 4: Report the result**

This step has no automated pass/fail — it's the real-world check nothing
else in this plan can substitute for. Report row count, timing, and any
error before considering this ticket's Download API pivot complete.

**Result (real run against the live GBIF API, 2026-09-22):** SUCCESS.
4,084,550 rows stored for `aoi_id='idf'`. `event_date` spans 2017-01-01
to 2026-09-07 (matches the expected 10-year rolling window). 16,832
distinct H3 zones, 20,095 distinct species. Wall-clock ~23-24 minutes
per run (GBIF's own processing time dominates). Two real bugs found and
fixed during this run: GBIF serves SIMPLE_CSV downloads as a ZIP archive
(not gzip) regardless of requested format; a test
(`test_load_gbif_credentials_raises_on_missing_username`) was silently
relying on the ambient `.env` not containing real credentials, fixed by
isolating it from `load_dotenv`. Both fixes have regression tests and
were re-verified end-to-end against the real API.
