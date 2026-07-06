import enum
import re
from datetime import date, datetime
from typing import Any, TypeAlias
from zoneinfo import ZoneInfo


class UnsupportedBucketFormat(Exception):
    """Exception raised when an unsupported bucket format is provided."""


class UnsupportedDateType(Exception):
    """Exception raised when an unsupported date type is provided."""


class InvalidDateString(ValueError):
    """Exception raised when a string cannot be parsed as an ISO 8601 date."""


def biowatch_now() -> date:
    """
    Returns the current date in the Europe/Paris timezone.
    """
    return datetime.now(tz=ZoneInfo("Europe/Paris")).date()


BucketId: TypeAlias = str


class BucketFormat(enum.Enum):
    """Enum for bucket formats."""

    MONTHLY = "monthly"
    BIMONTHLY = "bimonthly"
    YEARLY = "yearly"


def _coerce_to_date(value: Any) -> date:
    match value:
        case None:
            return biowatch_now()
        case str():
            try:
                return datetime.fromisoformat(value).date()
            except ValueError as exc:
                raise InvalidDateString(f"Invalid ISO 8601 date string: {value!r}.") from exc
        case datetime():
            return value.date()
        case date():
            return value
        case _:
            raise UnsupportedDateType(
                f"Unsupported type for bucket ID: {type(value)}. "
                "Expected None, str, datetime, or date."
            )


def _format_to_str(dt: date, format: BucketFormat) -> str:
    if format == BucketFormat.MONTHLY:
        return dt.strftime("%Y-%m")
    elif format == BucketFormat.BIMONTHLY:
        bimester = (dt.month - 1) // 2 + 1
        return "{}-b{:02d}".format(dt.year, bimester)
    elif format == BucketFormat.YEARLY:
        return dt.strftime("%Y")
    else:
        raise UnsupportedBucketFormat(f"Unsupported bucket format: {format}")


def bucket_id(
    value: datetime | date | str | None = None,
    format: BucketFormat = BucketFormat.MONTHLY,
) -> BucketId:
    """
    Return the bucket ID for a given date and format.

    `value` accepts a date, a datetime, an ISO 8601 string, or None
    (falls back to the current date in the project timezone).

    Returns:
        'YYYY'      for yearly buckets,
        'YYYY-MM'   for monthly buckets,
        'YYYY-bNN'  for bimonthly buckets (NN = bimester number, 01..06).
    """
    resolved_date = _coerce_to_date(value)

    return _format_to_str(resolved_date, format)


_YEARLY_RE = re.compile(r"^(\d{4})$")
_MONTHLY_RE = re.compile(r"^(\d{4})-(\d{2})$")
_BIMONTHLY_RE = re.compile(r"^(\d{4})-b(\d{2})$")


def _parse_bucket_id(bucket: BucketId) -> tuple[BucketFormat, int, int | None]:
    """
    Inverse of `bucket_id`: parse a bucket string into (format, year, index).

    `index` is the month (1..12) for MONTHLY, the bimester (1..6) for BIMONTHLY,
    and None for YEARLY. Validates content, not just shape:
    raises UnsupportedBucketFormat on any malformed or out-of-range input.
    """
    if m := _BIMONTHLY_RE.match(bucket):
        bimester = int(m.group(2))
        if not 1 <= bimester <= 6:
            raise UnsupportedBucketFormat(f"Invalid bimester in bucket ID: {bucket!r}")
        return BucketFormat.BIMONTHLY, int(m.group(1)), bimester
    if m := _MONTHLY_RE.match(bucket):
        month = int(m.group(2))
        if not 1 <= month <= 12:
            raise UnsupportedBucketFormat(f"Invalid month in bucket ID: {bucket!r}")
        return BucketFormat.MONTHLY, int(m.group(1)), month
    if m := _YEARLY_RE.match(bucket):
        return BucketFormat.YEARLY, int(m.group(1)), None
    raise UnsupportedBucketFormat(f"Invalid bucket ID format: {bucket!r}")


def related_bucket_ids(bucket: BucketId) -> list[BucketId]:
    """
    Return every bucket, in any format, that covers the same period as `bucket`.

        - YEARLY    -> the year + its 6 bimesters + its 12 months.
        - BIMONTHLY -> the bimester + its 2 months + its year.
        - MONTHLY   -> the month + its bimester + its year.
    """
    format, year, index = _parse_bucket_id(bucket)
    year_id = f"{year}"

    if format is BucketFormat.YEARLY:
        bimesters = [f"{year}-b{b:02d}" for b in range(1, 7)]
        months = [f"{year}-{m:02d}" for m in range(1, 13)]
        return [bucket, *bimesters, *months]

    if format is BucketFormat.BIMONTHLY:
        assert index is not None
        start = (index - 1) * 2 + 1
        months = [f"{year}-{start:02d}", f"{year}-{start + 1:02d}"]
        return [bucket, *months, year_id]

    # MONTHLY: no descendants, ancestors are its bimester then its year.
    assert index is not None
    bimester = (index - 1) // 2 + 1
    return [bucket, f"{year}-b{bimester:02d}", year_id]
