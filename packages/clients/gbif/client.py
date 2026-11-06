"""HTTP client for the GBIF Occurrence Download API.

No business logic here (no quality filtering, no H3, no DB) -- this module
only knows how to build a download predicate, request a download, poll its
status, and stream-parse the resulting file. See `species.quality` and
`species.gbif_occurrences` for the pipeline built on top of it.
"""

import csv
import io
import logging
import os
import tempfile
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx
from dotenv import load_dotenv
from shapely.geometry.base import BaseGeometry
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

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
    password: str = field(repr=False)

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


def geometry_to_gbif_wkt(geom: BaseGeometry) -> str:
    """
    Pure: WKT of `geom`'s convex hull, formatted the way GBIF accepts it.

    The exact polygon of a large AOI has too many vertices for GBIF, the
    convex hull is small and always contains it. GBIF also rejects polygons
    whose exterior ring is clockwise ("Polygon with clockwise exterior
    ring"), which is what Shapely's `convex_hull` returns -- so the ring is
    re-oriented counter-clockwise.
    """
    hull = geom.convex_hull
    if not isinstance(hull, Polygon):
        raise ValueError(f"GBIF search area must have a 2D extent, got {hull.geom_type}")
    return orient(hull, sign=1.0).wkt


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
            detail = response.text[:300]
            logger.error(
                "GBIF download request failed",
                extra={
                    "event": "gbif.download_request_error",
                    "context": {"status_code": response.status_code, "detail": detail},
                },
            )
            raise GbifApiError(f"GBIF download request failed: {exc} -- {detail}") from exc
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
                raise GbifApiError(f"GBIF download status check failed (key={key}): {exc}") from exc

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


def download_zip(
    key: str,
    auth: httpx.BasicAuth,
    client: httpx.Client | None = None,
) -> Path:
    """
    Download the finished GBIF archive to a temp file and return its path.

    GBIF always serves the finished download as a ZIP archive (regardless of
    the requested format) -- confirmed against the real API, not just the
    Swagger docs. The zip is streamed to disk chunk by chunk (it can be
    hundreds of MB for a large AOI/window), never held in memory. The caller
    owns the returned file and must delete it; on error here it is removed
    before the exception propagates.
    """
    owns_client = client is None
    http_client = client or httpx.Client(timeout=GBIF_TIMEOUT_SECONDS, follow_redirects=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            with http_client.stream(
                "GET", f"{GBIF_DOWNLOAD_FILE_URL}/{key}", auth=auth
            ) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise GbifApiError(
                        f"GBIF download file fetch failed (key={key}): {exc}"
                    ) from exc
                for chunk in response.iter_bytes():
                    tmp.write(chunk)
        return tmp_path
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    finally:
        if owns_client:
            http_client.close()


def iter_zip_rows(zip_path: Path, key: str = "") -> Iterator[dict]:
    """
    Yield one dict per row of the single tab-separated file inside a GBIF zip.

    The file has a header row. The member is parsed as a stream, so the
    whole CSV is never held in memory at once. Does not delete `zip_path`.
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if not names:
            raise GbifApiError(f"GBIF download {key} zip archive is empty")
        with (
            zf.open(names[0]) as raw,
            io.TextIOWrapper(raw, encoding="utf-8", newline="") as f,
        ):
            reader = csv.DictReader(f, delimiter="	")
            yield from reader


def stream_download(
    key: str,
    auth: httpx.BasicAuth,
    client: httpx.Client | None = None,
) -> Iterator[dict]:
    """
    Download the finished SIMPLE_CSV file and yield one dict per row.

    Convenience composition of `download_zip` + `iter_zip_rows`; the temp
    file is deleted once the generator is exhausted (or closed). Note the
    download only starts on the first `next()`: callers that need the file
    on disk *before* doing other work (e.g. opening a DB transaction) should
    call `download_zip` explicitly.
    """
    zip_path = download_zip(key, auth, client)
    try:
        yield from iter_zip_rows(zip_path, key)
    finally:
        zip_path.unlink(missing_ok=True)
