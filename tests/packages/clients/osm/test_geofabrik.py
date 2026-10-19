"""Tests for clients.osm.geofabrik — snapshot resolution and verified download."""

import hashlib

import httpx
import pytest

from clients.osm.geofabrik import (
    FIRST_SNAPSHOT_YEAR,
    GEOFABRIK_REGIONS,
    GeofabrikError,
    download,
    snapshot_for,
    snapshot_from_urls,
)
from core import aoi_registry

URL = "https://download.geofabrik.de/europe/france/ile-de-france-160101.osm.pbf"
PAYLOAD = b"fake-pbf-content" * 1000


def test_snapshot_for_single_extract_region():
    snap = snapshot_for("idf", "2016")
    assert snap.urls == (URL,)
    assert snap.year == 2016
    assert snap.source_version == "geofabrik_2016-01-01"


def test_snapshot_for_region_spanning_several_extracts():
    snap = snapshot_for("ges", "2024")
    assert [u.rsplit("/", 1)[1] for u in snap.urls] == [
        "alsace-240101.osm.pbf",
        "champagne-ardenne-240101.osm.pbf",
        "lorraine-240101.osm.pbf",
    ]


def test_snapshot_for_is_deterministic():
    assert snapshot_for("idf", "2020") == snapshot_for("idf", "2020")


@pytest.mark.parametrize("bucket", ["2016-01", "16", "abcd", "2016-b01"])
def test_snapshot_for_requires_annual_bucket(bucket):
    with pytest.raises(GeofabrikError, match="annual"):
        snapshot_for("idf", bucket)


def test_snapshot_for_rejects_years_before_first_snapshot():
    with pytest.raises(GeofabrikError, match=str(FIRST_SNAPSHOT_YEAR)):
        snapshot_for("idf", str(FIRST_SNAPSHOT_YEAR - 1))


def test_snapshot_for_rejects_unmapped_aoi():
    with pytest.raises(GeofabrikError, match="No Geofabrik extract"):
        snapshot_for("corse", "2016")


def test_every_registered_aoi_is_mapped():
    """Registry AOIs must be importable — a new AOI needs its Geofabrik mapping."""
    from core.aoi import aoi_data

    for entry in aoi_data["aois"]:
        assert entry["label"] in GEOFABRIK_REGIONS
    assert aoi_registry("idf")


def test_snapshot_from_urls_derives_source_version_from_names():
    snap = snapshot_from_urls("idf", "2016", [URL, "https://x/y/bretagne-160101.osm.pbf"])
    assert snap.source_version == "custom_ile-de-france-160101+bretagne-160101"
    assert snap.urls == (URL, "https://x/y/bretagne-160101.osm.pbf")


def test_snapshot_from_urls_validates_inputs():
    with pytest.raises(GeofabrikError):
        snapshot_from_urls("idf", "2016", [])
    with pytest.raises(GeofabrikError):
        snapshot_from_urls("idf", "2016-01", [URL])


def _client(md5: str | None, payload: bytes = PAYLOAD, status: int = 200, length=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".md5"):
            if md5 is None:
                return httpx.Response(404)
            return httpx.Response(200, text=f"{md5}  ile-de-france-160101.osm.pbf\n")
        headers = {"Content-Length": str(length if length is not None else len(payload))}
        return httpx.Response(status, content=payload, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_verifies_md5_when_published(tmp_path):
    md5 = hashlib.md5(PAYLOAD).hexdigest()
    path = download(URL, tmp_path, client=_client(md5))
    assert path == tmp_path / "ile-de-france-160101.osm.pbf"
    assert path.read_bytes() == PAYLOAD
    assert not list(tmp_path.glob("*.part"))


def test_download_rejects_md5_mismatch_and_leaves_no_file(tmp_path):
    with pytest.raises(GeofabrikError, match="md5"):
        download(URL, tmp_path, client=_client("0" * 32))
    assert list(tmp_path.iterdir()) == []


def test_download_falls_back_to_size_check_without_md5(tmp_path):
    path = download(URL, tmp_path, client=_client(None))
    assert path.read_bytes() == PAYLOAD


def test_download_rejects_size_mismatch_without_md5(tmp_path):
    with pytest.raises(GeofabrikError, match="Size mismatch"):
        download(URL, tmp_path, client=_client(None, length=len(PAYLOAD) + 1))
    assert list(tmp_path.iterdir()) == []


def test_download_reports_missing_snapshot(tmp_path):
    with pytest.raises(GeofabrikError, match="not found"):
        download(URL, tmp_path, client=_client(None, status=404))


def test_download_wraps_http_errors(tmp_path):
    with pytest.raises(GeofabrikError, match="Download failed"):
        download(URL, tmp_path, client=_client(None, status=500))
