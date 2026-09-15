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
