# AOI & grille H3 (`zones_hex`)

Doc courte de la fondation « Grille H3 & AOI » : convention AOI, règle de
bordure, versionnage, et lancement du job.

## Registry AOI — source de vérité

Les AOI (zones d'étude) sont déclarées dans
`packages/core/aoi/aoi_registry.json`. Aucun code ne prend un label libre :
`load_aoi(aoi_id)` valide le label contre le registry avant tout chargement.

Chaque entrée :

```json
{
  "label": "idf",
  "name": "Île-de-France",
  "geojson_path": "packages/core/aoi/regions/idf.geojson",
  "default_res": 8,
  "version": "1.0.0"
}
```

- **`label`** : identifiant AOI — convention **minuscules, 3 lettres** (`idf`, `bre`, `pac`, …).
- **`geojson_path`** : limites géographiques de l'AOI (un GeoJSON par région, `regions/`).
- **`default_res`** : résolution H3 par défaut si non précisée au lancement.
- **`version`** : version de l'AOI (**par-AOI**, pas globale). À bumper dès que la
  géométrie ou la règle de découpe change → nouvelle clé d'idempotence → grille recalculée.

`load_aoi(aoi_id)` renvoie un `AOI` (`geom` WGS84 / EPSG:4326, `bbox`, `version`,
`default_res`). Une entrée sans `version` ou `default_res` lève `LoadingAOIError`
avec un message explicite (pas de `KeyError` brut).

## Règle de bordure — FIGÉE : centroid-in-polygon

Une cellule H3 entre dans la grille **ssi son centre tombe dans le polygone de
l'AOI** (mode containment par défaut de `h3.h3shape_to_cells`, h3 v4). Cette
règle détermine les cellules de bord ; **la changer impose de bumper la version
de l'AOI** (les `zone_id` changeraient).

## Déterminisme & idempotence

`generate_h3_grid` est idempotent : même `aoi_id + h3_res + aoi_version` ⇒ mêmes
`zone_id`. Clé d'idempotence = `compute_idempotency_key(job, scope=aoi_id,
resolution, source_version=aoi.version)`. Enveloppé dans `job_run(...)` :
`success` ⇒ skip, `failed`/`partial` ⇒ retry. Écriture par `upsert` dans
`zones_hex` (pas de doublon sur re-run).

## Stockage des artefacts parquet — **local** (S3 différé)

La grille est exportée en parquet **en local**, partitionnée par version :

```
docs/grids/aoi={aoi_id}/version={aoi_version}/res={h3_res}/grid.parquet
```

> Choix d'archi : stockage **local VPS par défaut** (cf. `CLAUDE.md`). La migration
> S3 (`s3://…/processed/grids/…`) reste possible mais n'est **pas** implémentée :
> elle sera ajoutée seulement sur besoin documenté. La DoD Notion mentionne S3 —
> décision équipe de rester local pour l'instant.

## Champs `zones_hex`

`zone_id` (H3, PK), `resolution`, `aoi_id`, `aoi_version`, `geom` (Polygon 4326),
`centroid` (Point 4326), `bbox` (Polygon 4326). Unicité sur `(zone_id, resolution)`.

## Lancement

```bash
# résolution explicite
uv run biowatch-jobs generate_h3_grid --aoi idf --resolution 8

# résolution omise ⇒ default_res de l'AOI (8 pour idf)
uv run biowatch-jobs generate_h3_grid --aoi idf
```
