# Fonctions communes réutilisables

Catalogue des fonctions déjà implémentées dans `packages/core`, `packages/clients` et
`packages/geo`. **Règle** : avant d'écrire une fonction utilitaire (géospatial, temps,
idempotence, DB, logs...), vérifier ici si elle existe déjà. Ne pas dupliquer.

Ce fichier doit être mis à jour à chaque MR qui ajoute une fonction publique dans un de ces
packages (voir CLAUDE.md, "Documentation").

---

## packages/core

### `core.idempotency`
- `compute_idempotency_key(job_name, scope, source_version, period=None, resolution=None, env=None) -> str`
  Clé déterministe (SHA-256) pour l'idempotence d'un job. Même `(job, scope, period,
  source_version[, env])` ⇒ même clé.

### `core.time_bucket`
- `biowatch_now() -> date` — date du jour en Europe/Paris.
- `bucket_id(value=None, format=BucketFormat.MONTHLY) -> BucketId` — normalise une date en
  identifiant de période (`"2024"`, `"2024-03"`, `"2024-b02"`). `value` accepte `date`,
  `datetime`, une string ISO 8601, ou `None` (= aujourd'hui).
- `related_bucket_ids(bucket) -> list[BucketId]` — tous les buckets (année, bimestre, mois)
  qui couvrent la même période que `bucket`. Utile pour retrouver des features à une
  granularité différente.
- `BucketFormat` (enum : `MONTHLY`, `BIMONTHLY`, `YEARLY`).
- Exceptions : `UnsupportedBucketFormat`, `UnsupportedDateType`, `InvalidDateString`.

### `core.aoi`
- `aoi_registry(label) -> bool` — vérifie qu'un label AOI existe dans
  `packages/core/aoi/aoi_registry.json`.
- `load_aoi(aoi_label) -> AOI` — charge une AOI (géométrie, bbox, `version`, `default_res`)
  depuis le registre. Source de vérité pour la résolution H3 par défaut d'une zone d'étude.
- `AOI` (dataclass : `label`, `name`, `geom`, `bbox`, `version`, `default_res`).
- Exceptions : `UndefinedAOIError`, `LoadingAOIError`, `GeoJsonValueError`.

### `core.parquet`
- `write_parquet(df: pd.DataFrame, file_path: str) -> None` — écrit un DataFrame en Parquet.
  Utilisé pour les artefacts de reproductibilité des jobs (ex. `h3_grid.py`).

---

## packages/clients

### `clients.db` (session, transactions, idempotence)
- `session_scope(engine=None) -> Session` (context manager) — session transactionnelle :
  commit en sortie propre, rollback sur exception. Façon standard de parler à la DB en
  **lecture ou écriture hors job**.
- `job_run(job_name, scope, idempotency_key, bucket_id=None, engine=None) -> (RunContext, Session)`
  (context manager) — enveloppe un job avec traçabilité (`job_runs`), idempotence et session
  gérée. Lève `JobAlreadySucceeded` si déjà exécuté avec succès (à catcher pour skip). Marque
  `partial` si des erreurs par zone ont été enregistrées. **Tout job métier doit passer par
  là** (voir CLAUDE.md, "Accès base de données").
- `record_zone_error(run_id, zone_id, error_message, engine=None) -> None` — enregistre un
  échec pour une zone précise (reprise ciblée), idempotent sur `(run_id, zone_id)`.
- `get_failed_zones(idempotency_key, engine=None) -> list[str]` — zones en échec du dernier
  run pour une clé d'idempotence donnée ; à appeler **avant** de rentrer dans `job_run` pour
  un retry ciblé.
- `get_engine(echo=False) -> Engine` / `get_sessionmaker(engine=None) -> sessionmaker` —
  construction de l'engine SQLAlchemy depuis les variables d'env (`DATABASE_URL` ou
  `POSTGRES_*`), mise en cache (`lru_cache`).
- `init_db(engine=None) -> None` — crée les extensions PostgreSQL (`postgis`, `pgcrypto`) et
  toutes les tables ORM. Idempotent, à lancer une fois par environnement.
- `upsert(session, model, rows, update_columns=None) -> None` — `INSERT ... ON CONFLICT DO
  UPDATE` en masse sur la PK du modèle. **Seule façon d'écrire/mettre à jour** dans une table
  métier (jamais d'`INSERT` manuel). Chunké automatiquement sous la limite de bind params
  PostgreSQL.
- `JobAlreadySucceeded`, `RunContext` (dataclass), `Base` (déclarative base ORM).
- Modèles ORM (`clients.db.models`, source de vérité du schéma) : `ZonesHex`,
  `SatelliteFeaturesByZone`, `OsmFeaturesByZone`, `ProtectedAreasByZone`,
  `SpeciesFeaturesByZone`, `StressScoreByZone`, `JobRun`, `JobRunZoneError`.

### `clients.logs`
- `setup_logging(service="biowatch", level=None) -> None` — configure le logger racine en
  JSON structuré (un seul appel, à l'entrypoint du process — jamais dans un `__init__.py`).
  Idempotent.
- `run_id_var`, `request_id_var` (`ContextVar[str | None]`) — id de corrélation ambiant,
  injecté automatiquement dans chaque log émis pendant un job (`run_id_var`, posé par
  `job_run`) ou une requête API (`request_id_var`). Pas besoin de le passer à la main dans
  `extra`.
- Convention d'appel partout dans le repo : `logger.error/info(msg, extra={"event": "...",
  "context": {...}})`.

---

## packages/geo

Boîte à outils géospatiale pure (aucun accès DB). Voir aussi la page Notion "packages/geo —
Vocabulaire géospatial (H3, WKT, GeoJSON)" (base Documentation Technique) pour les
définitions des notions (cell, Polygon, bbox, WKT, GeoJSON).

**Convention d'API (spécifique à `packages/geo`)** : l'API publique reste minimale —
uniquement les fonctions ayant un besoin d'usage identifié ailleurs dans le repo. Une
fonction ne passe à un niveau de visibilité plus large que lorsqu'un besoin concret apparaît,
jamais par anticipation. Trois niveaux :

1. **Privé au fichier** (`_nom`, ex. `_intersects`/`_intersection`/`_reproject`) — détail
   d'implémentation, utilisé uniquement dans son propre module.
2. **Privé au package, public au fichier** (nom sans `_`, ex. `area_m2` dans
   `metrics.py`) — utilisé par d'autres modules de `packages/geo` (ici `intersections.py`),
   mais **pas exporté depuis `packages/geo/__init__.py`** : pas encore de besoin identifié en
   dehors du package.
3. **Public au package** (exporté dans `packages/geo/__init__.py`) — utilisable par le reste
   du repo (jobs, scoring, API...), soit parce qu'un appelant existe déjà (`generate_h3_grid`
   dans `apps/jobs`), soit parce que c'est un livrable explicite du DoD de la tâche Notion
   (ex. `length_m`, anticipé pour le futur job OSM même sans appelant aujourd'hui).

**Cette règle est propre à `packages/geo`** — elle ne s'applique pas (par défaut) aux autres
packages communs (`core`, `clients`), qui n'ont pas cette contrainte documentée pour
l'instant.

### `geo.h3`
- `cell_to_polygon(cell: H3Cell) -> Polygon`
- `cell_to_centroid(cell: H3Cell) -> Point`
- `cell_to_bbox(cell: H3Cell) -> Polygon`
- `cell_to_wkt(cell: H3Cell) -> str` / `cell_to_geojson(cell: H3Cell) -> dict`
- `polygon_to_cells(geom: Polygon | MultiPolygon, resolution, contain="overlap") -> list[H3Cell]`
  — cellules H3 couvrant une géométrie arbitraire (ex. un polygone OSM ou une aire protégée) —
  c'est la fonction à utiliser pour savoir dans quelles zones écrire une ligne `*_by_zone`.
  Containment par défaut **`overlap`** (pas `center`, contrairement à `compute_h3_cells`) :
  une géométrie plus petite qu'une cellule ne contient le centre d'aucune cellule, donc
  `center` la rattacherait silencieusement à zéro zone. `contain` reste surchargeable
  (`"center"`, `"full"`, `"bbox_overlap"`) via l'API expérimentale de h3
  (`h3shape_to_cells_experimental`, sans garantie de stabilité inter-versions côté h3).
  N'accepte que `Polygon`/`MultiPolygon` — passer une `LineString` (ex. une route OSM) lève
  `H3ConversionError`.
- Exception : `H3ConversionError`.

### `geo.io` (sérialisation générique, toute géométrie)
- `GeoJSON` (type alias), `GeometryIOError` : seuls exports publics de ce fichier (utilisés
  comme type/exception des fonctions publiques `cell_to_wkt`/`cell_to_geojson` de `geo.h3`).
- `to_wkt(geom) -> str` / `to_geojson(geom) -> dict` — **pas exportées depuis `geo`** (niveau
  2) : utilisées en interne par `h3.cell_to_wkt`/`cell_to_geojson`, aucun appelant hors du
  package aujourd'hui. Importables via `from geo.io import to_wkt, to_geojson`.
- `from_wkt(wkt) -> BaseGeometry` / `from_geojson(geojson) -> BaseGeometry` — **pas
  exportées depuis `geo`** (niveau 2) : aucun appelant, même interne à `packages/geo`,
  aujourd'hui. À remonter en public le jour où un besoin réel apparaît (ex. l'API doit
  parser du GeoJSON/WKT entrant).

### `geo.metrics`
- `length_m(geom, srid=4326) -> float` — longueur réelle en mètres (reprojette
  automatiquement si `srid != 2154`). Exportée depuis `geo` malgré l'absence d'appelant
  aujourd'hui : livrable explicite du DoD Notion, anticipé pour le futur job OSM
  (`road_density_major`/`road_density_all`).
- Constantes : `SRID_WGS84 = 4326`, `SRID_LAMBERT93 = 2154`.
- Exception : `MetricsError`.
- `area_m2(geom, srid=4326) -> float` — surface réelle en mètres carrés. **Pas exportée
  depuis `geo`** (niveau 2 ci-dessus) : utilisée en interne par
  `intersections.coverage_ratio`, importable directement via `from geo.metrics import
  area_m2` par les autres modules de `packages/geo`, mais aucun appelant hors du package
  aujourd'hui.
- `_reproject(geom, from_srid=4326, to_srid=2154) -> BaseGeometry` reste **privée au fichier**
  (niveau 1) : utilisée en interne par `area_m2`/`length_m`, pas encore de besoin identifié
  pour reprojeter une géométrie sans en tirer une métrique. À remonter le jour où un job en a
  besoin directement (ex. `buffer()` métrique, distance point-à-point).

### `geo.intersections`
- `filter_intersecting(geoms, mask) -> list[BaseGeometry]` — ne garde que les géométries
  d'une liste qui touchent `mask` (ex. filtrer les routes OSM qui traversent une cell).
- `coverage_ratio(geom, mask, srid=4326) -> float` — fraction de `geom` couverte par `mask` ;
  base de calcul pour un champ comme `protected_areas_by_zone.coverage_ratio`.
- Exception : `IntersectionError`.
- API volontairement minimale : `_intersects`/`_intersection` (test de contact, géométrie de
  chevauchement) existent en interne mais restent privées tant qu'aucun besoin concret ne
  justifie de les exposer — à réévaluer au cas par cas plutôt que d'élargir l'API par
  anticipation.

### `geo.h3_grid` (job, pas une fonction pure — accède à la DB)
- `compute_h3_cells(aoi_label, resolution) -> list[H3Cell]` — coverage H3 déterministe d'une
  AOI du registre (pure, sans DB — testée par golden fingerprint).
- `generate_h3_grid(aoi_label, resolution=None, engine=None) -> None` — génère et persiste la
  grille dans `zones_hex` via `job_run` + `upsert`.

---

## Scaffolding jobs (`apps/jobs`)

Pas des fonctions à appeler, mais le pattern pour brancher un nouveau job en CLI sans toucher
au reste : `apps/jobs/registry.py` (`Job` dataclass, `register(job)`) +
`apps/jobs/definitions.py` (un `Job(...)` par job métier, mappe une sous-commande CLI vers une
fonction de `packages/*`). Ajouter un job = ajouter une entrée dans `definitions.py`.

---

## Pas encore de fonctions communes

`packages/scoring`, `packages/ml`, `apps/api` sont encore des packages stubs (seul un
`__init__.py` vide) au moment de la rédaction de ce fichier — rien à réutiliser depuis eux
pour l'instant.
