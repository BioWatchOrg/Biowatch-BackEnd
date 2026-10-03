"""
OAuth2 client-credentials auth against the Copernicus Data Space Ecosystem
(CDSE) identity server, with in-memory token caching.

CDSE does not document a fixed token lifetime (see
https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Overview/Authentication.html):
each token response carries its own `expires_in`, which is server-configurable
and observed to vary. `get_access_token` never hardcodes a duration — it reads
`expires_in` from the response and proactively refreshes `_REFRESH_MARGIN_SECONDS`
ahead of that, instead of waiting for a 401.
"""

import logging
import os
import threading
import time

import httpx
from dotenv import load_dotenv

from .exceptions import SentinelHubAuthError
from .http_errors import safe_error_description
from .models import _CachedToken

logger = logging.getLogger(__name__)

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

# Refresh this many seconds *before* the token actually expires, instead of
# waiting for it to expire and getting a 401. 120s is a safety margin: it
# covers the time between "we checked the token is still valid" and "the HTTP
# request using it actually reaches the server".
_REFRESH_MARGIN_SECONDS = 120.0

_token_lock = threading.Lock()
_cached_token: _CachedToken | None = None


def _load_credentials() -> tuple[str, str]:
    load_dotenv(override=False)
    try:
        client_id = os.environ["SENTINEL_HUB_CLIENT_ID"]
        client_secret = os.environ["SENTINEL_HUB_CLIENT_SECRET"]
    except KeyError as e:
        raise RuntimeError(
            f"Variable d'environnement manquante : {e.args[0]} (voir .env.example)"
        ) from e
    return client_id, client_secret


def _fetch_token(client: httpx.Client) -> _CachedToken:
    client_id, client_secret = _load_credentials()

    try:
        # Body must be `x-www-form-urlencoded` (Keycloak doesn't parse JSON on
        # this endpoint): pass a dict via `data=`, never `json=`.
        response = client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    except httpx.HTTPError as e:
        # `str(e)` is safe to log here: httpx's own exception messages carry at
        # most a status/URL, never the request body or headers we just sent.
        logger.error(
            "sentinel_hub token request failed",
            extra={"event": "sentinel_hub.auth_error", "context": {"error": str(e)}},
        )
        raise SentinelHubAuthError(
            "Impossible de contacter le serveur d'authentification CDSE."
        ) from e

    if response.status_code != 200:
        description = safe_error_description(response)
        logger.error(
            "sentinel_hub token rejected",
            extra={
                "event": "sentinel_hub.auth_rejected",
                "context": {"status_code": response.status_code, "error_description": description},
            },
        )
        raise SentinelHubAuthError(
            f"Authentification CDSE refusée ({response.status_code}): {description}"
        )

    body = response.json()
    try:
        access_token = str(body["access_token"])
        expires_in = float(body["expires_in"])
    except KeyError as e:
        raise SentinelHubAuthError(
            f"Réponse d'authentification CDSE incomplète : champ {e.args[0]} manquant."
        ) from e

    return _CachedToken(access_token=access_token, expires_at=time.monotonic() + expires_in)


def get_access_token(client: httpx.Client) -> str:
    """
    Return a cached, valid access token, refreshing it ahead of expiry.

    Thread-safe: callers running zone extractions concurrently (job-level
    `max_workers`) share one cached token instead of each fetching their own.
    """
    # `_cached_token` is a module-level variable (not an attribute on some
    # object) because every caller in the process should share the exact same
    # cached token — there's nothing to instantiate here, just functions.
    # `global` is required to *reassign* it (reading it wouldn't need it).
    global _cached_token
    # The lock matters once the job calls this from several worker threads at
    # once (`max_workers`): without it, two threads could both see an expired
    # token at the same time and both fire a refresh request, or one thread
    # could read `_cached_token` while another is only half-done writing it.
    with _token_lock:
        token = _cached_token
        stale = token is None or time.monotonic() >= (token.expires_at - _REFRESH_MARGIN_SECONDS)
        if stale or token is None:
            token = _fetch_token(client)
            _cached_token = token
        return token.access_token
