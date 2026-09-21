"""Pure quality filtering for raw GBIF occurrence records.

No I/O, no DB, no H3 — this module only decides whether a raw GBIF JSON
record is good enough to keep, and normalizes it into the shape expected by
`staging.species_occurrences_raw` (minus `zone_id` / `aoi_id`, added by the
orchestrating job once the point is placed on the H3 grid).
"""

from datetime import date, datetime

_LAT_RANGE = (-90.0, 90.0)
_LON_RANGE = (-180.0, 180.0)


def _parse_event_date(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _is_valid_coordinate(lat: object, lon: object) -> bool:
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return False
    if lat == 0 and lon == 0:
        return False
    return _LAT_RANGE[0] <= lat <= _LAT_RANGE[1] and _LON_RANGE[0] <= lon <= _LON_RANGE[1]


def quality_filter(record: dict, date_min: date, date_max: date) -> dict | None:
    """
    Validate and normalize one raw GBIF occurrence record.

    Returns None if the record fails any quality check:
    - missing GBIF `key` or `scientificName`;
    - missing/invalid/out-of-range coordinates (including the (0, 0) "null
      island" marker some datasets use for missing data);
    - missing or unparseable `eventDate`, or a date outside
      `[date_min, date_max]`.

    Returns a dict ready for `staging.species_occurrences_raw` (without
    `zone_id`/`aoi_id`) otherwise.
    """
    key = record.get("key")
    scientific_name = record.get("scientificName")
    if key is None or not scientific_name:
        return None

    lat = record.get("decimalLatitude")
    lon = record.get("decimalLongitude")
    if not _is_valid_coordinate(lat, lon):
        return None

    event_date = _parse_event_date(record.get("eventDate"))
    if event_date is None or not (date_min <= event_date <= date_max):
        return None

    return {
        "gbif_occurrence_id": str(key),
        "event_date": event_date,
        "gbif_taxon_key": record.get("taxonKey"),
        "scientific_name": scientific_name,
        "decimal_latitude": float(lat),
        "decimal_longitude": float(lon),
    }
