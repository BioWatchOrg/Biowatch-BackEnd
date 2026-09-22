"""Tests for clients.gbif.client — GBIF Occurrence Download API."""

import csv
import io
import tempfile
import zipfile
from datetime import date

import httpx
import pytest

from clients.gbif.client import (
    GBIF_DOWNLOAD_FILE_URL,
    GBIF_DOWNLOAD_REQUEST_URL,
    GBIF_DOWNLOAD_STATUS_URL,
    GbifApiError,
    GbifCredentials,
    GbifDownloadTimeoutError,
    build_predicate,
    load_gbif_credentials,
    request_download,
    stream_download,
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
    # Isolate from the real .env file (this worktree genuinely has
    # GBIF_USERNAME/GBIF_PASSWORD set for Task 17's real smoke test) so this
    # test's own monkeypatch.setenv calls are the only source of truth for
    # os.environ, regardless of what's on disk.
    monkeypatch.setattr("clients.gbif.client.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("GBIF_USERNAME", "alice")
    monkeypatch.setenv("GBIF_PASSWORD", "secret")

    creds = load_gbif_credentials()

    assert creds.username == "alice"
    assert creds.password == "secret"
    assert isinstance(creds.auth, httpx.BasicAuth)


def test_load_gbif_credentials_raises_on_missing_username(monkeypatch):
    # Same isolation as above -- without this, load_gbif_credentials()'s own
    # load_dotenv(override=False) call repopulates GBIF_USERNAME from the
    # real .env file after delenv removes it from os.environ, silently
    # defeating this test on any machine with real GBIF credentials
    # configured (as this worktree now has, for Task 17).
    monkeypatch.setattr("clients.gbif.client.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("GBIF_USERNAME", raising=False)
    monkeypatch.setenv("GBIF_PASSWORD", "secret")

    with pytest.raises(RuntimeError, match="GBIF_USERNAME"):
        load_gbif_credentials()


def test_gbif_credentials_repr_omits_password():
    """Latent logging footgun from final review: repr()/str() must never
    leak the password (e.g. via a stray logger.debug(creds) or an exception
    traceback capturing local variables)."""
    creds = GbifCredentials(username="alice", password="super-secret")

    assert "super-secret" not in repr(creds)
    assert "super-secret" not in str(creds)
    assert repr(creds) == "GbifCredentials(username='alice')"


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


def test_wait_for_download_raises_on_http_error(monkeypatch):
    def fake_get(self, url, auth=None, **kwargs):
        return httpx.Response(500, request=httpx.Request("GET", url))

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


def _zipped_tsv(rows: list[dict[str, str]], *, member_name: str = "key123.csv") -> bytes:
    """
    Build a ZIP archive matching what GBIF's real Download API actually
    returns for a finished download (confirmed against the real API in
    Task 17): a single tab-separated member with a header row -- NOT a bare
    gzip stream, which is what this module originally (and wrongly) assumed.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]), delimiter="\t")
    writer.writeheader()
    writer.writerows(rows)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w") as zf:
        zf.writestr(member_name, buf.getvalue())
    return zip_buf.getvalue()


def test_stream_download_yields_parsed_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    body = _zipped_tsv(
        [
            {"gbifID": "1001", "scientificName": "Vulpes vulpes", "taxonKey": "5219243",
             "decimalLatitude": "48.8566", "decimalLongitude": "2.3522", "eventDate": "2020-06-15"},
            {"gbifID": "1002", "scientificName": "Bubo bubo", "taxonKey": "2497252",
             "decimalLatitude": "45.0", "decimalLongitude": "3.0", "eventDate": "2019-03-01"},
        ]
    )

    class _FakeStreamResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield body

    def fake_stream(self, method, url, auth=None, **kwargs):
        return _FakeStreamResponse()

    monkeypatch.setattr(httpx.Client, "stream", fake_stream)

    rows = list(stream_download("key123", auth=httpx.BasicAuth("a", "b")))

    assert [r["gbifID"] for r in rows] == ["1001", "1002"]
    assert rows[0]["scientificName"] == "Vulpes vulpes"


def test_stream_download_parses_zip_archive_not_gzip(monkeypatch, tmp_path):
    """
    Regression test for Task 17: the real GBIF Download API serves the
    finished download as a ZIP archive (e.g. member name
    "0001109-260921141020460.csv"), not a bare gzip stream -- confirmed
    against the real API, where the original gzip-based implementation
    failed immediately with `gzip.BadGzipFile: Not a gzipped file (b'PK')`
    (the zip magic bytes). This locks in the corrected zip-based parsing so
    it can't silently regress back to the gzip assumption.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    download_key = "0001109-260921141020460"
    body = _zipped_tsv(
        [
            {"gbifID": "2001", "scientificName": "Anthus campestris", "taxonKey": "2490274",
             "decimalLatitude": "43.48563", "decimalLongitude": "5.13173", "eventDate": "2020-06-01"},
        ],
        member_name=f"{download_key}.csv",
    )
    assert body[:2] == b"PK"  # sanity: this is a real zip, not a gzip stream

    class _FakeStreamResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield body

    def fake_stream(self, method, url, auth=None, **kwargs):
        return _FakeStreamResponse()

    monkeypatch.setattr(httpx.Client, "stream", fake_stream)

    rows = list(stream_download(download_key, auth=httpx.BasicAuth("a", "b")))

    assert len(rows) == 1
    assert rows[0]["gbifID"] == "2001"
    assert rows[0]["scientificName"] == "Anthus campestris"


def test_stream_download_raises_on_http_error(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    class _FakeErrorResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", GBIF_DOWNLOAD_FILE_URL), response=httpx.Response(404)
            )

        def iter_bytes(self):
            return iter([])

    def fake_stream(self, method, url, auth=None, **kwargs):
        return _FakeErrorResponse()

    monkeypatch.setattr(httpx.Client, "stream", fake_stream)

    with pytest.raises(GbifApiError):
        list(stream_download("key123", auth=httpx.BasicAuth("a", "b")))
