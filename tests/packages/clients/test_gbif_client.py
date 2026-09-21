"""Tests for clients.gbif.client — GBIF Occurrence Download API."""

from datetime import date

import httpx
import pytest

from clients.gbif.client import (
    GBIF_DOWNLOAD_REQUEST_URL,
    GBIF_DOWNLOAD_STATUS_URL,
    GbifApiError,
    GbifCredentials,
    GbifDownloadTimeoutError,
    build_predicate,
    load_gbif_credentials,
    request_download,
    wait_for_download,
)


def test_build_predicate_combines_geometry_and_date_range():
    predicate = build_predicate(
        "POLYGON((0 0,0 1,1 1,1 0,0 0))", date(2016, 1, 1), date(2025, 12, 31)
    )

    assert predicate["type"] == "and"
    sub = predicate["predicates"]
    assert {"type": "within", "geometry": "POLYGON((0 0,0 1,1 1,1 0,0 0))"} in sub
    assert {"type": "greaterThanOrEquals", "key": "EVENT_DATE", "value": "2016-01-01"} in sub
    assert {"type": "lessThanOrEquals", "key": "EVENT_DATE", "value": "2025-12-31"} in sub
    assert {"type": "equals", "key": "HAS_COORDINATE", "value": "true"} in sub
    assert {"type": "equals", "key": "HAS_GEOSPATIAL_ISSUE", "value": "false"} in sub


def test_load_gbif_credentials_reads_env_vars(monkeypatch):
    monkeypatch.setenv("GBIF_USERNAME", "alice")
    monkeypatch.setenv("GBIF_PASSWORD", "secret")

    creds = load_gbif_credentials()

    assert creds.username == "alice"
    assert creds.password == "secret"
    assert isinstance(creds.auth, httpx.BasicAuth)


def test_load_gbif_credentials_raises_on_missing_username(monkeypatch):
    monkeypatch.delenv("GBIF_USERNAME", raising=False)
    monkeypatch.setenv("GBIF_PASSWORD", "secret")

    with pytest.raises(RuntimeError, match="GBIF_USERNAME"):
        load_gbif_credentials()


def test_request_download_returns_key(monkeypatch):
    captured = {}

    def fake_post(self, url, json=None, auth=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        captured["auth"] = auth
        return httpx.Response(
            201, text="0012345-123456789012345", request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    key = request_download(
        {"type": "and", "predicates": []}, creator="alice", auth=httpx.BasicAuth("alice", "secret")
    )

    assert key == "0012345-123456789012345"
    assert captured["url"] == GBIF_DOWNLOAD_REQUEST_URL
    assert captured["json"]["creator"] == "alice"
    assert captured["json"]["format"] == "SIMPLE_CSV"


def test_request_download_raises_on_http_error(monkeypatch):
    def fake_post(self, url, json=None, auth=None, **kwargs):
        return httpx.Response(401, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    with pytest.raises(GbifApiError):
        request_download({"type": "and", "predicates": []}, creator="alice", auth=httpx.BasicAuth("a", "b"))


def test_wait_for_download_returns_on_succeeded(monkeypatch):
    responses = [
        httpx.Response(200, json={"status": "RUNNING"}, request=httpx.Request("GET", GBIF_DOWNLOAD_STATUS_URL)),
        httpx.Response(200, json={"status": "SUCCEEDED"}, request=httpx.Request("GET", GBIF_DOWNLOAD_STATUS_URL)),
    ]
    sleeps = []

    def fake_get(self, url, auth=None, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr("clients.gbif.client.time.sleep", lambda s: sleeps.append(s))

    wait_for_download("key123", auth=httpx.BasicAuth("a", "b"))  # must not raise

    assert len(sleeps) == 1


def test_wait_for_download_raises_on_failed_status(monkeypatch):
    def fake_get(self, url, auth=None, **kwargs):
        return httpx.Response(200, json={"status": "FAILED"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr("clients.gbif.client.time.sleep", lambda s: None)

    with pytest.raises(GbifApiError):
        wait_for_download("key123", auth=httpx.BasicAuth("a", "b"))


def test_wait_for_download_raises_on_timeout(monkeypatch):
    def fake_get(self, url, auth=None, **kwargs):
        return httpx.Response(200, json={"status": "RUNNING"}, request=httpx.Request("GET", url))

    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 10000.0  # jump well past any timeout on every call
        return clock["t"]

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr("clients.gbif.client.time.sleep", lambda s: None)
    monkeypatch.setattr("clients.gbif.client.time.monotonic", fake_monotonic)

    with pytest.raises(GbifDownloadTimeoutError):
        wait_for_download("key123", auth=httpx.BasicAuth("a", "b"))
