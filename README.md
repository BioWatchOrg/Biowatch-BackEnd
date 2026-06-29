# Biowatch-ESP
BioWatch aide à détecter et anticiper les tensions écologiques grâce à la fusion de données satellites, open data et IA.

---
## PostGis 
### Locally 
you need to provide .env file with var listed in .env.example


1) run postgis
```bash
docker compose -f infra/docker/docker-compose.yml up -d postgis
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
```
---
