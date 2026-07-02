# clients.db — schéma DB via ORM SQLAlchemy

Source de vérité unique du schéma PostgreSQL/PostGIS de BioWatch. Remplace les
anciens scripts `infra/postgres/init/*.sql`.

> Référence rapide. Pour le détail (schéma table par table, connexion,
> idempotence, cycle de vie des jobs, tests, dépannage) : **[`docs/database.md`](../../../docs/database.md)**.

## Contenu

- `base.py` — `Base` déclaratif partagé par tous les modèles.
- `models.py` — un modèle ORM par table métier (`zones_hex`,
  `satellite_features_by_zone`, `osm_features_by_zone`,
  `protected_areas_by_zone`, `species_features_by_zone`,
  `stress_score_by_zone`, `job_runs`, `job_run_zone_errors`).
- `session.py` — `get_engine()` / `get_sessionmaker()` + `session_scope()`
  (commit/rollback/close automatique). Construit l'URL depuis les variables
  d'environnement (`POSTGRES_*` ou `DATABASE_URL`).
- `upsert.py` — `upsert(session, Model, rows)` : écriture idempotente
  `INSERT ... ON CONFLICT DO UPDATE` sur la clé primaire du modèle.
- `job_runs.py` — helpers de traçabilité/idempotence : `job_run(...)`
  (context manager), `record_zone_error(...)`, `get_failed_zones(...)`,
  `JobAlreadySucceeded`.
- `init_db.py` — crée les extensions (`postgis`, `pgcrypto`) puis toutes les
  tables. Idempotent.

> La génération de la clé d'idempotence vit dans `core` :
> `from core import compute_idempotency_key`.

## Initialisation

```bash
# 1. démarrer PostGIS
docker compose -f infra/docker/docker-compose.yml up -d

# 2. créer extensions + tables (variables lues depuis .env)
uv run python -m clients.db.init_db
```

## Surcouche pour les jobs

Pattern standard d'un job métier : clé d'idempotence → `job_run` (skip si déjà
réussi, `failed` + re-raise si erreur) → écritures idempotentes via `upsert`.

```python
from core import compute_idempotency_key
from clients.db import (
    job_run, JobAlreadySucceeded, session_scope, upsert,
    record_zone_error, SatelliteFeaturesByZone,
)

key = compute_idempotency_key(
    "satellite_ingest", scope="idf", period="2024-Q1", source_version="s2_v3"
)

try:
    with job_run("satellite_ingest", scope="idf", bucket_id="2024-Q1",
                 idempotency_key=key) as run:
        rows = [{"zone_id": z, "bucket_id": "2024-Q1",
                 "source_version": "s2_v3", "ndvi": 0.4} for z in zones]
        with session_scope() as session:      # commit auto, rollback si exception
            upsert(session, SatelliteFeaturesByZone, rows)
        # erreur ciblée sur une zone sans faire échouer tout le run :
        record_zone_error(run.run_id, "8a1fb...", "no valid pixels")
    # sortie normale → run marqué success
except JobAlreadySucceeded:
    pass  # un run précédent a déjà réussi pour cette clé → rien à faire
# toute autre exception → run marqué failed (avec le message) puis propagée
```

Points clés :
- **`session_scope()`** commit à la sortie, **rollback sur exception** (revert),
  et ferme toujours la session.
- **`upsert`** résout les conflits sur la **PK** (clé métier naturelle des tables
  de features/scores). Sa valeur de retour n'est pas fiable sur PostgreSQL avec
  `ON CONFLICT` (psycopg renvoie souvent `-1`) — ne pas s'en servir pour de la
  logique.

## Cycle de vie d'un run

`job_run` gère les états de `job_runs.status` selon l'idempotence de `CLAUDE.md` :

```
                       ┌─ sortie propre, 0 erreur zone ──▶ success  (skip au prochain run)
running (au démarrage) ┼─ sortie propre, ≥1 erreur zone ─▶ partial  (retry, zones ciblées)
                       └─ exception ─────────────────────▶ failed   (retry, + re-raise)
```

- **`success`** : au prochain appel avec la **même `idempotency_key`**, `job_run`
  lève `JobAlreadySucceeded` → le job ne refait rien.
- **`partial`** : positionné automatiquement dès qu'au moins un
  `record_zone_error` a été enregistré pendant le bloc (sans exception). Le run
  reste **retryable**.
- **`failed`** : sur exception ; le message est stocké dans `error_message`, puis
  l'exception est propagée. Retryable aussi.

**Retry** (`partial` / `failed` / `running`) : le run existant est **réutilisé**
(même `run_id`, pas de doublon) et ses **erreurs de zone précédentes sont
purgées** au démarrage, pour que le verdict `success`/`partial` reflète seulement
la nouvelle tentative.

**Reprise ciblée** : pour ne rejouer que les zones échouées, lire les zones
**avant** de rentrer dans `job_run` (le retry purge les erreurs) :

```python
zones_to_process = get_failed_zones(key) or all_zones  # 1ʳᵉ exécution → tout
try:
    with job_run("satellite_ingest", scope="idf", idempotency_key=key) as run:
        for z in zones_to_process:
            try:
                ...  # traiter la zone z
            except Exception as e:
                record_zone_error(run.run_id, z, str(e))  # → run partial
except JobAlreadySucceeded:
    pass
```