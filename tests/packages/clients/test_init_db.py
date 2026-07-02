"""Tests for clients.db.init_db — extension creation and schema bootstrap."""

from unittest.mock import MagicMock

import clients.db.init_db as init_db_mod
from clients.db.init_db import EXTENSIONS, _create_extensions, init_db


def test_extensions_are_postgis_and_pgcrypto():
    assert EXTENSIONS == ("postgis", "pgcrypto")


def test_create_extensions_issues_one_create_per_extension():
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value

    _create_extensions(engine)

    executed = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert len(executed) == len(EXTENSIONS)
    assert all("CREATE EXTENSION IF NOT EXISTS" in stmt for stmt in executed)
    for extension in EXTENSIONS:
        assert any(extension in stmt for stmt in executed)


def test_init_db_creates_extensions_before_tables(monkeypatch):
    engine = MagicMock()
    order: list[str] = []
    monkeypatch.setattr(init_db_mod, "_create_extensions", lambda e: order.append(f"ext:{e}"))
    monkeypatch.setattr(
        init_db_mod.Base.metadata, "create_all", lambda e: order.append(f"tables:{e}")
    )

    init_db(engine=engine)

    # Extensions must exist before create_all: PostGIS geometry columns depend on them.
    assert order == [f"ext:{engine}", f"tables:{engine}"]


def test_init_db_falls_back_to_shared_engine(monkeypatch):
    sentinel = MagicMock(name="shared_engine")
    used: list[object] = []
    monkeypatch.setattr(init_db_mod, "get_engine", lambda: sentinel)
    monkeypatch.setattr(init_db_mod, "_create_extensions", lambda e: used.append(e))
    monkeypatch.setattr(init_db_mod.Base.metadata, "create_all", lambda e: used.append(e))

    init_db()

    assert used == [sentinel, sentinel]
