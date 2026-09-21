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
Si le port 5432 est déjà pris localement (ex. un postgres natif) : ajoute
`POSTGRES_HOST_PORT=5433` et `POSTGRES_PORT=5433` dans ton `.env`, puis lance
avec `--env-file` explicite (la substitution `${POSTGRES_HOST_PORT}` du compose
lit le `.env` du dossier courant, pas celui à côté du yml) :
```bash
docker compose -f infra/docker/docker-compose.yml --env-file .env up -d postgis
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
    uv run biowatch-jobs update_gbif_occurrences --aoi idf
````

**Note sur `update_gbif_occurrences`** : Nécessite un compte GBIF (https://www.gbif.org) : ajoute `GBIF_USERNAME`
et `GBIF_PASSWORD` dans `.env` avant de lancer ce job (voir
`.env.example`). Ce job appelle l'API Download de GBIF (asynchrone) : il
peut prendre plusieurs minutes selon le volume de l'AOI. Si la DB existante
n'a pas encore le schéma `staging`, relancer `uv run biowatch-jobs init_db`
avant.

### Enregistrer un nouveau job
Il faut enregistrer le nouveau job dans : apps/jobs/definitions.py

---


