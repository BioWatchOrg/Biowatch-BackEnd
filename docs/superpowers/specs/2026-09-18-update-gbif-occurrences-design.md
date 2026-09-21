# update_gbif_occurrences — design

## Contexte

Ticket : récupérer les occurrences GBIF pour une AOI sur une fenêtre glissante
de 10 ans, filtrer la qualité, convertir les points en `zone_id` H3, stocker
dans `staging.species_occurrences_raw`. Fréquence : 1×/an (année N → recharge
N-9 → N).

Source de vérité : page Notion « update_gbif_occurrences » (fournie par
l'utilisateur après une première itération de ce design basée uniquement sur
l'issue GitHub — cette v2 corrige le schéma et la logique pour coller
exactement au Notion). Le MCP `biowatch-notion` reste non connecté
(`NOTION_TOKEN` placeholder) ; si le Notion évolue après cette date, relire la
page directement plutôt que ce document.

Prérequis DoR déjà en place dans le repo : AOI registry + `load_aoi(aoi_id)`
(`packages/core/aoi.py`), grille H3 (`packages/geo/h3_grid.py`), idempotence +
`job_run` (`packages/clients/db/job_runs.py`), env local (voir commits
précédents). **Écart connu avec le DoR** : le Notion mentionne "CLI Click" —
le repo utilise un runner CLI maison (`argparse` + registre dans
`apps/jobs/registry.py`), fonctionnellement équivalent, pas un blocker.
**Écart non résolu** : env dev/prod VPS pas encore montés, seul l'env local
existe — hors scope de ce ticket, à suivre séparément.

## Décisions validées

- **API GBIF** : Occurrence Search API (`GET /v1/occurrence/search`), paginée,
  un sous-appel par année de la fenêtre. Pas de Download API (asynchrone,
  complexité opérationnelle non justifiée pour une AOI régionale).
- **Filtre spatial GBIF** : paramètre `geometry` (WKT du polygone réel de
  l'AOI, `aoi.geom.wkt`) — pas un bbox lat/lon. Risque connu : GBIF limite la
  complexité du WKT (auto-intersection interdite, nombre de sommets limité).
  Si la requête est rejetée pour un polygone trop détaillé, fallback :
  simplifier la géométrie (`shapely.simplify` avec une tolérance qui
  préserve le polygone englobant) avant de l'envoyer. À vérifier en pratique
  sur l'AOI `idf`.
- **Résolution H3** : `aoi.default_res` (celle de `zones_hex`), pour que le
  `zone_id` calculé soit cohérent avec la grille déjà générée.
- **Filtre taxonomique** : aucun — la table raw est une base brute pour
  l'agrégation en aval (`compute_species_features_by_zone`, #39).
- **Conflit PK** : `DO NOTHING` (pas `DO UPDATE`) — une occurrence GBIF est un
  fait historique immuable, inutile de la rafraîchir si déjà en base.
  `upsert(session, model, rows, update_columns=[])` fait déjà ça (le helper
  existant bascule sur `ON CONFLICT DO NOTHING` si `update_columns` est une
  liste vide).
- **Purge staging** : à chaque run, `DELETE` les lignes de l'AOI dont
  `event_date < date_min` — garde la table bornée aux 10 ans glissants au
  lieu de grossir indéfiniment. Confirmé explicitement (opération
  destructive, DoD Notion l'exige).
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

## Logique (ordre exact du Notion)

1. **`job_runs`** : `job_run(...)` standard — `success` → skip
   (`JobAlreadySucceeded`), `failed`/`partial`/`running` → retry.
2. **Fenêtre glissante** :
   `date_min = date(N - 9, 1, 1)`, `date_max = date(N, 12, 31)`
   (`N = biowatch_now().year`).
3. **Purge staging hors fenêtre** :
   `DELETE FROM staging.species_occurrences_raw WHERE aoi_id = :aoi AND event_date < :date_min`
   — avant le fetch, dans la même transaction que le job (`job_run` session).
4. **Appel GBIF** sur la fenêtre + AOI :
   `GET /v1/occurrence/search?geometry=<WKT>&hasCoordinate=true&year=<year>&limit=300&offset=<n>`
   — un sous-appel par année de `range(N-9, N+1)`, pagination jusqu'à
   `endOfRecords=true`.
5. **Filtrage qualité** (en plus des filtres GBIF natifs) :
   - coordonnées non nulles, pas `(0, 0)`, dans bornes valides
     (-90/90, -180/180) ;
   - `eventDate` GBIF présent et parseable en `DATE`, dans
     `[date_min, date_max]` — **`event_date` est `NOT NULL` en base**, donc
     une occurrence sans date exploitable est rejetée (pas de fallback sur
     `year` seul) ;
   - optionnel (non bloquant pour ce ticket) : `coordinateUncertaintyInMeters`
     sous un seuil.
6. **Conversion point → H3** : `zone_id = h3.latlng_to_cell(lat, lon, aoi.default_res)`.
   Safety optionnelle : rejeter si `zone_id` absent de `zones_hex` pour cette
   AOI (le `geometry` WKT réduit déjà ce cas vs. un bbox, mais garde le
   filet de sécurité, coût quasi nul — un `SELECT zone_id FROM zones_hex
   WHERE aoi_id=...` une fois avant la boucle).
7. **Insertion idempotente** : `upsert(session, SpeciesOccurrencesRaw, rows, update_columns=[])`
   par lot (batch par année, pas ligne à ligne) — `ON CONFLICT (gbif_occurrence_id) DO NOTHING`.
8. **Fin de run** : `job_run` marque `success` (ou `failed` si exception —
   pas de granularité par zone, la boucle est par année pas par zone H3).

## Schéma `staging.species_occurrences_raw`

Schéma Postgres `staging` créé dans `init_db.py` au même endroit que les
extensions (`CREATE SCHEMA IF NOT EXISTS staging`), avant `create_all`.

```sql
CREATE TABLE IF NOT EXISTS staging.species_occurrences_raw (
  gbif_occurrence_id   TEXT PRIMARY KEY,
  event_date           DATE NOT NULL,
  gbif_taxon_key       INT NULL,
  scientific_name      TEXT NOT NULL,
  decimal_latitude     DOUBLE PRECISION NOT NULL,
  decimal_longitude    DOUBLE PRECISION NOT NULL,
  zone_id              TEXT NOT NULL,   -- H3
  aoi_id               TEXT NOT NULL,
  ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Repris tel quel du Notion (DDL source de vérité) — colonnes exactes, pas de
`taxon_key`/`event_year`/`basis_of_record`/`source_version` ajoutés côté code.
`zone_id` n'a pas de contrainte FK explicite vers `zones_hex` dans le DDL
(cohérence assurée par le filtre applicatif à l'étape 6, pas par la DB).
`aoi_id` est obligatoire : la table est globale, partagée entre AOI, pas une
table par région.

`gbif_occurrence_id` = GBIF `key` (entier GBIF, casté en `TEXT`) — stable et
unique, jamais un `occurrenceID` Darwin Core (souvent absent/dupliqué entre
datasets).

## Idempotence

```
idempotency_key = compute_idempotency_key(
    job_name="update_gbif_occurrences",
    scope=aoi_label,
    period=str(N),                # année cible N
    source_version="gbif-search-v1",
)
```

Un run par (AOI, année N). Rejouer la même année N → skip (`job_run`
standard). L'année suivante (N+1) est un nouveau run : il refetch N-8..N+1
(chevauchement volontaire avec le run précédent), la purge (étape 3) et le
`DO NOTHING` (étape 7) garantissent zéro doublon et une table bornée à la
fenêtre courante.

## Gestion d'erreur

Pas de granularité par zone (la boucle est par année GBIF, pas par zone H3) :
`job_run_zone_errors` ne s'applique pas à ce job. Un échec réseau GBIF sur une
année fait échouer tout le run (`status=failed`), le retry standard de
`job_run` relance l'ensemble.

## Tests (objectif ≥ 90% coverage sur le nouveau code)

- **Client GBIF** (`packages/clients/gbif`) : mock `httpx` (aucun appel réseau
  réel), pagination multi-pages, arrêt sur `endOfRecords`, gestion erreur HTTP
  (4xx/5xx → exception typée), géométrie WKT trop complexe (simuler un rejet
  GBIF si testable).
- **Filtre qualité** (`packages/species/quality.py`) : cas limites — lat/lon
  `None`, `(0, 0)`, hors bornes, `eventDate` absent/invalide, date hors
  fenêtre `[date_min, date_max]`.
- **Conversion point → H3** : point connu → cell attendue (réutilise le test
  déterministe existant sur `compute_h3_cells`).
- **Purge staging** : lignes hors fenêtre supprimées, lignes dans la fenêtre
  conservées, scoping correct par `aoi_id` (ne touche pas les autres AOI).
- **Job complet** (`packages/species/gbif_occurrences.py`) : DB de test comme
  `test_upsert.py` — vérifie idempotence (rerun même année = skip via
  `JobAlreadySucceeded`), vérifie qu'un rerun avec un `gbif_occurrence_id`
  déjà connu ne duplique pas et ne modifie pas la ligne existante (`DO
  NOTHING`), vérifie que les points hors `zones_hex` sont exclus.

## Hors scope

- Agrégation par espèce / statut IUCN par zone → job séparé
  (`compute_species_features_by_zone`, #39).
- Job de retry dédié (#40) — ce job suit juste le contrat `job_run` standard.
- Env dev/prod VPS — hors scope, suivi séparément (issues #18/#19).
