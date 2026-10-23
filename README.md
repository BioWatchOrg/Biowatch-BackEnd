# Biowatch-ESP
BioWatch aide à détecter et anticiper les tensions écologiques grâce à la fusion de données satellites, open data et IA.

---
## PostGis 
### Locally 
Vous devez enregistrer les données dans un .env avant de lancer (cd .env.example)


1) run postgis
```bash
docker compose -f infra/docker/docker-compose.yml up -d postgis
# 2. créer extensions + tables (variables lues depuis .env)
uv run biowatch-jobs init_db
# 3. enregistrer les h3 pour le MVP 
uv run biowatch-jobs generate_h3_grid --aoi idf --resolution 8
```
2) see logs
```bash
docker compose -f infra/docker/docker-compose.yml logs -f postgis
```
3) connect to postgres
```bash
docker exec -it postgis psql -U <user_name> -d <db_name>
```
4) reset DB to apply migration
⚠️  all your local data will be deleted
```bash 
docker compose -f infra/docker/docker-compose.yml down -v && \
docker compose -f infra/docker/docker-compose.yml up -d postgis
# 2. créer extensions + tables (variables lues depuis .env)
uv run biowatch-jobs init_db
# 3. enregistrer les h3 pour le MVP 
uv run biowatch-jobs generate_h3_grid --aoi idf --resolution 8
```
---
## CLI 
### Lancer
````bash
    uv run biowatch-jobs --help # Pour voir les jobs disponibles 
    uv run biowatch-jobs <job> --help # Pour voir les arguments à mettre 
    uv run biowatch-jobs generate_h3_grid --aoi idf --resolution 5
````

### Extraction Sentinel-2
Indices NDVI / NDWI / NDBI / SWIR mensuels par cellule H3, calculés localement depuis les
images Sentinel-2 L2A de Planetary Computer (gratuit, sans compte ni clé — voir ADR-004 dans
`docs/architecture-decisions.md`). Résultat dans `satellite_features_by_zone`, une ligne par
cellule et par mois (`bucket_id` = `YYYY-MM`).

La grille H3 de l'AOI doit exister (`satellite_features_by_zone.zone_id` référence
`zones_hex` — lancer `generate_h3_grid` d'abord, **même résolution**).
```bash
uv run biowatch-jobs generate_h3_grid --aoi idf --resolution 8
uv run biowatch-jobs extract_sentinel --aoi idf --bucket_id 2025-05   # un mois
uv run biowatch-jobs extract_sentinel --aoi idf                       # le mois précédent (run mensuel)
```
Ordre de grandeur (IDF, résolution 8, ~18 300 cellules) : 12 à 18 min et ~1,8 Go de RAM par
mois. Relancer un mois déjà réussi ne fait rien (idempotence) ; une tuile illisible est
enregistrée dans `job_run_zone_errors` pour ses cellules.

### Backfill Sentinel-2 (historique)
Enchaîne le job mois par mois. Relancer la même commande reprend là où elle s'est arrêtée
(les mois déjà réussis sont sautés). Compter 25 à 35 h pour l'IDF depuis 2016 : à lancer
**hors VPS** (Mac branché, `caffeinate -i`), en écrivant dans la base du VPS via un tunnel SSH.
```bash
# terminal 1 : tunnel SSH (port local 15432 -> PostgreSQL du VPS, jamais exposé à Internet)
ssh -p 50022 -i ~/.ssh/biowatch_ed25519 -N \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -L 15432:127.0.0.1:5432 <user>@<IP_DU_VPS>

# terminal 2 : .env pointant sur le tunnel (POSTGRES_HOST=127.0.0.1, POSTGRES_PORT=15432,
# identifiants de prod — jamais commités)
caffeinate -i uv run biowatch-jobs backfill_sentinel --aoi idf --from 2016-01 --to 2026-09
```
Code de sortie != 0 s'il reste des mois en échec : relancer la même commande.

### Enregistrer un nouveau job
Il faut enregistrer le nouveau job dans : apps/jobs/definitions.py

---


