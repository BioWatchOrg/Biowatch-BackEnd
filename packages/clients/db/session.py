import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def _build_database_url() -> str:

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
    return create_engine(_build_database_url(), echo=echo, future=True)


def get_sessionmaker(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """
    Provide a transactional session as a context manager.

    Commits on clean exit, rolls back on any exception (reverting every write
    made in the block), and always closes the session. This is the standard way
    to talk to the DB in jobs:

        with session_scope() as session:
            session.add(obj)
            # commit happens automatically; an exception rolls everything back
    """
    session = get_sessionmaker(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
