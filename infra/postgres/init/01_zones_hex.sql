CREATE TABLE zones_hex (
    zone_id TEXT PRIMARY KEY,
    resolution INT NOT NULL,
    geom GEOMETRY(POLYGON, 4326) NOT NULL,
    centroid GEOMETRY(POINT, 4326),
    bbox GEOMETRY(POLYGON, 4326),
    aoi_id TEXT NOT NULL, 
    aoi_version TEXT NOT NULL,

    CONSTRAINT uq_zones_hex_zone_resolution
        UNIQUE (zone_id, resolution)
);

CREATE INDEX idx_zones_hex_geom
ON zones_hex
USING GIST (geom);