import enum
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
