# update_gbif_occurrences — design

## Contexte

Ticket : récupérer les occurrences GBIF pour une AOI sur une fenêtre glissante
de 10 ans, filtrer la qualité, convertir les points en `zone_id` H3, stocker
dans `staging.species_occurrences_raw`. Fréquence : 1×/an (année N → recharge
N-9 → N).

Source du ticket : issue GitHub (AC + DoR collées par l'utilisateur). La page
Notion « update_gbif_occurrences » n'a pas pu être consultée (MCP
`biowatch-notion` non connecté — `NOTION_TOKEN` est encore un placeholder dans
`.env`). Ce document est donc basé sur l'AC de l'issue + les choix validés
ci-dessous, pas sur le détail Notion.

Prérequis DoR déjà en place dans le repo : environnements (env local créé),
AOI registry (`packages/core/aoi.py`), grille H3 (`packages/geo/h3_grid.py`),
idempotence + `job_run` (`packages/clients/db/job_runs.py`).

## Décisions validées

- **API GBIF** : Occurrence Search API (`GET /v1/occurrence/search`), paginée,
  un sous-appel par année de la fenêtre. Pas de Download API (asynchrone,
  complexité opérationnelle non justifiée pour une AOI régionale).
- **Résolution H3** : `aoi.default_res` (celle de `zones_hex`), pour que le
  `zone_id` calculé soit directement une clé étrangère valide.
- **Filtre taxonomique** : aucun — la table raw est une base brute pour
  l'agrégation en aval (`compute_species_features_by_zone`, #39).
- **Nouveau package** : `packages/species/` — logique métier biodiversité,
  accueillera aussi #39. Le client HTTP GBIF vit dans
  `packages/clients/gbif/` (accès externe, cohérent avec la séparation
  `clients` = DB/storage/services externes).

## Architecture

```
apps/jobs/definitions.py
  └─ update_gbif_occurrences (CLI: --aoi, --debug)
        └─ packages/species/gbif_occurrences.py
              ├─ packages/clients/gbif/client.py   (HTTP GBIF, pagination brute)
              ├─ packages/species/quality.py        (filtre qualité, pur)
              └─ packages/geo/h3_grid.py            (+ nouvelle fn point→cell)
```

Le job orchestre ; le client GBIF ne connaît rien de la DB ; le filtre qualité
est pur (testable sans I/O) ; `packages/geo` gagne une fonction inverse de
`compute_h3_cells` (point → cell) réutilisable ailleurs.

## Data flow

1. `load_aoi(aoi_label)` → bbox (pour la requête GBIF) + résolution par défaut.
2. Année courante `N = biowatch_now().year`. Fenêtre = `range(N-9, N+1)`.
3. Pour chaque année de la fenêtre : pagination GBIF (`offset`/`limit=300`,
   arrêt sur `endOfRecords`), filtres natifs GBIF `hasCoordinate=true`,
   `hasGeospatialIssue=false`, `decimalLatitude`/`decimalLongitude` = bbox AOI,
   `year=<année>`.
4. Filtre qualité applicatif (fonction pure, testée en isolation) :
   - coordonnées non nulles, `-90 ≤ lat ≤ 90`, `-180 ≤ lon ≤ 180` ;
   - date exploitable : `eventDate` ou `year` parseable, année dans la
     fenêtre `[N-9, N]`.
5. Conversion point → `zone_id` (H3, résolution AOI).
6. Le bbox GBIF est plus large que le polygone réel de l'AOI (WKT complexe non
   fiable côté GBIF) : après conversion, on ne garde que les `zone_id`
   présents dans `zones_hex` pour cette AOI — réutilise la grille déjà
   calculée comme filtre d'appartenance, pas de recalcul géométrique.
7. `upsert(session, SpeciesOccurrencesRaw, rows)` par lot, dans le
   `job_run(...)`.

## Schéma `staging.species_occurrences_raw`

Nouveau schéma Postgres `staging`, créé dans `init_db.py` au même endroit que
les extensions (`CREATE SCHEMA IF NOT EXISTS staging`), avant `create_all`.

| Colonne | Type | Notes |
| --- | --- | --- |
| `gbif_occurrence_id` | String, **PK** | GBIF `key` (entier GBIF, casté en string) — stable, unique, jamais un `occurrenceID` Darwin Core (souvent absent/dupliqué entre datasets) |
| `zone_id` | String, FK `zones_hex.zone_id` | résolution AOI |
| `scientific_name` | String | |
| `taxon_key` | Integer, nullable | id taxon GBIF |
| `decimal_latitude` | Float | |
| `decimal_longitude` | Float | |
| `event_year` | Integer | année retenue pour la fenêtre |
| `event_date` | Date, nullable | peut être absent même si `year` présent |
| `basis_of_record` | String, nullable | |
| `source_version` | String | ex. `"gbif-search-v1"`, versionnement pipeline |
| `ingested_at` | DateTime(tz), `server_default=now()` | |

PK simple sur `gbif_occurrence_id` (demande explicite du ticket, différent du
pattern composite des autres tables features) : un `gbif_occurrence_id` est
déjà globalement unique, pas besoin de le composer avec `source_version`.

## Idempotence

```
idempotency_key = compute_idempotency_key(
    job_name="update_gbif_occurrences",
    scope=aoi_label,
    period=str(N),                # année cible, pas un bucket_id classique
    source_version="gbif-search-v1",
)
```

Un run par (AOI, année N). Rejouer la même année N → skip (`job_run` standard).
L'année suivante (N+1) est un nouveau run : il refetch N-8..N+1, chevauchement
volontaire avec le run précédent — sans risque de doublon grâce à l'`upsert`
sur la PK `gbif_occurrence_id`.

## Gestion d'erreur

Pas de granularité par zone ici (la boucle est par année GBIF, pas par zone
H3) : `job_run_zone_errors` ne s'applique pas à ce job. Un échec réseau GBIF
sur une année fait échouer tout le run (`status=failed`), le retry standard de
`job_run` relance l'ensemble — un refetch GBIF est peu coûteux et les réponses
sont déterministes, pas besoin de checkpoint par année.

## Tests (objectif ≥ 90% coverage sur le nouveau code)

- **Client GBIF** (`packages/clients/gbif`) : mock `httpx` (aucun appel réseau
  réel), pagination multi-pages, arrêt sur `endOfRecords`, gestion erreur HTTP
  (4xx/5xx → exception typée).
- **Filtre qualité** (`packages/species/quality.py`) : cas limites — lat/lon
  `None`, hors bornes, `eventDate` invalide, année hors fenêtre, `year`
  présent sans `eventDate`.
- **Conversion point → H3** : point connu → cell attendue (réutilise le test
  déterministe existant sur `compute_h3_cells`).
- **Job complet** (`packages/species/gbif_occurrences.py`) : DB de test comme
  `test_upsert.py` — vérifie idempotence (rerun même année = skip via
  `JobAlreadySucceeded`), vérifie qu'un rerun avec un GBIF `key` déjà connu
  met à jour la ligne sans dupliquer (PK), vérifie que les points hors
  `zones_hex` sont exclus.

## Hors scope

- Agrégation par espèce / statut IUCN par zone → job séparé
  (`compute_species_features_by_zone`, #39).
- Job de retry dédié (#40) — ce job suit juste le contrat `job_run` standard.
