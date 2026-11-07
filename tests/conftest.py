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
- `postgis_url` : base PostGIS de test (`TEST_DATABASE_URL`) ; skip en local,
  échec en CI si la variable manque.
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


@pytest.fixture
def postgis_url() -> str:
    """URL de la base PostGIS de test. Skip en local sans `TEST_DATABASE_URL`, ÉCHEC en CI.

    En CI (`CI=true`, posé par GitHub Actions), `.github/workflows/test.yml`
    fournit toujours la variable : son absence est une casse du workflow, pas une
    raison de skipper en silence les tests bout-en-bout.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail(
            "TEST_DATABASE_URL manquant en CI : les tests PostGIS doivent tourner "
            "(voir .github/workflows/test.yml)."
        )
    pytest.skip("requires a live PostGIS database (set TEST_DATABASE_URL)")
