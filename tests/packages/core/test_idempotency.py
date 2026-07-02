"""Tests for core.idempotency — deterministic idempotency keys."""

from core import compute_idempotency_key

BASE = dict(job_name="satellite_ingest", scope="idf", period="2024-Q1", source_version="s2_v3")


def test_key_is_deterministic():
    assert compute_idempotency_key(**BASE) == compute_idempotency_key(**BASE)


def test_key_is_sha256_hex():
    key = compute_idempotency_key(**BASE)
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_different_scope_changes_key():
    assert compute_idempotency_key(**BASE) != compute_idempotency_key(**{**BASE, "scope": "pac"})


def test_different_period_changes_key():
    other = {**BASE, "period": "2024-Q2"}
    assert compute_idempotency_key(**BASE) != compute_idempotency_key(**other)


def test_different_source_version_changes_key():
    other = {**BASE, "source_version": "s2_v4"}
    assert compute_idempotency_key(**BASE) != compute_idempotency_key(**other)


def test_env_discriminates_key():
    dev = compute_idempotency_key(**BASE, env="dev")
    prod = compute_idempotency_key(**BASE, env="prod")
    none = compute_idempotency_key(**BASE)
    assert dev != prod
    assert dev != none
