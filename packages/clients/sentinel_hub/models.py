from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ZoneStatistics:
    """
    Aggregated Sentinel-2 L2A indices and quality metrics for one zone over one
    time range, as returned by the Sentinel Hub Statistical API.

    `*_mean` fields are `None` when the Statistical API has no pixel to average
    (e.g. a zone fully masked out by clouds/snow for the whole period) — the
    caller decides how to handle that (typically: record a zone error instead
    of upserting a null-heavy row).
    """

    ndvi_mean: float | None
    ndwi_mean: float | None
    ndbi_mean: float | None
    swir_mean: float | None
    obs_count: int
    valid_pixel_ratio: float
    cloud_score: float


@dataclass(frozen=True, slots=True)
class _CachedToken:
    """Internal: an access token paired with its expiry, on the monotonic clock."""

    access_token: str
    expires_at: float
