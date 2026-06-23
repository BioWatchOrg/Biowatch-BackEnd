CREATE TABLE species_features_by_zone (
    zone_id TEXT NOT NULL,
    period DATE NOT NULL,
    species_total_count INT,
    source_version TEXT NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_species_zone
        FOREIGN KEY (zone_id)
        REFERENCES zones_hex(zone_id),

    CONSTRAINT uq_species_zone_period_version
        UNIQUE (
            zone_id,
            period,
            source_version
        )
);

CREATE INDEX idx_species_features_period
ON species_features_by_zone (period);

CREATE INDEX idx_species_features_zone_id
ON species_features_by_zone (zone_id);