import pytest

from clients.sentinel_hub import auth

SENTINEL_HUB_VARS = ("SENTINEL_HUB_CLIENT_ID", "SENTINEL_HUB_CLIENT_SECRET")


@pytest.fixture
def clean_env(monkeypatch):
    """Isolate the process env and the module-level token cache between tests."""
    monkeypatch.setattr(auth, "load_dotenv", lambda *a, **k: False)
    for var in SENTINEL_HUB_VARS:
        monkeypatch.delenv(var, raising=False)
    auth._cached_token = None
    yield monkeypatch
    auth._cached_token = None


@pytest.fixture
def credentials(clean_env):
    clean_env.setenv("SENTINEL_HUB_CLIENT_ID", "test-client-id")
    clean_env.setenv("SENTINEL_HUB_CLIENT_SECRET", "test-client-secret")
    return clean_env
