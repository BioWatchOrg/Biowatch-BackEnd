"""Tests for clients.sentinel_hub.auth — OAuth2 token fetch, caching, refresh."""

import httpx
import pytest

from clients.sentinel_hub import auth
from clients.sentinel_hub.exceptions import SentinelHubAuthError


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_missing_credentials_raises(clean_env):
    with pytest.raises(RuntimeError, match="SENTINEL_HUB_CLIENT_ID"):
        auth.get_access_token(_client(lambda request: httpx.Response(200)))


def test_fetch_token_success(credentials):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == auth.TOKEN_URL
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        body = request.content.decode()
        assert "grant_type=client_credentials" in body
        assert "client_id=test-client-id" in body
        assert "client_secret=test-client-secret" in body
        return httpx.Response(200, json={"access_token": "token-abc", "expires_in": 600})

    token = auth.get_access_token(_client(handler))
    assert token == "token-abc"


def test_token_is_cached_until_near_expiry(credentials):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": "token-abc", "expires_in": 600})

    client = _client(handler)
    first = auth.get_access_token(client)
    second = auth.get_access_token(client)

    assert first == second == "token-abc"
    assert len(calls) == 1  # second call served from cache, no new HTTP request


def test_token_refreshed_near_expiry(credentials):
    responses = iter(
        [
            httpx.Response(200, json={"access_token": "token-1", "expires_in": 600}),
            httpx.Response(200, json={"access_token": "token-2", "expires_in": 600}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = _client(handler)
    first = auth.get_access_token(client)
    assert first == "token-1"

    # Simulate time passing past the refresh margin without sleeping in the test.
    from clients.sentinel_hub.models import _CachedToken

    auth._cached_token = _CachedToken(access_token="token-1", expires_at=0.0)

    second = auth.get_access_token(client)
    assert second == "token-2"


def test_auth_rejected_raises(credentials):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_client", "error_description": "Invalid client secret"}
        )

    with pytest.raises(SentinelHubAuthError, match="Invalid client secret"):
        auth.get_access_token(_client(handler))


def test_transport_error_raises(credentials):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(SentinelHubAuthError):
        auth.get_access_token(_client(handler))


def test_incomplete_response_raises(credentials):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "bearer"})  # missing access_token

    with pytest.raises(SentinelHubAuthError, match="access_token"):
        auth.get_access_token(_client(handler))
