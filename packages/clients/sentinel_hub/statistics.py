"""
Sentinel Hub Statistical API client (Copernicus Data Space Ecosystem).

One request per zone per time range computes NDVI, NDWI, NDBI, SWIR and a
cloud/valid-pixel mask in a single evalscript — never one request per index,
to stay well inside both the rate limit and the free processing-unit quota.
"""

import logging
import time
from typing import Any

import httpx

from .auth import get_access_token
from .exceptions import SentinelHubRequestError
from .http_errors import safe_error_description
from .models import ZoneStatistics

logger = logging.getLogger(__name__)

STATISTICS_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"

_MAX_RETRY_ATTEMPTS = 5
_DEFAULT_TIMEOUT_SECONDS = 30.0

# SCL (Scene Classification Layer) codes excluded from the valid-pixel mask:
# cloud shadow (3), cloud medium probability (8), cloud high probability (9),
# thin cirrus (10), snow/ice (11). See the Sentinel-2 L2A SCL legend.
_CLOUD_SCL_CLASSES = (3, 8, 9, 10, 11)

_EVALSCRIPT = """//VERSION=3
function setup() {{
  return {{
    input: [{{ bands: ["B03", "B04", "B08", "B11", "B12", "SCL", "dataMask"] }}],
    output: [
      {{ id: "ndvi", bands: 1 }},
      {{ id: "ndwi", bands: 1 }},
      {{ id: "ndbi", bands: 1 }},
      {{ id: "swir", bands: 1 }},
      {{ id: "dataMask", bands: 1 }}
    ]
  }};
}}
function evaluatePixel(s) {{
  var cloudFree = [{cloud_classes}].indexOf(s.SCL) === -1 ? 1 : 0;
  return {{
    ndvi: [index(s.B08, s.B04)],
    ndwi: [index(s.B03, s.B08)],
    ndbi: [index(s.B11, s.B08)],
    swir: [s.B12],
    dataMask: [s.dataMask * cloudFree]
  }};
}}
""".format(cloud_classes=",".join(str(c) for c in _CLOUD_SCL_CLASSES))


def fetch_zone_statistics(
    geometry: dict[str, Any],
    time_range: tuple[str, str],
    *,
    max_cloud_coverage: int = 80,
    client: httpx.Client | None = None,
) -> ZoneStatistics:
    """
    Fetch aggregated Sentinel-2 indices and quality metrics for one zone.

    `geometry` is a GeoJSON Polygon/MultiPolygon in EPSG:4326 (e.g. the output
    of `geo.h3.cell_to_geojson`). `time_range` is an (ISO 8601 start, end) pair
    covering the aggregation period — the caller (the job) derives it from a
    monthly `bucket_id`; this client only knows about time ranges.

    Pass a shared `httpx.Client` when calling this repeatedly (e.g. once per
    H3 zone within a job run) to reuse the TCP connection and the cached auth
    token; otherwise a client is created and closed for this call alone.
    """
    owns_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=_DEFAULT_TIMEOUT_SECONDS)
    try:
        body: dict[str, Any] = {
            "input": {
                "bounds": {
                    "geometry": geometry,
                    "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
                },
                "data": [
                    {
                        "type": "sentinel-2-l2a",
                        "dataFilter": {"maxCloudCoverage": max_cloud_coverage},
                    }
                ],
            },
            "aggregation": {
                "timeRange": {"from": time_range[0], "to": time_range[1]},
                "aggregationInterval": {"of": "P1M"},
                "resx": 10,
                "resy": 10,
                "evalscript": _EVALSCRIPT,
            },
            "calculations": {"default": {"statistics": {"default": {}}}},
        }

        response = _post_with_retry(http_client, body)
        return _parse_statistics(response.json())
    finally:
        if owns_client:
            http_client.close()


def _post_with_retry(client: httpx.Client, body: dict[str, Any]) -> httpx.Response:
    """
    POST to the Statistical API, retrying with backoff on 429 only.

    Other 4xx (400 malformed evalscript, 401 bad/expired token, 403 quota)
    are permanent failures — retrying would hide a real bug instead of
    surfacing it, so they raise immediately via `_raise_for_status`.
    """
    response: httpx.Response | None = None
    for attempt in range(_MAX_RETRY_ATTEMPTS):
        token = get_access_token(client)
        response = client.post(
            STATISTICS_URL,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 429:
            break
        delay = float(response.headers.get("Retry-After", 2**attempt))
        logger.warning(
            "sentinel_hub rate limited, backing off",
            extra={
                "event": "sentinel_hub.rate_limited",
                "context": {"attempt": attempt, "delay_s": delay},
            },
        )
        time.sleep(delay)

    assert response is not None  # loop always runs at least once
    _raise_for_status(response)
    return response


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    description = safe_error_description(response)
    logger.error(
        "sentinel_hub statistics request failed",
        extra={
            "event": "sentinel_hub.request_failed",
            "context": {"status_code": response.status_code, "error_description": description},
        },
    )
    raise SentinelHubRequestError(
        f"Statistical API error ({response.status_code}): {description}"
    )


def _band_stats(outputs: dict[str, Any], output_id: str) -> dict[str, Any]:
    try:
        return dict(outputs[output_id]["bands"]["B0"]["stats"])
    except (KeyError, TypeError) as e:
        raise SentinelHubRequestError(
            f"Réponse Statistical API inattendue : sortie '{output_id}' absente ou mal formée."
        ) from e


def _optional_mean(outputs: dict[str, Any], output_id: str) -> float | None:
    mean = _band_stats(outputs, output_id).get("mean")
    return float(mean) if mean is not None else None


def _parse_statistics(payload: dict[str, Any]) -> ZoneStatistics:
    intervals = payload.get("data") or []
    if not intervals:
        raise SentinelHubRequestError(
            "Statistical API n'a renvoyé aucun intervalle pour la période demandée."
        )
    outputs = intervals[0]["outputs"]

    data_mask_stats = _band_stats(outputs, "dataMask")
    obs_count = int(data_mask_stats.get("sampleCount", 0))
    valid_pixel_ratio = float(data_mask_stats.get("mean", 0.0))

    return ZoneStatistics(
        ndvi_mean=_optional_mean(outputs, "ndvi"),
        ndwi_mean=_optional_mean(outputs, "ndwi"),
        ndbi_mean=_optional_mean(outputs, "ndbi"),
        swir_mean=_optional_mean(outputs, "swir"),
        obs_count=obs_count,
        valid_pixel_ratio=valid_pixel_ratio,
        cloud_score=1.0 - valid_pixel_ratio,
    )
