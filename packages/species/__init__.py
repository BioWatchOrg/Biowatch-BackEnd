"""Species/biodiversity business logic (GBIF ingestion, future feature aggregation)."""

from .gbif_occurrences import update_gbif_occurrences

__all__ = ["update_gbif_occurrences"]
