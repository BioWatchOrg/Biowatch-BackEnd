CREATE TABLE protected_areas_by_zone (
    zone_id TEXT NOT NULL,
    protected_area_type TEXT NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE,
    coverage_ratio FLOAT,
    source_version TEXT NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_protected_area_zone
        FOREIGN KEY (zone_id)
        REFERENCES zones_hex(zone_id),

    CONSTRAINT uq_protected_area_zone_type_validity_version
        UNIQUE (
            zone_id,
            protected_area_type,
            valid_from,
            source_version
        )
);

CREATE INDEX idx_protected_areas_type
ON protected_areas_by_zone (protected_area_type);

CREATE INDEX idx_protected_areas_zone_id
ON protected_areas_by_zone (zone_id);