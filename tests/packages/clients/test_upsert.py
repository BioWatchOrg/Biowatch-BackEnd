"""Tests for clients.db.upsert — batching to stay under the bind-parameter limit."""

import importlib
from unittest.mock import MagicMock

from clients.db import JobRun, upsert

# `clients.db.upsert` resolves to the re-exported function, not the module;
# fetch the actual module to monkeypatch its module-level constant.
upsert_mod = importlib.import_module("clients.db.upsert")


def test_empty_rows_is_noop():
    session = MagicMock()
    upsert(session, JobRun, [])
    session.execute.assert_not_called()


def test_single_batch_when_small():
    session = MagicMock()
    rows = [{"run_id": i, "job_name": "j", "scope": "s"} for i in range(3)]
    upsert(session, JobRun, rows)
    assert session.execute.call_count == 1


def test_splits_into_batches_over_param_limit(monkeypatch):
    # 3 params/row, limit 10 → batch_size = 3 → 5 rows = 2 batches.
    monkeypatch.setattr(upsert_mod, "_MAX_BIND_PARAMS", 10)
    session = MagicMock()
    rows = [{"run_id": i, "job_name": "j", "scope": "s"} for i in range(5)]

    upsert(session, JobRun, rows)

    assert session.execute.call_count == 2  # ceil(5 / 3)


def test_onupdate_columns_are_refreshed_on_conflict():
    """`onupdate=func.now()` n'est pas appliqué par SQLAlchemy à ON CONFLICT DO UPDATE."""
    from clients.db import IucnTaxa
    from sqlalchemy.dialects import postgresql

    session = MagicMock()
    row = {
        "iucn_taxon_id": 1,
        "source_version": "2026-1",
        "scientific_name": "Lamna nasus",
        "category": "VU",
        "assessment_id": 10,
    }
    upsert(session, IucnTaxa, [row])

    stmt = session.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "updated_at = now()" in sql


def test_onupdate_column_is_not_refreshed_when_only_pk_columns():
    from clients.db import IucnTaxa
    from sqlalchemy.dialects import postgresql

    session = MagicMock()
    upsert(session, IucnTaxa, [{"iucn_taxon_id": 1, "source_version": "2026-1"}])

    sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "DO NOTHING" in sql
