# Biowatch-ESP
BioWatch aide à détecter et anticiper les tensions écologiques grâce à la fusion de données satellites, open data et IA.

---
## Environnements (dev / prod)

Deux environnements, deux bases, deux répertoires de données. **`dev` est le défaut** :
une commande qui ne précise rien ne peut pas écrire en production.

| | fichier de config | base | artefacts |
| --- | --- | --- | --- |
| dev | `.env.dev` (local) | `biowatch_dev` | `data/dev/` |
| prod | `.env.prod` (**VPS uniquement**) | `biowatch_prod` | `data/prod/` |

L'environnement est résolu dans cet ordre : `--env` > `BIOWATCH_ENV` > `dev`.

```bash
uv run biowatch-jobs generate_h3_grid --aoi idf   # dev (défaut)
uv run biowatch-jobs --env prod generate_h3_grid --aoi idf
```

`.env.prod` n'existe pas sur un poste de dev : c'est ce qui fait échouer un
`--env prod` lancé par erreur, avec un message explicite, avant tout accès DB.

Les tokens d'outillage (Notion) restent dans `.env`, commun aux deux envs.

### Setup (une fois)

```bash
cp .env.example .env            # tokens Notion
cp .env.dev.example .env.dev    # config applicative dev
# ⚠️ change POSTGRES_PASSWORD dans .env.dev AVANT le premier `up` :
#    Postgres ne lit ces variables qu'à l'initialisation du volume.
uv sync
```

---
## PostGis 
### Locally 
La config de la base est lue depuis `.env.dev` (voir ci-dessus).


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
### Enregistrer un nouveau job
Il faut enregistrer le nouveau job dans : apps/jobs/definitions.py

---


