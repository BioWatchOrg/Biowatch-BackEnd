"""
Geofabrik client: resolve the OSM `.pbf` snapshots of an AOI and download them.

Geofabrik keeps a dated snapshot of every French region on January 1st of each
year since 2014 (`ile-de-france-160101.osm.pbf`). With the annual `bucket_id`
used by the OSM pipeline, bucket `2016` maps to the `160101` snapshot, which
makes the source version deterministic (`geofabrik_2016-01-01`).

Geofabrik still splits France along the pre-2016 regions: a current region may
span several extracts (Grand Est = Alsace + Champagne-Ardenne + Lorraine).
osm2pgsql accepts several input files and merges them, so an AOI simply maps to
a list of extracts.
"""

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

GEOFABRIK_FRANCE_URL = "https://download.geofabrik.de/europe/france"

# First year with a January 1st snapshot on the Geofabrik download server.
FIRST_SNAPSHOT_YEAR = 2014

# AOI label (packages/core/aoi/aoi_registry.json) → Geofabrik extracts covering it.
# Order matters: it is the order in which the scheduler walks the regions.
GEOFABRIK_REGIONS: dict[str, tuple[str, ...]] = {
    "idf": ("ile-de-france",),
    "ara": ("auvergne", "rhone-alpes"),
    "bfc": ("bourgogne", "franche-comte"),
    "bre": ("bretagne",),
    "cvl": ("centre",),
    "ges": ("alsace", "champagne-ardenne", "lorraine"),
    "hdf": ("nord-pas-de-calais", "picardie"),
    "nor": ("basse-normandie", "haute-normandie"),
    "naq": ("aquitaine", "limousin", "poitou-charentes"),
    "occ": ("languedoc-roussillon", "midi-pyrenees"),
    "pdl": ("pays-de-la-loire",),
    "pac": ("provence-alpes-cote-d-azur",),
}

_YEAR_RE = re.compile(r"^\d{4}$")
_CHUNK_SIZE = 1024 * 1024


class GeofabrikError(Exception):
    """Raised when a snapshot cannot be resolved or downloaded."""


@dataclass(frozen=True)
class Snapshot:
    """The extracts to import for one AOI × year, and their source version."""

    aoi_label: str
    year: int
    urls: tuple[str, ...]
    source_version: str


def snapshot_for(aoi_label: str, bucket: str) -> Snapshot:
    """
    Resolve the January 1st snapshot(s) of an AOI for an annual bucket (`"2016"`).

    Pure: builds URLs, does not check that they exist (the download does).
    """
    regions = GEOFABRIK_REGIONS.get(aoi_label)
    if regions is None:
        raise GeofabrikError(f"No Geofabrik extract mapped for AOI '{aoi_label}'.")
    if not _YEAR_RE.match(bucket):
        raise GeofabrikError(f"Bucket must be an annual bucket (YYYY), got '{bucket}'.")
    year = int(bucket)
    if year < FIRST_SNAPSHOT_YEAR:
        raise GeofabrikError(
            f"No Geofabrik snapshot before {FIRST_SNAPSHOT_YEAR} (bucket '{bucket}')."
        )
    stamp = f"{year % 100:02d}0101"
    urls = tuple(f"{GEOFABRIK_FRANCE_URL}/{region}-{stamp}.osm.pbf" for region in regions)
    return Snapshot(
        aoi_label=aoi_label,
        year=year,
        urls=urls,
        source_version=f"geofabrik_{year}-01-01",
    )


def snapshot_from_urls(aoi_label: str, bucket: str, urls: list[str]) -> Snapshot:
    """
    Snapshot built from explicit URLs (CLI `--link`), bypassing the AOI mapping.

    The source version is derived from the file names so that the same links
    always yield the same idempotency key.
    """
    if not urls:
        raise GeofabrikError("At least one URL is required.")
    if not _YEAR_RE.match(bucket):
        raise GeofabrikError(f"Bucket must be an annual bucket (YYYY), got '{bucket}'.")
    names = [os.path.basename(u).removesuffix(".osm.pbf") for u in urls]
    return Snapshot(
        aoi_label=aoi_label,
        year=int(bucket),
        urls=tuple(urls),
        source_version="custom_" + "+".join(names),
    )


def _fetch_md5(client: httpx.Client, url: str) -> str | None:
    """Expected md5 published next to the file, or None (old snapshots have none)."""
    response = client.get(f"{url}.md5")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.text.split()[0].strip().lower()


def download(url: str, dest_dir: Path, client: httpx.Client | None = None) -> Path:
    """
    Download `url` into `dest_dir` and return the local path.

    Integrity: the md5 published by Geofabrik is checked when it exists,
    otherwise the size is checked against `Content-Length`. The file is written
    to `<name>.part` and renamed only once verified, so a crash never leaves a
    truncated file that looks complete.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / os.path.basename(url)
    partial = target.with_name(target.name + ".part")
    own_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(60.0), follow_redirects=True)
    log_ctx = {"url": url, "path": str(target)}
    try:
        expected_md5 = _fetch_md5(http, url)
        digest = hashlib.md5()
        size = 0
        logger.info(
            "downloading OSM snapshot",
            extra={"event": "geofabrik.download.start", "context": log_ctx},
        )
        with http.stream("GET", url) as response:
            if response.status_code == 404:
                raise GeofabrikError(f"Snapshot not found on Geofabrik: {url}")
            response.raise_for_status()
            expected_size = response.headers.get("Content-Length")
            with open(partial, "wb") as f:
                for chunk in response.iter_bytes(_CHUNK_SIZE):
                    f.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)

        if expected_md5 is not None and digest.hexdigest() != expected_md5:
            raise GeofabrikError(f"md5 mismatch for {url}")
        if expected_md5 is None and expected_size is not None and size != int(expected_size):
            raise GeofabrikError(f"Size mismatch for {url}: got {size}, expected {expected_size}")

        partial.replace(target)
        logger.info(
            "downloaded OSM snapshot",
            extra={
                "event": "geofabrik.download.done",
                "context": {**log_ctx, "bytes": size, "md5_checked": expected_md5 is not None},
            },
        )
        return target
    except httpx.HTTPError as e:
        raise GeofabrikError(f"Download failed for {url}: {e}") from e
    finally:
        partial.unlink(missing_ok=True)
        if own_client:
            http.close()
