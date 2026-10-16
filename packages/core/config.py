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

from dotenv import load_dotenv

Env = Literal["dev", "prod"]

ENVS: tuple[Env, ...] = get_args(Env)
DEFAULT_ENV: Env = "dev"
ENV_VAR = "BIOWATCH_ENV"
DATA_ROOT = Path("data")
DATA_ROOT_VAR = "BIOWATCH_DATA_ROOT"

# Racine du dépôt (packages/core/config.py -> ../../). Tout ce qui est relatif
# au projet (`.env.<env>`, `data/`) est ancré ici et JAMAIS sur le cwd : un job
# lancé depuis `~` ou par un timer systemd sans WorkingDirectory trouve quand
# même sa config, et un `.env.prod` qui traîne dans un dossier parent ne compte
# pas. Suppose une installation éditable (uv sync), ce qui est le cas partout.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# Environnements dont le fichier `.env.<env>` est obligatoire. En prod, le
# fichier est la preuve qu'on tourne sur la machine autorisée (le VPS) : sans
# lui, on refuse de démarrer, quelles que soient les variables déjà présentes.
ENV_FILE_REQUIRED: frozenset[Env] = frozenset({"prod"})


class UnknownEnvError(ValueError):
    """Nom d'environnement inconnu (attendu : `dev` ou `prod`)."""


class EnvFileNotFoundError(RuntimeError):
    """`.env.<env>` introuvable alors que l'environnement l'exige (prod), ou
    présent mais illisible (permissions) — dans tous les environnements."""


def normalize_env(value: str) -> str:
    """Normalisation unique d'un nom d'env (casse, espaces) : `" PROD "` → `"prod"`.

    Utilisée par `resolve_env` ET par le parser du CLI (`type=`), pour que
    `--env PROD` et `BIOWATCH_ENV=PROD` se comportent exactement pareil.
    """
    return value.strip().lower()


def resolve_env(cli_env: str | None = None) -> Env:
    """Environnement effectif : flag CLI, sinon `BIOWATCH_ENV`, sinon `dev`.

    La valeur est normalisée (espaces, casse) pour qu'un `--env PROD` ou un
    `BIOWATCH_ENV=" dev "` mal copié ne parte pas silencieusement sur le défaut.
    """
    raw = cli_env if cli_env is not None else os.environ.get(ENV_VAR)
    # Normaliser AVANT de retomber sur le défaut : "" et "  " sont équivalents.
    value = normalize_env(raw or "") or DEFAULT_ENV
    if value not in ENVS:
        raise UnknownEnvError(
            f"Environnement inconnu : {value!r}. Valeurs acceptées : {', '.join(ENVS)}."
        )
    return cast(Env, value)


def env_file(env: Env) -> str:
    """Nom du fichier de config applicative de l'environnement (`.env.dev`…)."""
    return f".env.{env}"


def load_env_file(env: Env) -> str | None:
    """Charge `<racine du dépôt>/.env.<env>` dans le process ; retourne le chemin.

    Le fichier est cherché à `PROJECT_ROOT` uniquement — pas dans le cwd, pas
    dans les dossiers parents.

    À appeler au démarrage de l'entrypoint, AVANT toute lecture de variable —
    `setup_logging()` lit `LOG_LEVEL` dès son appel, donc un chargement
    paresseux (au premier accès DB) arriverait trop tard.

    Garde-fou prod : si `env` est dans `ENV_FILE_REQUIRED` et que le fichier
    est introuvable, lève `EnvFileNotFoundError` — c'est ce qui bloque un
    `--env prod` hors du VPS, même si un `DATABASE_URL` est déjà exporté.
    Sur le VPS, `.env.prod` est un marqueur posé par le déploiement à côté du
    code : il peut être vide, les secrets restent injectés par systemd
    (`EnvironmentFile=`). En dev, l'absence du fichier retourne `None`
    (variables injectées par le shell ou la CI).

    Fige aussi `BIOWATCH_ENV` à `env` dans le process, AVANT de lire le
    fichier : l'env qui a chargé la config est l'env du process, un
    `BIOWATCH_ENV=prod` collé par erreur dans `.env.dev` ne peut pas faire
    diverger les `resolve_env()` suivants (clé d'idempotence, `data_root`)
    de l'env qui a validé la base.

    Idempotent : `override=False` laisse gagner les variables déjà présentes
    dans l'environnement, un second appel ne change donc rien.
    """
    os.environ[ENV_VAR] = env
    path = PROJECT_ROOT / env_file(env)
    if not path.is_file():
        if env in ENV_FILE_REQUIRED:
            raise EnvFileNotFoundError(
                f"Fichier {path} introuvable : l'environnement '{env}' exige ce fichier "
                f"et refuse de démarrer sans lui (il n'existe que sur le VPS)."
            )
        return None
    try:
        load_dotenv(path, override=False)
    except OSError as e:
        # Présent mais illisible (ex. .env.prod en 600 root sur le VPS) : une
        # erreur de config lisible, pas un PermissionError brut.
        raise EnvFileNotFoundError(
            f"Fichier {path} illisible ({e.strerror}) : vérifier ses permissions "
            f"(attendu : lisible par le compte qui lance les jobs)."
        ) from None
    return str(path)


def expected_db_suffix(env: Env) -> str:
    """Suffixe obligatoire du nom de la base de l'environnement (`_dev`, `_prod`).

    Convention structurelle, pas cosmétique : `clients.db` refuse de se
    connecter à une base dont le nom ne porte pas le suffixe de l'env courant.
    C'est ce qui bloque un `DATABASE_URL` ou un `POSTGRES_DB` pointant vers
    `biowatch_prod` (tunnel SSH, variable oubliée dans le shell) alors qu'on
    tourne en `dev` — le défaut, donc le cas le plus exposé.
    """
    return f"_{env}"


def data_root(env: Env) -> Path:
    """Racine des artefacts de données de l'environnement (`data/dev`…).

    `data/` est gitignoré : les artefacts produits par les jobs (grilles,
    exports) ne rentrent jamais dans le dépôt, et les deux environnements
    n'écrivent jamais dans le même répertoire.

    Le défaut `data/` est ancré sur `PROJECT_ROOT`, pas sur le cwd — et un
    `BIOWATCH_DATA_ROOT` relatif l'est aussi. `BIOWATCH_DATA_ROOT` remplace
    cette racine : sur le VPS, le code déployé (`/opt/biowatch`) est en lecture
    seule pour `biowatch-jobs`, les artefacts doivent aller dans
    `/var/lib/biowatch/jobs`.
    """
    root = Path(os.environ.get(DATA_ROOT_VAR) or DATA_ROOT)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root / env
