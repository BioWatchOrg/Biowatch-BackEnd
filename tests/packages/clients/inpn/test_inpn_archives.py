"""Tests pour clients.inpn.archives — date d'édition, téléchargement, reprises sur 429."""

import datetime

import clients.inpn.archives as archives
import httpx
import pytest
from clients.inpn import InpnError, archive_last_modified, download_archive

URL = "https://assets.patrinat.fr/files/donnees/ep/EP.zip"
LAST_MODIFIED = "Fri, 06 Nov 2026 15:12:42 GMT"


@pytest.fixture
def sleeps(monkeypatch):
    """Attentes demandées entre deux essais (aucune attente réelle)."""
    waited: list[float] = []
    monkeypatch.setattr(archives, "_sleep", waited.append)
    return waited


def _client(*responses):
    """Client qui renvoie les réponses dans l'ordre ; une exception est levée telle quelle."""
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.seen = seen  # type: ignore[attr-defined]
    return client


# --- archive_last_modified --------------------------------------------------------


def test_edition_is_the_last_modified_day_in_utc(sleeps):
    client = _client(httpx.Response(200, headers={"Last-Modified": LAST_MODIFIED}))
    assert archive_last_modified(URL, client) == datetime.date(2026, 11, 6)
    assert [r.method for r in client.seen] == ["HEAD"]  # rien n'est téléchargé
    assert sleeps == []


def test_edition_is_converted_to_utc_before_taking_the_day(sleeps):
    client = _client(
        httpx.Response(200, headers={"Last-Modified": "Sat, 07 Nov 2026 00:30:00 +0100"})
    )
    assert archive_last_modified(URL, client) == datetime.date(2026, 11, 6)


def test_too_many_requests_is_retried_with_the_delay_asked_by_the_server(sleeps):
    client = _client(
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(503),
        httpx.Response(200, headers={"Last-Modified": LAST_MODIFIED}),
    )
    assert archive_last_modified(URL, client) == datetime.date(2026, 11, 6)
    assert sleeps == [7.0, archives.RETRY_DELAYS_S[1]]


def test_retry_after_is_capped(sleeps):
    client = _client(
        httpx.Response(429, headers={"Retry-After": "86400"}),
        httpx.Response(200, headers={"Last-Modified": LAST_MODIFIED}),
    )
    archive_last_modified(URL, client)
    assert sleeps == [archives.MAX_RETRY_AFTER_S]


def test_network_errors_are_retried_then_reported(sleeps):
    client = _client(*[httpx.ConnectError("refusée")] * (len(archives.RETRY_DELAYS_S) + 1))
    with pytest.raises(InpnError, match="injoignable après reprises"):
        archive_last_modified(URL, client)
    assert sleeps == list(archives.RETRY_DELAYS_S)


@pytest.mark.parametrize(
    "response, message",
    [
        (httpx.Response(404), "introuvable"),
        (httpx.Response(200), "sans en-tête Last-Modified"),
        (httpx.Response(200, headers={"Last-Modified": "hier"}), "illisible"),
    ],
)
def test_missing_archive_or_header_fails_without_retry(sleeps, response, message):
    with pytest.raises(InpnError, match=message):
        archive_last_modified(URL, _client(response))
    assert sleeps == []


def test_a_client_is_created_and_closed_when_none_is_given(monkeypatch, sleeps):
    created = _client(httpx.Response(200, headers={"Last-Modified": LAST_MODIFIED}))
    monkeypatch.setattr(archives, "_new_client", lambda: created)
    archive_last_modified(URL)
    assert created.is_closed


# --- download_archive -------------------------------------------------------------


def test_download_writes_the_archive_under_its_name(tmp_path, sleeps):
    body = b"PK" + b"x" * 100
    client = _client(httpx.Response(200, content=body))
    path = download_archive(URL, tmp_path / "work", client)
    assert path == tmp_path / "work" / "EP.zip"
    assert path.read_bytes() == body
    assert sorted(p.name for p in path.parent.iterdir()) == ["EP.zip"]  # pas de .part laissé


def test_truncated_download_is_retried(tmp_path, sleeps):
    short = httpx.Response(200, headers={"Content-Length": "10"}, content=b"PK123")
    short.stream = httpx.ByteStream(b"PK123")  # 5 octets reçus au lieu de 10 annoncés
    client = _client(short, httpx.Response(200, content=b"PK12345678"))
    path = download_archive(URL, tmp_path, client)
    assert path.read_bytes() == b"PK12345678"
    assert sleeps == [archives.RETRY_DELAYS_S[0]]


def test_failed_download_leaves_no_file(tmp_path, sleeps):
    client = _client(*[httpx.Response(429)] * (len(archives.RETRY_DELAYS_S) + 1))
    with pytest.raises(InpnError):
        download_archive(URL, tmp_path, client)
    assert list(tmp_path.iterdir()) == []


def test_missing_archive_is_not_retried(tmp_path, sleeps):
    with pytest.raises(InpnError, match="HTTP 404"):
        download_archive(URL, tmp_path, _client(httpx.Response(404)))
    assert sleeps == []
    assert list(tmp_path.iterdir()) == []
