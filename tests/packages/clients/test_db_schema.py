"""Schema-level tests for the ORM models (no live database required)."""

from sqlalchemy import create_mock_engine
from sqlalchemy.schema import CreateTable

from clients.db import Base
from clients.db.init_db import EXTENSIONS

EXPECTED_TABLES = {
    "zones_hex",
    "satellite_features_by_zone",
    "osm_features_by_zone",
    "protected_areas_by_zone",
    "species_features_by_zone",
    "stress_score_by_zone",
    "job_runs",
    "job_run_zone_errors",
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
}


def test_all_business_tables_are_declared():
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_primary_keys_match_expected():
    for table_name, expected_pk in EXPECTED_PRIMARY_KEYS.items():
        table = Base.metadata.tables[table_name]
        actual_pk = {col.name for col in table.primary_key.columns}
        assert actual_pk == expected_pk, table_name


def test_every_feature_table_references_zones_hex():
    referencing = EXPECTED_TABLES - {"zones_hex", "job_runs"}
    for table_name in referencing:
        table = Base.metadata.tables[table_name]
        targets = {fk.column.table.name for fk in table.foreign_keys}
        assert "zones_hex" in targets, table_name


def test_schema_compiles_to_postgresql_ddl():
    engine = create_mock_engine("postgresql+psycopg://", executor=lambda *a, **k: None)
    # Compiling every table against the PostgreSQL dialect exercises the
    # geometry, UUID and timestamp column types without needing a live DB.
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=engine.dialect))
        assert f"CREATE TABLE {table.name}" in ddl


def test_extensions_declared_for_init():
    assert "postgis" in EXTENSIONS
    assert "pgcrypto" in EXTENSIONS
