import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from core import Env, env_file, expected_db_suffix, load_env_file, resolve_env
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class DatabaseConfigError(RuntimeError):
    """Configuration de connexion invalide ou incomplète (erreur d'exploitation,
    pas un bug) : le CLI l'affiche sans traceback. Ne contient jamais d'URL
    ni de mot de passe."""


class DatabaseEnvMismatchError(DatabaseConfigError):
    """Le nom de la base ne porte pas le suffixe de l'environnement courant."""


def _check_db_matches_env(db_name: str, env: Env) -> None:
    """Refuse une base dont le nom ne se termine pas par `_<env>`.

    Dernier filet, indépendant de l'origine des variables : un `DATABASE_URL`
    exporté dans le shell (tunnel vers la prod) gagne sur `.env.dev`, et
    `.env.prod` n'est exigé qu'en prod. Sans ce contrôle, un job lancé sans
    `--env` pourrait écrire en production. Le message ne contient jamais
    l'URL ni le mot de passe, seulement le nom de la base.
    """
    suffix = expected_db_suffix(env)
    if not db_name.endswith(suffix):
        raise DatabaseEnvMismatchError(
            f"Base '{db_name}' incohérente avec l'environnement '{env}' : son nom doit "
            f"se terminer par '{suffix}'. Un DATABASE_URL ou un POSTGRES_DB du shell "
            f"(tunnel SSH ?) pointe vers une autre base que celle de .env.{env}."
        )


def _database_name(url: URL) -> str:
    """Nom de base effectivement ouvert par le driver.

    Avec psycopg/libpq, un `?dbname=` dans la query string prime sur le chemin
    de l'URL : `.../biowatch_dev?dbname=biowatch_prod` ouvre `biowatch_prod`.
    Le contrôle du suffixe doit donc porter sur ce nom-là, pas sur `url.database`.
    """
    override = url.query.get("dbname")
    if isinstance(override, tuple):  # paramètre répété : libpq garde le dernier
        override = override[-1]
    return override or url.database or ""


def _build_database_url() -> str:
    """URL SQLAlchemy de l'environnement courant.

    L'environnement vient de `BIOWATCH_ENV`, que l'entrypoint (CLI) pose
    avant tout accès DB — `get_engine` étant mis en cache, il n'est jamais
    reconstruit ensuite.
    """
    env = resolve_env()
    # Filet pour les entrypoints qui ne passent pas par le CLI (API à venir,
    # usage en librairie). Idempotent si le CLI l'a déjà fait. En prod, lève
    # `EnvFileNotFoundError` si `.env.prod` manque — AVANT de lire DATABASE_URL
    # ou POSTGRES_* : une variable résiduelle dans le shell ne doit jamais
    # suffire à atteindre la base de production depuis un poste local.
    load_env_file(env)
    logger.debug(
        "environment resolved",
        extra={
            "event": "db.env_resolved",
            "context": {"env": env, "env_file": env_file(env)},
        },
    )

    explicit_url = os.environ.get("DATABASE_URL")
    if explicit_url:
        try:
            db_from_url = _database_name(make_url(explicit_url))
        except ArgumentError:
            # `from None` : le message SQLAlchemy cite l'URL complète, donc le
            # mot de passe — il ne doit apparaître ni ici ni dans la chaîne.
            raise DatabaseConfigError(
                f"DATABASE_URL illisible — environnement '{env}', fichier {env_file(env)}. "
                "Format attendu : postgresql+psycopg://user:password@host:port/base."
            ) from None
        _check_db_matches_env(db_from_url, env)
        return explicit_url

    try:
        user = os.environ["POSTGRES_USER"]
        password = os.environ["POSTGRES_PASSWORD"]
        db = os.environ["POSTGRES_DB"]
    except KeyError as e:
        raise DatabaseConfigError(
            f"Variable d'environnement manquante : {e.args[0]} — environnement "
            f"'{env}', fichier attendu : {env_file(env)} (voir .env.dev.example)."
        ) from e
    _check_db_matches_env(db, env)
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
    except Exception as e:
        session.rollback()
        logger.error(
            "session rolled back on error",
            extra={"event": "db.session_error", "context": {"error": str(e)}},
        )
        raise
    finally:
        session.close()
