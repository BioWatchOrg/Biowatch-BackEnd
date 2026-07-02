import os
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def _build_database_url() -> str:
    """
    Build a SQLAlchemy connection URL from environment variables.

    Reads the same POSTGRES_* variables used by the docker-compose stack, so a
    single `.env` drives both the container and the ORM. `DATABASE_URL` takes
    precedence when set (useful for the dev SSH tunnel documented in the infra).
    """

    load_dotenv(override=False)

    explicit_url = os.environ.get("DATABASE_URL")
    if explicit_url:
        return explicit_url

    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    db = os.environ["POSTGRES_DB"]
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")

    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


@lru_cache(maxsize=1)
def get_engine(echo: bool = False) -> Engine:
    """
    Return the shared SQLAlchemy engine, created from the environment config.

    Memoized: the engine (and its connection pool) is built once and reused on
    every subsequent call, so scattered `get_engine()` calls don't spawn extra
    pools. Call `get_engine.cache_clear()` to force a rebuild (e.g. in tests).
    """
    return create_engine(_build_database_url(), echo=echo, future=True)


def get_sessionmaker(engine: Engine | None = None) -> sessionmaker[Session]:
    """Return a configured session factory bound to the given (or a new) engine."""
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)
