CREATE TABLE species_features_by_zone (
    zone_id TEXT NOT NULL,
    period_year INT NOT NULL,
    species_total_count INT,
    species_cr_count INT,
    species_en_count INT,
    species_vu_count INT,
    species_nt_count INT,
    vulnerability_weighted_score FLOAT,
    source_version TEXT NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_species_zone
        FOREIGN KEY (zone_id)
        REFERENCES zones_hex(zone_id),

    CONSTRAINT uq_species_zone_period_version
        UNIQUE (
            zone_id,
            period_year,
            source_version
        )
);

CREATE INDEX idx_species_features_period_year
ON species_features_by_zone (period_year);

CREATE INDEX idx_species_features_zone_id
ON species_features_by_zone (zone_id);
