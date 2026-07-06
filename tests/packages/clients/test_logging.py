"""Tests for the centralised JSON logging (packages/clients/logs)."""

import json
import logging

import pytest

from clients import setup_logging
from clients.logs.logger_config import JsonFormatter


def _format(record: logging.LogRecord, service: str = "biowatch-test") -> dict:
    return json.loads(JsonFormatter(service).format(record))


def _record(msg: str = "hello", level: int = logging.INFO, **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_output_is_valid_json_with_standard_fields():
    payload = _format(_record("run started"))

    assert set(payload) >= {"timestamp", "level", "service", "event", "message"}
    assert payload["level"] == "INFO"
    assert payload["service"] == "biowatch-test"
    assert payload["message"] == "run started"
    # No explicit event → falls back to the message.
    assert payload["event"] == "run started"


def test_explicit_event_overrides_message():
    payload = _format(_record("run started", event="job.start"))

    assert payload["event"] == "job.start"
    assert payload["message"] == "run started"


def test_flat_extras_are_collected_into_context():
    # Matches the existing call style in clients.db.job_runs.
    payload = _format(_record("run started", run_id="abc", idempotency_key="k", duration_ms=12))

    assert payload["context"] == {"run_id": "abc", "idempotency_key": "k", "duration_ms": 12}


def test_explicit_context_dict_is_merged():
    payload = _format(_record("done", event="h3_grid.computed", context={"aoi": "idf", "n": 3}))

    assert payload["context"] == {"aoi": "idf", "n": 3}
    assert "context" not in payload["context"]


def test_exception_is_serialised_under_error():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="t", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = _format(record)

    assert "error" in payload
    assert "ValueError: boom" in payload["error"]


def test_no_context_key_when_no_extras():
    payload = _format(_record("plain"))

    assert "context" not in payload


def test_setup_logging_is_idempotent_and_emits_json(capsys):
    setup_logging(service="biowatch-jobs", level="DEBUG")
    setup_logging(service="biowatch-jobs", level="DEBUG")

    root = logging.getLogger()
    try:
        # A single call must not stack handlers.
        assert len(root.handlers) == 1
        assert root.level == logging.DEBUG

        logging.getLogger("some.module").info("business event", extra={"run_id": "r1"})
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)

        assert payload["service"] == "biowatch-jobs"
        assert payload["context"]["run_id"] == "r1"
    finally:
        root.handlers.clear()


def test_setup_logging_reads_level_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "warning")
    setup_logging(service="biowatch-jobs")

    root = logging.getLogger()
    try:
        assert root.level == logging.WARNING
    finally:
        root.handlers.clear()


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Keep the global root logger clean between tests."""
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers.clear()
    root.handlers.extend(saved)
    root.setLevel(saved_level)
