-- osm2pgsql flex style for the BioWatch OSM import (ticket #35).
--
-- Keeps only what the compute job (#38) needs for the human-pressure features
-- of `osm_features_by_zone`, nothing else:
--   buildings → building_area_ratio
--   roads     → road_density_major / road_density_all
--   landuse   → urban_landuse_ratio
--
-- Geometries are stored in EPSG:4326 (project rule); metric computations are
-- done later in EPSG:2154. Tables land in the schema given by `--schema`.

local tables = {}

tables.buildings = osm2pgsql.define_area_table('buildings', {
    { column = 'building', type = 'text', not_null = true },
    { column = 'geom', type = 'multipolygon', projection = 4326, not_null = true },
})

tables.roads = osm2pgsql.define_way_table('roads', {
    { column = 'highway', type = 'text', not_null = true },
    { column = 'is_major', type = 'boolean', not_null = true },
    { column = 'geom', type = 'linestring', projection = 4326, not_null = true },
})

tables.landuse = osm2pgsql.define_area_table('landuse', {
    { column = 'landuse', type = 'text', not_null = true },
    { column = 'geom', type = 'multipolygon', projection = 4326, not_null = true },
})

-- Major network: motorways down to secondary roads, with their links.
local MAJOR_ROADS = {
    motorway = true, motorway_link = true,
    trunk = true, trunk_link = true,
    primary = true, primary_link = true,
    secondary = true, secondary_link = true,
}

-- Roads open to motor traffic (paths, footways, cycleways, tracks excluded).
local ROADS = {
    tertiary = true, tertiary_link = true,
    unclassified = true, residential = true, living_street = true, service = true,
}
for k in pairs(MAJOR_ROADS) do ROADS[k] = true end

-- Artificialised land uses.
local URBAN_LANDUSE = {
    residential = true, commercial = true, industrial = true, retail = true,
    construction = true, garages = true, railway = true, brownfield = true,
}

local function insert_area(object, geom)
    local building = object.tags.building
    if building and building ~= 'no' then
        tables.buildings:insert({ building = building, geom = geom })
    end
    local landuse = object.tags.landuse
    if landuse and URBAN_LANDUSE[landuse] then
        tables.landuse:insert({ landuse = landuse, geom = geom })
    end
end

function osm2pgsql.process_way(object)
    local highway = object.tags.highway
    if highway and ROADS[highway] and object.tags.area ~= 'yes' then
        tables.roads:insert({
            highway = highway,
            is_major = MAJOR_ROADS[highway] == true,
            geom = object:as_linestring(),
        })
    end
    if object.is_closed then
        insert_area(object, object:as_polygon())
    end
end

function osm2pgsql.process_relation(object)
    if object.tags.type == 'multipolygon' then
        insert_area(object, object:as_multipolygon())
    end
end
