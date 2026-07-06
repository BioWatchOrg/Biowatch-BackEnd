"""
Centralised structured (JSON) logging for the BioWatch Python backend.

One entrypoint configures the root logger once via `setup_logging(...)`; every
module keeps using the stdlib logger it already has
(`logging.getLogger(__name__)`). No `print()`, no per-module handler, no
per-module `setLevel` — the level is driven centrally (env `LOG_LEVEL`, or the
explicit `level` argument passed at startup).

Log shape (one JSON object per line, stdout → Logdy):

    {"timestamp": "...", "level": "INFO", "service": "biowatch-jobs",
     "logger": "geo.h3_grid", "event": "job.start", "message": "run started",
     "context": {"run_id": "...", "idempotency_key": "...", "duration_ms": 12}}

Field rules (aligned with CLAUDE.md "Exigences de qualité" / the Notion task):
- `timestamp`, `level`, `service`, `event` are always present.
- `event` is the machine-readable `event=` passed via `extra` when provided,
  otherwise it falls back to the log message.
- Any extra field passed by the caller (`extra={"run_id": ...}` or
  `extra={"context": {...}}`) is collected under `context`. This makes the
  formatter compatible with the existing call sites in `clients.db.job_runs`
  (flat `extra`) and with the nested `context` convention.
- `error` carries the formatted traceback when `exc_info=True`.
"""

import datetime
import json
import logging
import os
import sys

# Attributes the stdlib puts on every LogRecord. Anything NOT in here that a
# caller attached via `extra=` is application context, so it goes into `context`.
_RESERVED_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}

DEFAULT_LEVEL = "INFO"


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.datetime.fromtimestamp(
                record.created, datetime.UTC
            ).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "event": getattr(record, "event", None) or record.getMessage(),
            "message": record.getMessage(),
        }

        # Merge an explicit `context` dict with any other custom extras.
        context: dict[str, object] = {}
        explicit = getattr(record, "context", None)
        if isinstance(explicit, dict):
            context.update({str(k): v for k, v in explicit.items()})  # type: ignore[misc]
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key in ("event", "context"):
                continue
            context[key] = value
        if context:
            payload["context"] = context

        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(service: str = "biowatch", level: str | int | None = None) -> None:
    """
    Configure the root logger once, at process startup.

    Call this exactly once from an entrypoint (`apps/jobs/cli.py:main`, and the
    API startup when it exists) — never at import time / from an `__init__.py`,
    which would reconfigure logging at an uncontrolled moment.

    - `service`: value emitted in the `service` field ("biowatch-jobs",
      "biowatch-api", ...).
    - `level`: explicit level (e.g. "DEBUG"); defaults to env `LOG_LEVEL`, then
      to INFO (prod). Dev turns on DEBUG via the CLI `--debug` flag or
      `LOG_LEVEL=DEBUG`.

    Idempotent: re-calling replaces existing handlers instead of stacking them.
    """
    resolved = level or os.environ.get("LOG_LEVEL", DEFAULT_LEVEL)
    if isinstance(resolved, str):
        resolved = resolved.upper()

    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root.addHandler(handler)
    root.setLevel(resolved)
