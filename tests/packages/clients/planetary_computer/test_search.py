"""Tests for clients.planetary_computer.search.search_scenes (no network)."""

import datetime
import logging

import pytest
import requests
from pystac_client.exceptions import APIError

import clients.planetary_computer.search as search_module
from clients.planetary_computer import MAX_SCENE_CLOUD_COVER, PlanetaryComputerError, search_scenes

BBOX = (2.2, 48.8, 2.5, 48.9)
MAY_START, MAY_END = datetime.date(2025, 5, 1), datetime.date(2025, 5, 31)


class FakeClient:
    """Remplace pystac_client.Client : enregistre les paramètres de recherche."""

    def __init__(self, items=(), error=None):
        self._items = list(items)
        self._error = error
        self.search_kwargs = None

    def search(self, **kwargs):
        self.search_kwargs = kwargs
        if self._error:
            raise self._error
        return self

    def items(self):
        return iter(self._items)


def test_search_returns_scenes(stac_items):
    client = FakeClient(stac_items)
    scenes = search_scenes(BBOX, MAY_START, MAY_END, client=client)
    assert [s.scene_id for s in scenes] == [item.id for item in stac_items]


def test_search_parameters(stac_items):
    client = FakeClient(stac_items)
    search_scenes(BBOX, MAY_START, MAY_END, client=client)
    kwargs = client.search_kwargs
    assert kwargs["collections"] == ["sentinel-2-l2a"]
    assert kwargs["bbox"] == list(BBOX)
    # Le dernier jour du mois doit être inclus jusqu'à 23:59:59.
    assert kwargs["datetime"] == "2025-05-01T00:00:00Z/2025-05-31T23:59:59Z"
    assert kwargs["query"] == {"eo:cloud_cover": {"lt": MAX_SCENE_CLOUD_COVER}}


def test_search_custom_cloud_cover():
    client = FakeClient()
    search_scenes(BBOX, MAY_START, MAY_END, max_cloud_cover=20, client=client)
    assert client.search_kwargs["query"] == {"eo:cloud_cover": {"lt": 20}}


def test_default_cloud_cover_is_70():
    assert MAX_SCENE_CLOUD_COVER == 70


def test_search_empty_result():
    assert search_scenes(BBOX, MAY_START, MAY_END, client=FakeClient()) == []


@pytest.mark.parametrize(
    "error", [APIError("boom"), requests.ConnectionError("down"), requests.Timeout("slow")]
)
def test_search_errors_are_wrapped(error):
    with pytest.raises(PlanetaryComputerError, match="STAC search failed"):
        search_scenes(BBOX, MAY_START, MAY_END, client=FakeClient(error=error))


def test_search_logs_summary(stac_items, caplog):
    with caplog.at_level(logging.INFO, logger=search_module.__name__):
        search_scenes(BBOX, MAY_START, MAY_END, client=FakeClient(stac_items))
    record = next(r for r in caplog.records if r.event == "planetary_computer.search_done")
    assert record.context["n_scenes"] == 3
    assert record.context["n_tiles"] == 1


def test_default_client_uses_retry_and_timeout(monkeypatch):
    captured = {}

    def fake_open(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(search_module.Client, "open", fake_open)
    search_scenes(BBOX, MAY_START, MAY_END)
    assert captured["url"] == "https://planetarycomputer.microsoft.com/api/stac/v1"
    stac_io = captured["stac_io"]
    retry = stac_io.session.adapters["https://"].max_retries
    assert retry.total == 5
    assert 429 in retry.status_forcelist
    assert retry.allowed_methods is None  # la recherche STAC est un POST
