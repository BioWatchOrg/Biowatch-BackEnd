"""Tests for clients.sentinel_hub.statistics — request building, retry, parsing."""

import httpx
import pytest

from clients.sentinel_hub import statistics
from clients.sentinel_hub.exceptions import SentinelHubRequestError

GEOMETRY = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}
TIME_RANGE = ("2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")

_TOKEN_RESPONSE = httpx.Response(200, json={"access_token": "token-abc", "expires_in": 600})


def _full_statistics_response(**overrides: dict) -> dict:
    def band(mean: float | None, sample_count: int = 1000, no_data_count: int = 100) -> dict:
        stats = {"sampleCount": sample_count, "noDataCount": no_data_count}
        if mean is not None:
            stats["mean"] = mean
        return {"bands": {"B0": {"stats": stats}}}

    # No "dataMask" entry: the API consumes an output literally named
    # "dataMask" as the mask applied to every other output and never echoes
    # it back as a queryable output itself (confirmed against a real
    # response — see statistics._parse_statistics).
    outputs = {
        "ndvi": band(0.42),
        "ndwi": band(-0.31),
        "ndbi": band(0.05),
        "swir": band(1820.0),
    }
    outputs.update(overrides)
    return {"data": [{"interval": {}, "outputs": outputs}]}


def _client(statistics_handler) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == statistics.STATISTICS_URL:
            return statistics_handler(request)
        return _TOKEN_RESPONSE

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_zone_statistics_success(credentials):
    client = _client(lambda request: httpx.Response(200, json=_full_statistics_response()))

    result = statistics.fetch_zone_statistics(GEOMETRY, TIME_RANGE, client=client)

    assert result.ndvi_mean == 0.42
    assert result.ndwi_mean == -0.31
    assert result.ndbi_mean == 0.05
    assert result.swir_mean == 1820.0
    assert result.obs_count == 1000
    assert result.valid_pixel_ratio == 0.9
    assert result.cloud_score == pytest.approx(0.1)


def test_single_request_computes_all_indices(credentials):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = request.content.decode()
        for index in ("ndvi", "ndwi", "ndbi", "swir"):
            assert index in body
        return httpx.Response(200, json=_full_statistics_response())

    statistics.fetch_zone_statistics(GEOMETRY, TIME_RANGE, client=_client(handler))
    assert len(calls) == 1


def test_retry_on_429_then_success(credentials, monkeypatch):
    monkeypatch.setattr(statistics.time, "sleep", lambda seconds: None)
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json=_full_statistics_response()),
        ]
    )

    result = statistics.fetch_zone_statistics(
        GEOMETRY, TIME_RANGE, client=_client(lambda request: next(responses))
    )
    assert result.obs_count == 1000


def test_no_retry_on_400(credentials):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, json={"error_description": "invalid evalscript"})

    with pytest.raises(SentinelHubRequestError, match="invalid evalscript"):
        statistics.fetch_zone_statistics(GEOMETRY, TIME_RANGE, client=_client(handler))
    assert len(calls) == 1  # no retry on a non-429 error


def test_empty_data_raises(credentials):
    client = _client(lambda request: httpx.Response(200, json={"data": []}))
    with pytest.raises(SentinelHubRequestError, match="aucun intervalle"):
        statistics.fetch_zone_statistics(GEOMETRY, TIME_RANGE, client=client)


def test_missing_output_raises(credentials):
    response = _full_statistics_response()
    del response["data"][0]["outputs"]["ndvi"]
    client = _client(lambda request: httpx.Response(200, json=response))
    with pytest.raises(SentinelHubRequestError, match="ndvi"):
        statistics.fetch_zone_statistics(GEOMETRY, TIME_RANGE, client=client)


def test_creates_and_closes_own_client_when_not_provided(credentials, monkeypatch):
    closed = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == statistics.STATISTICS_URL:
            return httpx.Response(200, json=_full_statistics_response())
        return _TOKEN_RESPONSE

    owned_client = httpx.Client(transport=httpx.MockTransport(handler))
    original_close = owned_client.close
    monkeypatch.setattr(owned_client, "close", lambda: (closed.append(True), original_close())[1])
    monkeypatch.setattr(statistics.httpx, "Client", lambda *a, **k: owned_client)

    result = statistics.fetch_zone_statistics(GEOMETRY, TIME_RANGE)

    assert result.obs_count == 1000
    assert closed == [True]


def test_fully_masked_zone_has_null_means(credentials):
    # Every pixel masked out: the anchor ("ndvi") has samples but none valid
    # (no "mean" key — nothing to average), so valid_pixel_ratio is derived
    # from its own sampleCount/noDataCount being equal.
    response = _full_statistics_response(
        ndvi={"bands": {"B0": {"stats": {"sampleCount": 1000, "noDataCount": 1000}}}},
    )
    client = _client(lambda request: httpx.Response(200, json=response))

    result = statistics.fetch_zone_statistics(GEOMETRY, TIME_RANGE, client=client)
    assert result.ndvi_mean is None
    assert result.obs_count == 1000
    assert result.valid_pixel_ratio == 0.0
    assert result.cloud_score == 1.0


def test_valid_pixel_ratio_derived_from_anchor_sample_and_no_data_count(credentials):
    """valid_pixel_ratio is 1 - noDataCount/sampleCount read off the "ndvi"
    output's own stats, not a separate "dataMask" output (never returned)."""
    response = _full_statistics_response(
        ndvi={"bands": {"B0": {"stats": {"mean": 0.42, "sampleCount": 1000, "noDataCount": 250}}}},
    )
    client = _client(lambda request: httpx.Response(200, json=response))

    result = statistics.fetch_zone_statistics(GEOMETRY, TIME_RANGE, client=client)
    assert result.obs_count == 1000
    assert result.valid_pixel_ratio == pytest.approx(0.75)
    assert result.cloud_score == pytest.approx(0.25)
