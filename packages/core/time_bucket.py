import enum
from datetime import date, datetime
from typing import Any, TypeAlias
from zoneinfo import ZoneInfo


class UnsupportedBucketFormat(Exception):
    """Exception raised when an unsupported bucket format is provided."""


class UnsupportedDateType(Exception):
    """Exception raised when an unsupported date type is provided."""


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


def _coerce_to_date(dt: Any) -> date:
    match dt:
        case str():
            return datetime.fromisoformat(dt).date()
        case datetime():
            return dt.date()
        case date():
            return dt
        case _:
            raise UnsupportedDateType(
                f"Unsupported type for bucket ID: {type(dt)}. Expected str, datetime, or date."
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


def bucket_id(date: Any, format: BucketFormat = BucketFormat.MONTHLY) -> BucketId:
    """
    Returns the bucket ID for a given datetime and format.

    returns :
        'YYYY' for yearly buckets,
        'YYYY-MM' for monthly buckets,
        'YYYY-bB' for bimonthly buckets (where B is the bimester
    """
    date_formated = _coerce_to_date(date)

    return _format_to_str(date_formated, format)
