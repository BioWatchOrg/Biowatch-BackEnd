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

# Override to point at another binary (e.g. a version-pinned install).
OSM2PGSQL_BIN_ENV = "OSM2PGSQL_BIN"

# Keep the tail of osm2pgsql's output in error messages, not megabytes of it.
_STDERR_TAIL = 2000


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


def run_osm2pgsql(pbf_paths: list[Path], schema: str, engine: Engine | None = None) -> None:
    """
    Import `pbf_paths` into `schema` (which must already exist).

    Raises `Osm2pgsqlError` if the binary is missing or the import fails.
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
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
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
            f"osm2pgsql exited with code {result.returncode}: {result.stderr[-_STDERR_TAIL:]}"
        )
    logger.info(
        "osm2pgsql import done",
        extra={"event": "osm2pgsql.done", "duration_ms": duration_ms, "context": log_ctx},
    )
