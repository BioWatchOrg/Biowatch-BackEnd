"""
Run once against a fresh database to create the required PostgreSQL extensions
and every table declared by the ORM models:

    uv run python -m clients.db.init_db

The ORM models (`clients.db.models`) are the single source of truth for the
schema. Extensions cannot be expressed as ORM models, so they are created here
in raw SQL before `create_all`, since PostGIS geometry columns depend on them.
"""

import logging

from sqlalchemy import Engine, text

from .base import Base
from .session import get_engine

logger = logging.getLogger(__name__)

EXTENSIONS = ("postgis", "pgcrypto")


def _create_extensions(engine: Engine) -> None:
    """Create the PostgreSQL extensions required before any table is built."""
    with engine.begin() as conn:
        for extension in EXTENSIONS:
            conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))


def init_db(engine: Engine | None = None) -> None:
    """Create extensions and every table. Idempotent: safe to re-run."""
    try:
        engine = engine or get_engine()
        _create_extensions(engine)
        Base.metadata.create_all(engine)
        logger.info(
            "CLIENTS-DB-init_db : schema initialized (%d tables)", len(Base.metadata.tables)
        )
    except Exception as e:
        logger.error("CLIENTS-DB-init_db : error initializing schema: %s", e)
        raise
