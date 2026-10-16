"""Tests pour clients.iucn — client de l'API IUCN v4, sans réseau (MockTransport)."""

import httpx
import pytest

from clients.iucn import TOKEN_VAR, IucnApiError, IucnClient


def _client(handler, token="tok"):
    return IucnClient(token=token, transport=httpx.MockTransport(handler))


def test_missing_token_raises_a_clear_error(monkeypatch):
    monkeypatch.delenv(TOKEN_VAR, raising=False)
    with pytest.raises(IucnApiError, match=TOKEN_VAR):
        IucnClient()


def test_token_is_read_from_the_environment_and_sent_as_bearer(monkeypatch):
    monkeypatch.setenv(TOKEN_VAR, "secret-tok")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"red_list_version": "2026-1"})

    with IucnClient(transport=httpx.MockTransport(handler)) as client:
        client.red_list_version()

    assert seen["auth"] == "Bearer secret-tok"


def test_red_list_version():
    def handler(request):
        assert request.url.path == "/api/v4/information/red_list_version"
        return httpx.Response(200, json={"red_list_version": "2026-1"})

    assert _client(handler).red_list_version() == "2026-1"


def test_red_list_version_missing_in_response_raises():
    client = _client(lambda request: httpx.Response(200, json={}))
    with pytest.raises(IucnApiError):
        client.red_list_version()


def test_http_error_is_wrapped():
    client = _client(lambda request: httpx.Response(401, json={"error": "unauthorised"}))
    with pytest.raises(IucnApiError, match="401"):
        client.red_list_version()


def _page(assessments, total_pages="1", total_count=None):
    headers = {"total-pages": total_pages}
    headers["total-count"] = str(len(assessments) if total_count is None else total_count)
    return httpx.Response(200, headers=headers, json={"assessments": assessments})


def test_country_assessments_walk_every_page():
    pages = {1: [{"sis_taxon_id": 1}, {"sis_taxon_id": 2}], 2: [{"sis_taxon_id": 3}]}
    requested = []

    def handler(request):
        assert request.url.path == "/api/v4/countries/FR"
        assert request.url.params["latest"] == "true"
        assert request.url.params["scope_code"] == "1"  # évaluations globales
        page = int(request.url.params["page"])
        requested.append(page)
        return _page(pages[page], total_pages="2", total_count=3)

    result = list(_client(handler).iter_country_assessments("FR"))

    assert requested == [1, 2]
    assert [a["sis_taxon_id"] for a in result] == [1, 2, 3]


@pytest.mark.parametrize("headers", [{}, {"total-pages": "beaucoup", "total-count": "1"}])
def test_missing_or_invalid_pagination_header_raises(headers):
    """Sans `total-pages` fiable, on ne sait pas si la liste est complète : on refuse."""

    def handler(request):
        return httpx.Response(200, headers=headers, json={"assessments": [{"sis_taxon_id": 1}]})

    with pytest.raises(IucnApiError, match="total-pages"):
        list(_client(handler).iter_country_assessments("FR"))


def test_truncated_list_raises():
    """Moins d'évaluations reçues que le total annoncé → pas d'import partiel « réussi »."""
    client = _client(lambda request: _page([{"sis_taxon_id": 1}], total_count=5002))
    with pytest.raises(IucnApiError, match="incomplètes"):
        list(client.iter_country_assessments("FR"))


def test_non_json_body_raises():
    """Ex. page HTML de maintenance renvoyée en 200 par un proxy."""
    client = _client(
        lambda request: httpx.Response(
            200, headers={"total-pages": "1", "total-count": "0"}, text="<html>maintenance</html>"
        )
    )
    with pytest.raises(IucnApiError, match="non JSON"):
        list(client.iter_country_assessments("FR"))


@pytest.mark.parametrize("body", [[{"sis_taxon_id": 1}], {"result": []}, {"assessments": {}}])
def test_unexpected_body_shape_raises(body):
    client = _client(
        lambda request: httpx.Response(
            200, headers={"total-pages": "1", "total-count": "1"}, json=body
        )
    )
    with pytest.raises(IucnApiError):
        list(client.iter_country_assessments("FR"))


def test_transport_error_is_wrapped():
    def handler(request):
        raise httpx.ConnectError("unreachable")

    with pytest.raises(IucnApiError):
        _client(handler).red_list_version()
