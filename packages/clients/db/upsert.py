from collections.abc import Sequence
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .base import Base

# PostgreSQL caps a single statement at 65535 bind parameters. Each row uses one
# parameter per column, so we chunk rows to stay under the limit (with margin).
_MAX_BIND_PARAMS = 65535


def upsert(
    session: Session,
    model: type[Base],
    rows: Sequence[dict[str, Any]],
    update_columns: Sequence[str] | None = None,
) -> None:
    """
    Idempotent bulk INSERT ... ON CONFLICT DO UPDATE for an ORM model.

    Conflicts are resolved on the model's primary key (which, for the feature and
    score tables, is the natural business key — e.g. zone_id + bucket_id +
    source_version). Re-running a job with the same rows updates in place instead
    of creating duplicates.

    Args:
        session: an open session (typically from `session_scope`).
        model: the ORM model class to write into.
        rows: list of dicts, one per row (keys = column names).
        update_columns: columns to overwrite on conflict. Defaults to every
            non-primary-key column present, so the row is fully refreshed.

    Large row sets are automatically split into batches so a single statement
    never exceeds PostgreSQL's 65535-parameter limit.

    Returns nothing: the affected-row count is unreliable with ON CONFLICT on
    PostgreSQL (psycopg often reports -1), so it is intentionally not exposed.
    """
    if not rows:
        return

    mapper = inspect(model)
    pk_columns = [col.name for col in mapper.primary_key]

    if update_columns is None:
        # Refresh all non-PK columns that appear in the incoming rows.
        provided = {key for row in rows for key in row}
        update_columns = [
            col.name
            for col in mapper.columns
            if col.name not in pk_columns and col.name in provided
        ]

    # One bind parameter per column per row → cap rows so params stay under limit.
    params_per_row = max(len(row) for row in rows)
    batch_size = max(1, _MAX_BIND_PARAMS // params_per_row)

    for start in range(0, len(rows), batch_size):
        batch = list(rows[start : start + batch_size])
        stmt = insert(model).values(batch)
        if update_columns:
            stmt = stmt.on_conflict_do_update(
                index_elements=pk_columns,
                set_={col: stmt.excluded[col] for col in update_columns},
            )
        else:
            # Nothing to update (rows carry only PK columns) → ignore duplicates.
            stmt = stmt.on_conflict_do_nothing(index_elements=pk_columns)
        session.execute(stmt)
