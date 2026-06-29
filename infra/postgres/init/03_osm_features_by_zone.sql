CREATE TABLE osm_features_by_zone (
    zone_id TEXT NOT NULL,
    bucket_id TEXT NOT NULL,
    building_area_ratio FLOAT,
    road_density_major FLOAT,
    road_density_all FLOAT,
    urban_landuse_ratio FLOAT,
    source_version TEXT NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_osm_zone
        FOREIGN KEY (zone_id)
        REFERENCES zones_hex(zone_id),

    CONSTRAINT uq_osm_zone_bucket_version
        UNIQUE (zone_id, bucket_id, source_version)
);

CREATE INDEX idx_osm_features_bucket_id
ON osm_features_by_zone (bucket_id);

CREATE INDEX idx_osm_features_zone_id
ON osm_features_by_zone (zone_id);