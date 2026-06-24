CREATE TABLE job_runs (
    run_id UUID PRIMARY KEY,
    job_name TEXT NOT NULL,
    scope TEXT NOT NULL,
    bucket_id TEXT,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    idempotency_key TEXT NOT NULL UNIQUE,
    error_message TEXT,
    output_ref TEXT
);

CREATE TABLE job_run_zone_errors (
    run_id UUID NOT NULL,
    zone_id TEXT NOT NULL,
    error_message TEXT,
    failed_at TIMESTAMPTZ NOT NULL,

    CONSTRAINT fk_job_run_zone_errors_run
        FOREIGN KEY (run_id)
        REFERENCES job_runs(run_id),

    CONSTRAINT uq_job_run_zone_error
        UNIQUE (
            run_id,
            zone_id
        )
);

CREATE INDEX idx_job_runs_status
ON job_runs (status);

CREATE INDEX idx_job_runs_bucket_id
ON job_runs (bucket_id);

CREATE INDEX idx_job_run_zone_errors_run_id
ON job_run_zone_errors (run_id);

CREATE INDEX idx_job_run_zone_errors_zone_id
ON job_run_zone_errors (zone_id);