import hashlib
import json


def compute_idempotency_key(
    job_name: str,
    scope: str,
    source_version: str,
    period: str | None = None,
    resolution: int | None = None,
    env: str | None = None,
) -> str:
    """
    Build a deterministic idempotency key for a job run.

    Same (job, scope, period, source_version[, env]) always yields the same key,
    so a re-run of the exact same work can be detected and skipped. Different
    inputs yield a different key. The key is a SHA-256 hex digest.

    Args:
        job_name: logical job identifier (e.g. "satellite_ingest").
        scope: what the run covers (e.g. an AOI label like "idf").
        period: normalized time bucket (e.g. "2024-Q1" or a bucket_id).
        source_version: version of the source/computation for reproducibility.
        env: optional environment discriminator (e.g. "dev" / "prod").
    """
    payload = {
        "job_name": job_name,
        "scope": scope,
        "period": period,
        "source_version": source_version,
        "resolution": resolution,
        "env": env,
    }
    # sort_keys makes the serialization stable regardless of dict insertion order.
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
