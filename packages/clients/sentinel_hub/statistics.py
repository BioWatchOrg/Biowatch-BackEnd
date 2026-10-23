"""
Sentinel Hub Statistical API client (Copernicus Data Space Ecosystem).

One request per zone per time range computes NDVI, NDWI, NDBI, SWIR and a
cloud/valid-pixel mask in a single evalscript — never one request per index,
to stay well inside both the rate limit and the free processing-unit quota.

Detailed walkthrough of the evalscript and an example API response:
https://app.notion.com/p/Extraction-Sentinel-2-3ee50bea018d80919b5ac91d3031f345
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

# These are two different, complementary filters — not redundant:
# - `max_cloud_coverage` (passed to the API below) is a coarse, SCENE-level
#   filter: "ignore whole Sentinel-2 images whose global cloud % exceeds this".
#   It just avoids wasting compute on images that are obviously unusable.
# - `_CLOUD_SCL_CLASSES` below is a precise, PIXEL-level filter applied inside
#   the evalscript: for images that pass the scene filter, which individual
#   pixels are still cloud/shadow/snow and must be excluded from the average.
#   This is what actually drives `valid_pixel_ratio` / `cloud_score`.
# SCL (Scene Classification Layer) codes excluded from the valid-pixel mask:
# cloud shadow (3), cloud medium probability (8), cloud high probability (9),
# thin cirrus (10), snow/ice (11). See the Sentinel-2 L2A SCL legend.
_CLOUD_SCL_CLASSES = (3, 8, 9, 10, 11)

# An "evalscript" is a small JavaScript function that Sentinel Hub runs
# *server-side*, once per pixel, over the raw satellite bands — this is how
# you tell the API "compute NDVI/NDWI/... for me" instead of downloading raw
# bands and computing indices ourselves. Two parts:
# - `setup()`: declares which bands we read (`input`) and which named outputs
#   we produce (`output`) — one output per index, plus `dataMask`.
# - `evaluatePixel(s)`: runs for every pixel; `s` holds that pixel's band
#   values. `index(a, b)` is a Sentinel Hub builtin for `(a-b)/(a+b)`, the
#   formula shared by NDVI/NDWI/NDBI. `cloudFree` is 0 for pixels whose `SCL`
#   class is in `_CLOUD_SCL_CLASSES` above, 1 otherwise; multiplying
#   `dataMask` by it means "this pixel counts as valid only if it was already
#   valid AND it isn't cloud/shadow/snow".
# The `{{`/`}}` doubled braces below are just Python's `str.format()` escaping
# for literal `{`/`}` in the JS — `{cloud_classes}` is the one real
# placeholder, filled in with the SCL codes from `_CLOUD_SCL_CLASSES`.

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
    # `httpx.Client` holds a reusable TCP/TLS connection (like a `requests.Session`).
    # Accepting one as a parameter — instead of always creating our own inside
    # this function — lets the job (which calls this once per H3 zone, so
    # potentially thousands of times per run) pass ONE shared client and reuse
    # both the connection and the cached auth token across every call, rather
    # than paying a fresh TCP handshake + token fetch each time. When nobody
    # passes one (e.g. a one-off call, or a test), we create and close our own
    # so the function still works standalone.
    owns_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=_DEFAULT_TIMEOUT_SECONDS)
    try:
        body: dict[str, Any] = {
            "input": {
                "bounds": {
                    "geometry": geometry,
                    # CRS = Coordinate Reference System: this tells the API our
                    # `geometry` coordinates are plain lon/lat (EPSG:4326, aka
                    # WGS84) — the same system GeoJSON and `geo.h3` use.
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
                # "P1M" = ISO 8601 duration for "1 month" — groups every pixel
                # observation across `time_range` into one aggregated result,
                # matching our monthly `bucket_id`. ("P1D" would mean "one
                # result per day" instead.)
                "aggregationInterval": {"of": "P1M"},
                # Pixel size in meters for the stats computation grid. Sentinel-2
                # bands aren't all the same native resolution (B03/B04/B08 are
                # 10m, B11/B12 are 20m); requesting 10m here makes the API
                # resample everything to a common grid before averaging.
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
        # When the API tells us how long to wait (`Retry-After`, in seconds),
        # trust that over guessing. Only fall back to our own exponential
        # backoff (1s, 2s, 4s, ...) when the header is absent.
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


# Shape of a successful Statistical API response (trimmed):
#   {"data": [{"outputs": {
#       "ndvi":     {"bands": {"B0": {"stats": {"mean": 0.42, "sampleCount": 1000, ...}}}},
#       "dataMask": {"bands": {"B0": {"stats": {"mean": 0.87, "sampleCount": 1000, ...}}}},
#       ...
#   }}]}
# "B0" is fixed: each of our outputs (ndvi/ndwi/.../dataMask) declares exactly
# one band in the evalscript, so there's only ever a single "B0" per output.


def _band_stats(outputs: dict[str, Any], output_id: str) -> dict[str, Any]:
    """Return the `stats` dict for one evalscript output (e.g. "ndvi")."""
    try:
        return dict(outputs[output_id]["bands"]["B0"]["stats"])
    except (KeyError, TypeError) as e:
        raise SentinelHubRequestError(
            f"Réponse Statistical API inattendue : sortie '{output_id}' absente ou mal formée. "
            f"Contenu brut reçu pour cette sortie : {outputs.get(output_id)!r}"
        ) from e


def _optional_mean(outputs: dict[str, Any], output_id: str) -> float | None:
    """The `mean` of one output, or `None` if the API computed no pixel for it
    (e.g. a zone fully masked by clouds has no `ndvi` mean to report)."""
    mean = _band_stats(outputs, output_id).get("mean")
    return float(mean) if mean is not None else None


def _parse_statistics(payload: dict[str, Any]) -> ZoneStatistics:
    """Turn the raw JSON response above into our `ZoneStatistics` type."""
    intervals = payload.get("data") or []
    if not intervals:
        raise SentinelHubRequestError(
            "Statistical API n'a renvoyé aucun intervalle pour la période demandée."
        )
    outputs = intervals[0]["outputs"]

    # The evalscript output literally named "dataMask" is consumed by the API
    # as the validity mask applied to every OTHER output's statistics — it is
    # never echoed back as a queryable output itself (confirmed against a
    # real response: `outputs.get("dataMask")` is None). sampleCount/
    # noDataCount are identical across ndvi/ndwi/ndbi/swir (the same mask is
    # applied to all of them), so any one of them gives us the valid-pixel
    # accounting; "ndvi" is used as the anchor.
    anchor_stats = _band_stats(outputs, "ndvi")
    obs_count = int(anchor_stats.get("sampleCount", 0))
    no_data_count = int(anchor_stats.get("noDataCount", 0))
    valid_pixel_ratio = 1.0 - (no_data_count / obs_count) if obs_count else 0.0

    return ZoneStatistics(
        ndvi_mean=_optional_mean(outputs, "ndvi"),
        ndwi_mean=_optional_mean(outputs, "ndwi"),
        ndbi_mean=_optional_mean(outputs, "ndbi"),
        swir_mean=_optional_mean(outputs, "swir"),
        obs_count=obs_count,
        valid_pixel_ratio=valid_pixel_ratio,
        cloud_score=1.0 - valid_pixel_ratio,
    )
