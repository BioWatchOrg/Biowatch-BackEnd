"""Fixtures globales.

- `_restore_environ` (autouse) : `os.environ` est restauré après chaque test.
  Le code testé écrit directement dedans (`cli.main()` pose `BIOWATCH_ENV`,
  `load_dotenv` pose les variables du fichier) et `monkeypatch.delenv(...,
  raising=False)` sur une variable absente n'enregistre aucune restauration :
  sans ce filet, un `BIOWATCH_ENV=prod` posé par un test fuit vers les suivants
  et les rend dépendants de l'ordre d'exécution.
- `project_root` : racine de projet isolée. `.env.<env>` et `data/` sont ancrés
  sur `core.config.PROJECT_ROOT` (pas sur le cwd) ; un test qui veut poser un
  `.env.prod` ou lire un artefact passe par cette fixture.
"""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _restore_environ():
    saved = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def project_root(tmp_path, monkeypatch) -> Path:
    import core.config

    monkeypatch.setattr(core.config, "PROJECT_ROOT", tmp_path)
    return tmp_path
