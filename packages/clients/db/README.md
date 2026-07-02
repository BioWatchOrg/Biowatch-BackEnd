# clients.db — schéma DB via ORM SQLAlchemy

Source de vérité unique du schéma PostgreSQL/PostGIS de BioWatch. Remplace les
anciens scripts `infra/postgres/init/*.sql`.

## Contenu

- `base.py` — `Base` déclaratif partagé par tous les modèles.
- `models.py` — un modèle ORM par table métier (`zones_hex`,
  `satellite_features_by_zone`, `osm_features_by_zone`,
  `protected_areas_by_zone`, `species_features_by_zone`,
  `stress_score_by_zone`, `job_runs`, `job_run_zone_errors`).
- `session.py` — `get_engine()` / `get_sessionmaker()` + construction de l'URL
  depuis les variables d'environnement (`POSTGRES_*` ou `DATABASE_URL`).
- `init_db.py` — crée les extensions (`postgis`, `pgcrypto`) puis toutes les
  tables. Idempotent.

## Initialisation

```bash
# 1. démarrer PostGIS
docker compose -f infra/docker/docker-compose.yml up -d

# 2. créer extensions + tables (variables lues depuis .env)
uv run python -m clients.db.init_db
```