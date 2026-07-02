"""
Ready-made helpers around the `job_runs` / `job_run_zone_errors` tables.

Implements the idempotence contract from CLAUDE.md:
- a deterministic idempotency_key identifies a unit of work;
- a run already in `success` is skipped (raises `JobAlreadySucceeded`);
- a run in `failed` / `partial` / `running` is retried (reused);
- normal completion marks `success`, or `partial` if per-zone errors were
  recorded during the block; an exception marks `failed`, records the error and
  re-raises;
- partial per-zone failures go to `job_run_zone_errors` for targeted retry.
"""

import datetime
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from .models import JobRun, JobRunZoneError
from .session import get_sessionmaker, session_scope

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = ("failed", "partial", "running")


class JobAlreadySucceeded(Exception):
    """Raised by `job_run` when a run with the same idempotency_key already succeeded."""

    def __init__(self, idempotency_key: str, run_id: uuid.UUID):
        self.idempotency_key = idempotency_key
        self.run_id = run_id
        super().__init__(f"Job already succeeded for idempotency_key={idempotency_key}")


@dataclass
class RunContext:
    """Handle on the active run, yielded by `job_run`."""

    run_id: uuid.UUID
    job_name: str
    scope: str
    idempotency_key: str
    bucket_id: str | None = None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _start_run(
    job_name: str,
    scope: str,
    idempotency_key: str,
    bucket_id: str | None,
    engine: Engine | None,
) -> RunContext:
    """Create a new run, or reuse a retryable one. Raise if already succeeded."""
    with session_scope(engine) as session:
        existing = session.scalar(select(JobRun).where(JobRun.idempotency_key == idempotency_key))

        if existing is not None:
            if existing.status == "success":
                raise JobAlreadySucceeded(idempotency_key, existing.run_id)
            # Retry: reset the existing run in place (failed / partial / running).
            existing.status = "running"
            existing.started_at = _now()
            existing.ended_at = None
            existing.error_message = None
            run_id = existing.run_id
            # Purge the previous attempt's per-zone errors so the new attempt's
            # success/partial verdict reflects only this run.
            session.execute(delete(JobRunZoneError).where(JobRunZoneError.run_id == run_id))
        else:
            run_id = uuid.uuid4()
            session.add(
                JobRun(
                    run_id=run_id,
                    job_name=job_name,
                    scope=scope,
                    bucket_id=bucket_id,
                    status="running",
                    started_at=_now(),
                    idempotency_key=idempotency_key,
                )
            )

    return RunContext(
        run_id=run_id,
        job_name=job_name,
        scope=scope,
        idempotency_key=idempotency_key,
        bucket_id=bucket_id,
    )


def _count_zone_errors(run_id: uuid.UUID, engine: Engine | None) -> int:
    with session_scope(engine) as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(JobRunZoneError)
                .where(JobRunZoneError.run_id == run_id)
            )
            or 0
        )


def _finish_run(run_id: uuid.UUID, status: str, error_message: str | None, engine: Engine | None):
    with session_scope(engine) as session:
        run = session.get(JobRun, run_id)
        if run is None:  # pragma: no cover - defensive
            return
        run.status = status
        run.ended_at = _now()
        run.error_message = error_message


@contextmanager
def job_run(
    job_name: str,
    scope: str,
    idempotency_key: str,
    bucket_id: str | None = None,
    engine: Engine | None = None,
) -> Iterator[tuple[RunContext, Session]]:
    """
    Wrap a job body with run traceability, idempotence and a managed session.

        try:
            with job_run("satellite_ingest", scope="idf", idempotency_key=key) as (run, session):
                upsert(session, SatelliteFeaturesByZone, rows)  # committed at clean exit
        except JobAlreadySucceeded:
            return  # nothing to do, a previous run already succeeded

    The yielded `session` is the transaction for the job's data writes:
    - clean exit → the session is **committed**, then the run is marked `success`
      (or `partial` if a per-zone error was recorded via `record_zone_error`);
    - exception → the session is **rolled back** (data reverted), then the run is
      marked `failed` with the error message and the exception is re-raised.

    The run status is written in a **separate** transaction, so it is persisted
    even when the data session rolls back. Both `partial` and `failed` runs are
    retryable (see `get_failed_zones` for targeted retry).
    """
    run = _start_run(job_name, scope, idempotency_key, bucket_id, engine)
    logger.info(
        "CLIENTS-DB-job_run : run started",
        extra={"run_id": str(run.run_id), "idempotency_key": idempotency_key, "event": "job.start"},
    )
    started = _now()
    session = get_sessionmaker(engine)()
    try:
        yield run, session
        session.commit()
    except Exception as exc:
        session.rollback()
        _finish_run(run.run_id, "failed", str(exc), engine)
        logger.error(
            "CLIENTS-DB-job_run : run failed",
            extra={
                "run_id": str(run.run_id),
                "idempotency_key": idempotency_key,
                "event": "job.failed",
                "duration_ms": int((_now() - started).total_seconds() * 1000),
            },
        )
        raise
    else:
        # Per-zone errors recorded during the block downgrade success to partial.
        status = "partial" if _count_zone_errors(run.run_id, engine) else "success"
        _finish_run(run.run_id, status, None, engine)
        logger.info(
            "CLIENTS-DB-job_run : run %s" % status,
            extra={
                "run_id": str(run.run_id),
                "idempotency_key": idempotency_key,
                "event": "job.%s" % status,
                "duration_ms": int((_now() - started).total_seconds() * 1000),
            },
        )
    finally:
        session.close()


def record_zone_error(
    run_id: uuid.UUID,
    zone_id: str,
    error_message: str,
    engine: Engine | None = None,
) -> None:
    """
    Record a per-zone failure for the run, enabling targeted retry.

    Idempotent on (run_id, zone_id): re-recording the same zone updates the
    message instead of duplicating the row.
    """
    with session_scope(engine) as session:
        session.merge(
            JobRunZoneError(
                run_id=run_id,
                zone_id=zone_id,
                error_message=error_message,
                failed_at=_now(),
            )
        )
    logger.warning(
        "CLIENTS-DB-record_zone_error : zone failed",
        extra={"run_id": str(run_id), "zone_id": zone_id, "event": "job.zone_error"},
    )


def get_failed_zones(idempotency_key: str, engine: Engine | None = None) -> list[str]:
    """
    Return the zone_ids that failed on the last run for this idempotency_key.

    Use this for targeted retry: read the failed zones BEFORE re-entering
    `job_run` (a retry purges the previous attempt's per-zone errors). Returns an
    empty list if no run exists yet or none of its zones failed.
    """
    with session_scope(engine) as session:
        run = session.scalar(select(JobRun).where(JobRun.idempotency_key == idempotency_key))
        if run is None:
            return []
        return list(
            session.scalars(
                select(JobRunZoneError.zone_id).where(JobRunZoneError.run_id == run.run_id)
            ).all()
        )
