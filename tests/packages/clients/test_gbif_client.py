"""Tests for clients.gbif.client — GBIF Occurrence Search pagination."""

import httpx
import pytest

from clients.gbif.client import GBIF_PAGE_LIMIT, GBIF_SEARCH_URL, GbifApiError, fetch_occurrences


def _response(results: list[dict], end_of_records: bool) -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": results, "endOfRecords": end_of_records},
        request=httpx.Request("GET", GBIF_SEARCH_URL),
    )


def test_fetch_occurrences_single_page(monkeypatch):
    calls = []

    def fake_get(self, url, params=None, **kwargs):
        calls.append(params)
        return _response([{"key": 1}, {"key": 2}], end_of_records=True)

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    records = list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020))

    assert [r["key"] for r in records] == [1, 2]
    assert len(calls) == 1
    assert calls[0]["geometry"] == "POLYGON((0 0,0 1,1 1,1 0,0 0))"
    assert calls[0]["year"] == 2020
    assert calls[0]["hasCoordinate"] == "true"
    assert calls[0]["limit"] == GBIF_PAGE_LIMIT
    assert calls[0]["offset"] == 0


def test_fetch_occurrences_paginates_until_end_of_records(monkeypatch):
    pages = [
        _response([{"key": 1}], end_of_records=False),
        _response([{"key": 2}], end_of_records=False),
        _response([{"key": 3}], end_of_records=True),
    ]
    calls = []

    def fake_get(self, url, params=None, **kwargs):
        calls.append(params["offset"])
        return pages.pop(0)

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    records = list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020))

    assert [r["key"] for r in records] == [1, 2, 3]
    assert calls == [0, GBIF_PAGE_LIMIT, 2 * GBIF_PAGE_LIMIT]


def test_fetch_occurrences_raises_on_http_error(monkeypatch):
    def fake_get(self, url, params=None, **kwargs):
        return httpx.Response(500, request=httpx.Request("GET", GBIF_SEARCH_URL))

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    with pytest.raises(GbifApiError):
        list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020))


def test_fetch_occurrences_uses_injected_client():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"results": [{"key": 42}], "endOfRecords": True}
            )
        )
    )

    records = list(fetch_occurrences("POLYGON((0 0,0 1,1 1,1 0,0 0))", 2020, client=client))

    assert [r["key"] for r in records] == [42]
    client.close()
