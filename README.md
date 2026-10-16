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
`.env.<env>` et `data/` sont cherchés **à la racine du dépôt**, quel que soit le
répertoire d'où la commande est lancée (jamais dans le cwd ni ses parents).

```bash
uv run biowatch-jobs generate_h3_grid --aoi idf   # dev (défaut)
uv run biowatch-jobs --env prod generate_h3_grid --aoi idf
```

`.env.prod` n'existe pas sur un poste de dev, et **prod exige ce fichier** : un
`--env prod` lancé par erreur s'arrête avec un message explicite, avant tout accès
DB — même si un `DATABASE_URL` ou des `POSTGRES_*` traînent dans le shell. Sur le
VPS, c'est un marqueur posé par le déploiement à côté du code (il peut être vide :
les secrets restent injectés par systemd, voir `infra/vps/README.md`). En dev, le
fichier est optionnel (la CI injecte ses variables directement).

Deuxième filet, valable dans les deux sens : **le nom de la base doit finir par
`_<env>`** (`biowatch_dev` en dev, `biowatch_prod` en prod). Une variable du shell
gagne sur `.env.dev` (`override=False`), donc un tunnel SSH qui exporte un
`DATABASE_URL` vers `biowatch_prod` ferait écrire en prod un job lancé sans `--env`.
Avec ce contrôle, la connexion est refusée avant d'être ouverte.

Ces deux garde-fous protègent contre **l'erreur**, pas contre une action délibérée :
créer soi-même `.env.prod` sur un laptop les contourne. La protection contre
l'intention est réseau et PostgreSQL (DB jamais exposée, comptes, droits). Raisonnement
complet dans `docs/architecture-decisions.md` (ADR-003).

Les tokens d'outillage (Notion) restent dans `.env`, commun aux deux envs.

**Migration** (si tu avais déjà un `.env` avec `POSTGRES_*` / `LOG_LEVEL`) : ces
variables ne sont plus lues depuis `.env`. Déplace-les dans `.env.dev` ; `.env`
ne garde que les tokens Notion.

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
# 2. créer extensions + tables (variables lues depuis .env.dev)
uv run biowatch-jobs init_db
# 3. enregistrer les h3 pour le MVP 
uv run biowatch-jobs generate_h3_grid --aoi idf --resolution 8
# 4. référentiel IUCN des espèces de France (IUCN_API_TOKEN dans .env.dev)
uv run biowatch-jobs update_iucn_taxonomy
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
# 2. créer extensions + tables (variables lues depuis .env.dev)
uv run biowatch-jobs init_db
# 3. enregistrer les h3 pour le MVP 
uv run biowatch-jobs generate_h3_grid --aoi idf --resolution 8
```
---
## Tests

```bash
uv run pytest                                   # unitaires : aucune base requise
uv run ruff check . && uv run mypy .            # ce que la CI fait tourner
```

Les tests bout-en-bout sur PostGIS (idempotence réelle des jobs, séparation dev / prod
dans `job_runs`, `init_db` à travers les garde-fous) sont skippés en local tant que
`TEST_DATABASE_URL` n'est pas défini. **La CI les exécute toujours** (service PostGIS dans
`.github/workflows/test.yml`) : si la variable y manquait, ils échoueraient au lieu de
skipper. En local, sur une base jetable pour ne pas polluer `biowatch_dev` (ils oublient
leurs propres runs précédents dans `job_runs`, jamais rien d'autre) :

```bash
docker exec postgis psql -U biowatch -d biowatch_dev -c "CREATE DATABASE biowatch_test_dev"
TEST_DATABASE_URL=postgresql+psycopg://biowatch:<mot de passe de .env.dev>@127.0.0.1:5432/biowatch_test_dev \
  uv run pytest tests/packages/geo/test_generate_h3_grid.py
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


