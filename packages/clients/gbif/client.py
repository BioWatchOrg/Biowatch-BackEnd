"""HTTP client for the GBIF Occurrence Download API.

No business logic here (no quality filtering, no H3, no DB) -- this module
only knows how to build a download predicate, request a download, poll its
status, and (see Task 13) stream-parse the resulting file. See
`species.quality` and `species.gbif_occurrences` for the pipeline built on
top of it.
"""

import logging
import os
import time
from dataclasses import dataclass
from datetime import date

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

GBIF_API_BASE = "https://api.gbif.org/v1"
GBIF_DOWNLOAD_REQUEST_URL = f"{GBIF_API_BASE}/occurrence/download/request"
GBIF_DOWNLOAD_STATUS_URL = f"{GBIF_API_BASE}/occurrence/download"
GBIF_DOWNLOAD_FILE_URL = f"{GBIF_API_BASE}/occurrence/download/request"

GBIF_TIMEOUT_SECONDS = 30.0
GBIF_POLL_INTERVAL_SECONDS = 30.0
GBIF_POLL_TIMEOUT_SECONDS = 3600.0  # 1h ceiling on waiting for a download to finish
GBIF_FAILED_STATUSES = frozenset({"FAILED", "CANCELLED", "KILLED"})


class GbifApiError(Exception):
    """Raised when the GBIF Download API returns an error response."""


class GbifDownloadTimeoutError(GbifApiError):
    """Raised when a download doesn't reach SUCCEEDED within the poll timeout."""


@dataclass(frozen=True)
class GbifCredentials:
    """GBIF Basic Auth credentials. Registration required at gbif.org --
    Basic Auth uses the *username* (not the email) and password."""

    username: str
    password: str

    @property
    def auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.username, self.password)


def load_gbif_credentials() -> GbifCredentials:
    """Load GBIF credentials from the environment (GBIF_USERNAME/GBIF_PASSWORD)."""
    load_dotenv(override=False)
    try:
        username = os.environ["GBIF_USERNAME"]
        password = os.environ["GBIF_PASSWORD"]
    except KeyError as e:
        raise RuntimeError(
            f"Variable d'environnement manquante : {e.args[0]} (voir .env.example)"
        ) from e
    return GbifCredentials(username=username, password=password)


def build_predicate(geometry_wkt: str, date_min: date, date_max: date) -> dict:
    """
    Pure: build the GBIF download predicate for one AOI + date window.

    A single predicate covers the *entire* rolling window in one request --
    no per-year looping needed (unlike the old Search API pagination this
    replaces), since the Download API has no offset cap.
    """
    return {
        "type": "and",
        "predicates": [
            {"type": "within", "geometry": geometry_wkt},
            {"type": "greaterThanOrEquals", "key": "EVENT_DATE", "value": date_min.isoformat()},
            {"type": "lessThanOrEquals", "key": "EVENT_DATE", "value": date_max.isoformat()},
            {"type": "equals", "key": "HAS_COORDINATE", "value": "true"},
            {"type": "equals", "key": "HAS_GEOSPATIAL_ISSUE", "value": "false"},
        ],
    }


def request_download(
    predicate: dict,
    creator: str,
    auth: httpx.BasicAuth,
    client: httpx.Client | None = None,
) -> str:
    """POST a download request, return the GBIF download key."""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=GBIF_TIMEOUT_SECONDS)
    try:
        response = http_client.post(
            GBIF_DOWNLOAD_REQUEST_URL,
            json={"creator": creator, "format": "SIMPLE_CSV", "predicate": predicate},
            auth=auth,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "GBIF download request failed",
                extra={
                    "event": "gbif.download_request_error",
                    "context": {"status_code": response.status_code},
                },
            )
            raise GbifApiError(f"GBIF download request failed: {exc}") from exc
        return response.text.strip()
    finally:
        if owns_client:
            http_client.close()


def wait_for_download(
    key: str,
    auth: httpx.BasicAuth,
    client: httpx.Client | None = None,
) -> None:
    """Poll download status until SUCCEEDED, or raise on failure/timeout."""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=GBIF_TIMEOUT_SECONDS)
    started = time.monotonic()
    try:
        while True:
            response = http_client.get(f"{GBIF_DOWNLOAD_STATUS_URL}/{key}", auth=auth)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise GbifApiError(
                    f"GBIF download status check failed (key={key}): {exc}"
                ) from exc

            status = response.json().get("status")
            if status == "SUCCEEDED":
                return
            if status in GBIF_FAILED_STATUSES:
                raise GbifApiError(f"GBIF download {key} ended in status {status}")
            if time.monotonic() - started > GBIF_POLL_TIMEOUT_SECONDS:
                raise GbifDownloadTimeoutError(
                    f"GBIF download {key} did not succeed within "
                    f"{GBIF_POLL_TIMEOUT_SECONDS}s (last status: {status})"
                )
            logger.info(
                "GBIF download in progress",
                extra={"event": "gbif.download_polling", "context": {"key": key, "status": status}},
            )
            time.sleep(GBIF_POLL_INTERVAL_SECONDS)
    finally:
        if owns_client:
            http_client.close()
