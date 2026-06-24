CREATE TABLE stress_score_by_zone (
    zone_id TEXT NOT NULL,
    bucket_id TEXT NOT NULL,
    score_global FLOAT,

    score_human_pressure FLOAT,
    score_vegetation FLOAT,
    score_biodiversity FLOAT,
    score_dynamic FLOAT,
    score_protection FLOAT,

    score_method TEXT NOT NULL,
    run_id TEXT,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_stress_score_zone
        FOREIGN KEY (zone_id)
        REFERENCES zones_hex(zone_id),

    CONSTRAINT uq_stress_score_zone_bucket_method
        UNIQUE (
            zone_id,
            bucket_id,
            score_method
        )
);

CREATE INDEX idx_stress_score_zone_id
ON stress_score_by_zone (zone_id);

CREATE INDEX idx_stress_score_bucket_id
ON stress_score_by_zone (bucket_id);

CREATE INDEX idx_stress_score_method
ON stress_score_by_zone (score_method);