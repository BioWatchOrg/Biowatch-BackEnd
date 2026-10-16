"""Résolution de l'environnement d'exécution BioWatch (dev / prod).

Un seul endroit décide de l'environnement du process :

    flag CLI `--env`  >  variable `BIOWATCH_ENV`  >  défaut `dev`

Le défaut est volontairement `dev` : un lancement qui ne précise rien ne doit
jamais pouvoir écrire en production. La protection réelle reste structurelle —
`.env.prod` n'existe que sur le VPS, et `load_env_file("prod")` **exige** ce
fichier : un `--env prod` lancé en local échoue sur son absence, avant toute
lecture de variable de connexion. Un `DATABASE_URL` ou des `POSTGRES_*` qui
traînent dans le shell ne suffisent donc pas à atteindre la vraie base.
"""

import os
from pathlib import Path
from typing import Literal, cast, get_args

from dotenv import find_dotenv, load_dotenv

Env = Literal["dev", "prod"]

ENVS: tuple[Env, ...] = get_args(Env)
DEFAULT_ENV: Env = "dev"
ENV_VAR = "BIOWATCH_ENV"
DATA_ROOT = Path("data")
DATA_ROOT_VAR = "BIOWATCH_DATA_ROOT"


# Environnements dont le fichier `.env.<env>` est obligatoire. En prod, le
# fichier est la preuve qu'on tourne sur la machine autorisée (le VPS) : sans
# lui, on refuse de démarrer, quelles que soient les variables déjà présentes.
ENV_FILE_REQUIRED: frozenset[Env] = frozenset({"prod"})


class UnknownEnvError(ValueError):
    """Nom d'environnement inconnu (attendu : `dev` ou `prod`)."""


class EnvFileNotFoundError(RuntimeError):
    """`.env.<env>` introuvable alors que l'environnement l'exige (prod)."""


def resolve_env(cli_env: str | None = None) -> Env:
    """Environnement effectif : flag CLI, sinon `BIOWATCH_ENV`, sinon `dev`.

    La valeur est normalisée (espaces, casse) pour qu'un `--env PROD` ou un
    `BIOWATCH_ENV=" dev "` mal copié ne parte pas silencieusement sur le défaut.
    """
    raw = cli_env if cli_env is not None else os.environ.get(ENV_VAR)
    value = (raw or DEFAULT_ENV).strip().lower()
    if value not in ENVS:
        raise UnknownEnvError(
            f"Environnement inconnu : {value!r}. Valeurs acceptées : {', '.join(ENVS)}."
        )
    return cast(Env, value)


def env_file(env: Env) -> str:
    """Nom du fichier de config applicative de l'environnement (`.env.dev`…)."""
    return f".env.{env}"


def load_env_file(env: Env) -> str | None:
    """Charge `.env.<env>` dans l'environnement du process ; retourne le chemin.

    À appeler au démarrage de l'entrypoint, AVANT toute lecture de variable —
    `setup_logging()` lit `LOG_LEVEL` dès son appel, donc un chargement
    paresseux (au premier accès DB) arriverait trop tard.

    Garde-fou prod : si `env` est dans `ENV_FILE_REQUIRED` et que le fichier
    est introuvable, lève `EnvFileNotFoundError` — c'est ce qui bloque un
    `--env prod` hors du VPS, même si un `DATABASE_URL` est déjà exporté.
    En dev, l'absence du fichier retourne `None` (variables injectées par le
    shell ou la CI).

    Idempotent : `override=False` laisse gagner les variables déjà présentes
    dans l'environnement, un second appel ne change donc rien.
    """
    name = env_file(env)
    path = find_dotenv(name, usecwd=True)
    if not path:
        if env in ENV_FILE_REQUIRED:
            raise EnvFileNotFoundError(
                f"Fichier {name} introuvable : l'environnement '{env}' exige ce fichier "
                f"et refuse de démarrer sans lui (il n'existe que sur le VPS)."
            )
        return None
    load_dotenv(path, override=False)
    return path


def data_root(env: Env) -> Path:
    """Racine des artefacts de données de l'environnement (`data/dev`…).

    `data/` est gitignoré : les artefacts produits par les jobs (grilles,
    exports) ne rentrent jamais dans le dépôt, et les deux environnements
    n'écrivent jamais dans le même répertoire.

    `BIOWATCH_DATA_ROOT` remplace la racine `data/` : sur le VPS, le code
    déployé (`/opt/biowatch`) est en lecture seule pour `biowatch-jobs`, les
    artefacts doivent aller dans `/var/lib/biowatch/jobs`.
    """
    return Path(os.environ.get(DATA_ROOT_VAR) or DATA_ROOT) / env
