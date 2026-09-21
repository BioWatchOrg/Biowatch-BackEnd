"""HTTP client for the GBIF Occurrence Search API.

No business logic here (no quality filtering, no H3, no DB) — this module
only knows how to talk to GBIF and paginate. See `species.quality` and
`species.gbif_occurrences` for the pipeline built on top of it.
"""

import logging
from collections.abc import Iterator

import httpx

logger = logging.getLogger(__name__)

GBIF_SEARCH_URL = "https://api.gbif.org/v1/occurrence/search"
GBIF_PAGE_LIMIT = 300
GBIF_TIMEOUT_SECONDS = 30.0


class GbifApiError(Exception):
    """Raised when the GBIF Occurrence Search API returns an error response."""


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
            response = http_client.get(
                GBIF_SEARCH_URL,
                params={
                    "geometry": geometry_wkt,
                    "year": year,
                    "hasCoordinate": "true",
                    "hasGeospatialIssue": "false",
                    "limit": GBIF_PAGE_LIMIT,
                    "offset": offset,
                },
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
