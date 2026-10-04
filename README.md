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
Nécessite `SENTINEL_HUB_CLIENT_ID`/`SENTINEL_HUB_CLIENT_SECRET` dans `.env` (voir `.env.example`),
et que la grille H3 de l'AOI existe déjà (`satellite_features_by_zone.zone_id` référence
`zones_hex` — lancer `generate_h3_grid` d'abord, **même résolution**).
```bash
uv run biowatch-jobs generate_h3_grid --aoi idf --resolution 5
uv run biowatch-jobs extract_sentinel --aoi idf --bucket_id 2026-02 --h3_res 5 --max_workers 5
```
⚠️ Chaque appel consomme du quota Sentinel Hub réel — préférer une petite résolution
(`idf` en résolution 5 = 52 cellules) pour un premier test, pas la résolution 8 du MVP (~16 000
cellules).
### Enregistrer un nouveau job
Il faut enregistrer le nouveau job dans : apps/jobs/definitions.py

---


