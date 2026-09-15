"""Tests for clients.db.session.session_scope — commit / rollback / close semantics."""

from unittest.mock import MagicMock

import pytest

import clients.db.session as session_mod
from clients.db import session_scope


@pytest.fixture
def fake_session(monkeypatch):
    """Replace the session factory with one that yields a MagicMock session."""
    session = MagicMock(name="session")
    monkeypatch.setattr(session_mod, "get_sessionmaker", lambda engine=None: lambda: session)
    return session


def test_commits_and_closes_on_success(fake_session):
    with session_scope() as s:
        assert s is fake_session
    fake_session.commit.assert_called_once()
    fake_session.rollback.assert_not_called()
    fake_session.close.assert_called_once()


def test_rolls_back_and_closes_on_exception(fake_session):
    with pytest.raises(ValueError):
        with session_scope():
            raise ValueError("boom")
    fake_session.commit.assert_not_called()
    fake_session.rollback.assert_called_once()
    fake_session.close.assert_called_once()
