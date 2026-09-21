"""HTTP client for the GBIF Occurrence Search API.

No business logic here (no quality filtering, no H3, no DB) — this module
only knows how to talk to GBIF and paginate. See `species.quality` and
`species.gbif_occurrences` for the pipeline built on top of it.
"""

import logging
import time
from collections.abc import Iterator

import httpx

logger = logging.getLogger(__name__)

GBIF_SEARCH_URL = "https://api.gbif.org/v1/occurrence/search"
GBIF_PAGE_LIMIT = 300
GBIF_TIMEOUT_SECONDS = 30.0
GBIF_MAX_RETRIES = 5
GBIF_DEFAULT_RETRY_SECONDS = 2.0


class GbifApiError(Exception):
    """Raised when the GBIF Occurrence Search API returns an error response."""


def _retry_delay_seconds(response: httpx.Response, attempt: int) -> float:
    """Delay before retrying a 429: honor GBIF's Retry-After header when
    present (exact seconds to wait), otherwise exponential backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return GBIF_DEFAULT_RETRY_SECONDS * (2**attempt)


def _get_with_retry(
    http_client: httpx.Client, params: dict, year: int, offset: int
) -> httpx.Response:
    """GET one page, retrying on GBIF's 429 (rate limit) with backoff.

    Non-429 errors are returned as-is (not retried — those aren't
    transient); the caller's existing `raise_for_status()` handles them.
    If still 429 after `GBIF_MAX_RETRIES` attempts, the final 429 response
    is returned and `raise_for_status()` converts it to a `GbifApiError`
    the same way any other error status would be.
    """
    response = http_client.get(GBIF_SEARCH_URL, params=params)
    for attempt in range(GBIF_MAX_RETRIES):
        if response.status_code != 429:
            return response
        delay = _retry_delay_seconds(response, attempt)
        logger.warning(
            "GBIF rate limit hit, retrying",
            extra={
                "event": "gbif.rate_limited",
                "context": {"year": year, "offset": offset, "attempt": attempt, "delay_s": delay},
            },
        )
        time.sleep(delay)
        response = http_client.get(GBIF_SEARCH_URL, params=params)
    return response


def fetch_occurrences(
    geometry_wkt: str,
    year: int,
    client: httpx.Client | None = None,
) -> Iterator[dict]:
    """
    Yield every GBIF occurrence record for one year within a WKT geometry.

    Paginates the GBIF Occurrence Search API (`offset`/`limit`) until
    `endOfRecords` is true. Each yielded item is the raw GBIF JSON record
    (a dict) — no transformation, no filtering.

    `client` can be injected (e.g. for tests); when omitted, a short-lived
    `httpx.Client` is created and closed internally.
    """
    owns_client = client is None
    http_client = client or httpx.Client(timeout=GBIF_TIMEOUT_SECONDS)
    try:
        offset = 0
        while True:
            response = _get_with_retry(
                http_client,
                params={
                    "geometry": geometry_wkt,
                    "year": year,
                    "hasCoordinate": "true",
                    "hasGeospatialIssue": "false",
                    "limit": GBIF_PAGE_LIMIT,
                    "offset": offset,
                },
                year=year,
                offset=offset,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "GBIF API request failed",
                    extra={
                        "event": "gbif.request_error",
                        "context": {
                            "year": year,
                            "offset": offset,
                            "status_code": response.status_code,
                        },
                    },
                )
                raise GbifApiError(
                    f"GBIF request failed (year={year}, offset={offset}): {exc}"
                ) from exc

            data = response.json()
            yield from data.get("results", [])

            if data.get("endOfRecords", True):
                break
            offset += GBIF_PAGE_LIMIT
    finally:
        if owns_client:
            http_client.close()
