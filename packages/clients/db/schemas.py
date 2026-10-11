"""
PostgreSQL schema helpers (namespaces, not ORM tables).

Used by jobs that stage data in a temporary schema (e.g. the OSM import writes
into `osm_tmp_{aoi}_{bucket}`, dropped later by the compute job). The ORM models
stay in `public`; these helpers never touch them.

Schema names cannot be passed as bind parameters, so every name is validated
against a strict pattern before being interpolated into DDL.
"""

import logging
import re

from sqlalchemy import Engine, text

from .session import session_scope

logger = logging.getLogger(__name__)

# Lowercase identifiers only: no quoting needed, no injection possible.
_SCHEMA_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class InvalidSchemaName(ValueError):
    """Raised when a schema name is not a safe lowercase PostgreSQL identifier."""


def validate_schema_name(name: str) -> str:
    """Return `name` unchanged if it is a safe identifier, raise otherwise."""
    if not _SCHEMA_NAME_RE.match(name) or name == "public" or name.startswith("pg_"):
        raise InvalidSchemaName(f"Invalid schema name: {name!r}.")
    return name


def schema_exists(name: str, engine: Engine | None = None) -> bool:
    """True if the schema exists in the database."""
    validate_schema_name(name)
    with session_scope(engine) as session:
        return bool(
            session.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"),
                {"name": name},
            )
        )


def create_schema(name: str, engine: Engine | None = None) -> None:
    """Create the schema. Fails if it already exists (no silent reuse)."""
    validate_schema_name(name)
    with session_scope(engine) as session:
        session.execute(text(f"CREATE SCHEMA {name}"))
    logger.info(
        "schema created",
        extra={"event": "db.schema.created", "context": {"schema": name}},
    )


def drop_schema(name: str, engine: Engine | None = None) -> None:
    """Drop the schema and everything in it. No-op if it does not exist."""
    validate_schema_name(name)
    with session_scope(engine) as session:
        session.execute(text(f"DROP SCHEMA IF EXISTS {name} CASCADE"))
    logger.info(
        "schema dropped",
        extra={"event": "db.schema.dropped", "context": {"schema": name}},
    )
