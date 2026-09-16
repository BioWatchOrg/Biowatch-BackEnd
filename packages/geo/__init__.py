# API publique de packages/geo — volontairement minimale (voir docs/shared-functions.md).
#
# D'autres fonctions existent dans les modules du package (h3.py, io.py, metrics.py,
# intersections.py) mais ne sont pas exposées ici faute de besoin identifié hors du package.
# Elles restent importables directement (ex. `from geo.metrics import area_m2`) pour un usage
# interne à `packages/geo`, et peuvent être remontées ici — puis documentées dans
# docs/shared-functions.md — le jour où un besoin réel apparaît ailleurs dans le repo.
#
# Doc Notion (structure du package, workflows, conventions) :
# https://notion.so/3dd50bea018d816b9d41e8472d033b2b

from .h3 import (
    H3Cell,  # alias de type : id d'une cell H3 (string)
    H3ConversionError,  # erreur de conversion H3 <-> géométrie
    cell_to_bbox,  # H3 cell -> rectangle englobant (Polygon)
    cell_to_centroid,  # H3 cell -> centre (Point)
    cell_to_geojson,  # H3 cell -> GeoJSON (dict)
    cell_to_polygon,  # H3 cell -> polygone exact (Polygon)
    cell_to_wkt,  # H3 cell -> WKT (str)
    polygon_to_cells,  # géométrie arbitraire -> cellules H3 qui la couvrent
)
from .h3_grid import (
    compute_h3_cells,  # coverage H3 déterministe d'une AOI du registre (pure, sans DB)
    generate_h3_grid,  # génère et persiste la grille H3 d'une AOI dans zones_hex (DB)
)
from .intersections import (
    IntersectionError,  # erreur de calcul d'intersection géométrique
    coverage_ratio,  # % d'une géométrie couvert par une autre
    filter_intersecting,  # ne garde que les géométries d'une liste qui touchent une géométrie
)
from .io import (
    GeoJSON,  # alias de type : un dict GeoJSON
    GeometryIOError,  # erreur de sérialisation/parsing géométrique (WKT/GeoJSON)
)
from .metrics import (
    SRID_LAMBERT93,  # EPSG 2154 (Lambert-93, mètres, France métropolitaine)
    SRID_WGS84,  # EPSG 4326 (WGS84, degrés, SRID de stockage/échange)
    MetricsError,  # erreur de calcul de métrique géométrique (surface/longueur)
    length_m,  # longueur réelle en mètres d'une géométrie
)

__all__ = [
    "H3Cell",
    "H3ConversionError",
    "cell_to_bbox",
    "cell_to_centroid",
    "cell_to_geojson",
    "cell_to_polygon",
    "cell_to_wkt",
    "polygon_to_cells",
    "compute_h3_cells",
    "generate_h3_grid",
    "IntersectionError",
    "coverage_ratio",
    "filter_intersecting",
    "GeoJSON",
    "GeometryIOError",
    "SRID_LAMBERT93",
    "SRID_WGS84",
    "MetricsError",
    "length_m",
]
