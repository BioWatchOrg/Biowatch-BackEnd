CREATE TABLE satellite_features_by_zone (
    zone_id TEXT NOT NULL,
    bucket_id TEXT NOT NULL,
    ndvi FLOAT,
    ndwi FLOAT,
    ndbi FLOAT,
    swir FLOAT,
    obs_count INT,
    valid_pixel_ratio FLOAT,
    cloud_score FLOAT,
    source_version TEXT NOT NULL,
    is_valid_data BOOLEAN NOT NULL DEFAULT TRUE,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_satellite_zone
        FOREIGN KEY (zone_id)
        REFERENCES zones_hex(zone_id),

    CONSTRAINT uq_satellite_zone_bucket_version
        UNIQUE (zone_id, bucket_id, source_version)
);

CREATE INDEX idx_satellite_features_bucket_id
ON satellite_features_by_zone (bucket_id);

CREATE INDEX idx_satellite_features_zone_id
ON satellite_features_by_zone (zone_id);