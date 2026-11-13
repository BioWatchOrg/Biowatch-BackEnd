"""
Thin wrapper around the `osm2pgsql` binary (>= 1.9 for `--schema`).

Runs a one-shot import of one or more `.pbf` files into a dedicated schema with
the BioWatch flex style (`style.lua`). Slim mode + `--drop` keeps memory low on
the VPS (no in-RAM node cache required) and leaves only the output tables.

Connection parameters go through libpq environment variables (`PG*`), never on
the command line, so the password does not show up in `ps`.
"""

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from sqlalchemy import Engine

from ..db.schemas import validate_schema_name
from ..db.session import get_engine

logger = logging.getLogger(__name__)

STYLE_PATH = Path(__file__).with_name("style.lua")

# Version of `style.lua` (what is extracted, and how) — NOT a date. Bump it by
# hand whenever the style changes, so already-imported buckets get a new
# idempotency key and are re-imported instead of keeping the old definitions.
#   1 : buildings, major roads, urban landuse (ticket #35).
STYLE_VERSION = "1"

# Override to point at another binary (e.g. a version-pinned install).
OSM2PGSQL_BIN_ENV = "OSM2PGSQL_BIN"

# Keep the tail of osm2pgsql's output in error messages, not megabytes of it.
_STDERR_TAIL = 2000

# A regional import takes a few minutes to ~1 h: beyond this, it is stuck. Kept
# below `TimeoutStartSec=6h` (infra/systemd/biowatch-job@.service) so the job
# fails with a clear error (and a `failed` run) instead of being killed by
# systemd mid-run.
DEFAULT_TIMEOUT_S = 3 * 3600


class Osm2pgsqlError(Exception):
    """Raised when osm2pgsql is missing or exits with an error."""


def _libpq_env(engine: Engine) -> dict[str, str]:
    url = engine.url
    env = {
        "PGHOST": url.host or "127.0.0.1",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "",
        "PGDATABASE": url.database or "",
    }
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    return env


def build_command(binary: str, pbf_paths: list[Path], schema: str) -> list[str]:
    """Command line for an import of `pbf_paths` into `schema` (pure, testable)."""
    validate_schema_name(schema)
    return [
        binary,
        "--create",
        "--slim",
        "--drop",
        "--output=flex",
        f"--style={STYLE_PATH}",
        f"--schema={schema}",
        f"--middle-schema={schema}",
        *(str(p) for p in pbf_paths),
    ]


def _tail(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    return (output or "")[-_STDERR_TAIL:]


def run_osm2pgsql(
    pbf_paths: list[Path],
    schema: str,
    engine: Engine | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> None:
    """
    Import `pbf_paths` into `schema` (which must already exist).

    Raises `Osm2pgsqlError` if the binary is missing, the import fails, or it
    runs longer than `timeout_s` (the process is then killed).
    """
    binary = os.environ.get(OSM2PGSQL_BIN_ENV, "osm2pgsql")
    if shutil.which(binary) is None:
        raise Osm2pgsqlError(
            f"osm2pgsql binary not found ('{binary}'). Install it (apt install osm2pgsql) "
            f"or set {OSM2PGSQL_BIN_ENV}."
        )
    command = build_command(binary, pbf_paths, schema)
    env = {**os.environ, **_libpq_env(engine or get_engine())}
    log_ctx = {"schema": schema, "files": [p.name for p in pbf_paths]}
    logger.info("osm2pgsql import started", extra={"event": "osm2pgsql.start", "context": log_ctx})
    started = time.monotonic()
    try:
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, check=False, timeout=timeout_s
        )
    except subprocess.TimeoutExpired as e:
        logger.error(
            "osm2pgsql import timed out",
            extra={
                "event": "osm2pgsql.timeout",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "context": {**log_ctx, "timeout_s": timeout_s},
            },
        )
        raise Osm2pgsqlError(
            f"osm2pgsql did not finish within {timeout_s:.0f} s and was killed: {_tail(e.stderr)}"
        ) from e
    duration_ms = int((time.monotonic() - started) * 1000)
    if result.returncode != 0:
        logger.error(
            "osm2pgsql import failed",
            extra={
                "event": "osm2pgsql.failed",
                "duration_ms": duration_ms,
                "context": {**log_ctx, "returncode": result.returncode},
            },
        )
        raise Osm2pgsqlError(
            f"osm2pgsql exited with code {result.returncode}: {_tail(result.stderr)}"
        )
    logger.info(
        "osm2pgsql import done",
        extra={"event": "osm2pgsql.done", "duration_ms": duration_ms, "context": log_ctx},
    )
