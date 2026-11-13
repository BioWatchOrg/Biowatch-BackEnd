# Jobs planifiés (systemd)

## Import OSM mensuel (#35)

| Fichier | Rôle |
|---|---|
| `biowatch-osm-import.timer` | Déclenche le service le 5 de chaque mois à 03:00 (Europe/Paris). |
| `biowatch-osm-import.service` | Lance `biowatch-jobs osm_import_scheduled` sous `biowatch-jobs`. |
| `jobs.env.example` | Modèle de `/etc/biowatch/jobs.env` (secrets, hors dépôt). Pose `BIOWATCH_ENV=prod` : c'est lui qui choisit l'environnement (#18). |

Chaque déclenchement importe **une** région, dans l'ordre du registre
(`packages/core/aoi/aoi_registry.json`) : d'abord les régions jamais tentées pour l'année en
cours, puis seulement celles dont le dernier essai a échoué. Une région qui échoue en boucle
ne bloque donc pas les autres ; chaque échec émet l'event `osm_import.region_failed`. Quand
toutes les régions sont importées, le job ne fait rien jusqu'à l'année suivante.

Résultat : schéma `osm_tmp_{aoi}_{année}_{clé}` (8 premiers caractères de
l'`idempotency_key`) + ligne `job_runs` en `success`. C'est le signal de fin d'import consommé
par #38. Son `output_ref` est un JSON court,
`{"schema": "osm_tmp_idf_2026_ab12cd34", "source_version": "geofabrik_2026-01-01_style-v1"}` :
#38 y lit le schéma (sans reconstruire son nom) et la version de l'import.

### Prérequis sur le VPS

- PostgreSQL + PostGIS installés, base initialisée (`biowatch-jobs init_db`).
- Code déployé dans `/opt/biowatch/app` avec son venv (voir `infra/vps/README.md`, #25).
- `osm2pgsql` >= 1.9 : `sudo apt install osm2pgsql` (2.2 sur Ubuntu 26.04).
- `/etc/biowatch/jobs.env` rempli à partir de `jobs.env.example` (`root:root`, 600).

### Installation

```bash
sudo install -m 644 infra/systemd/biowatch-osm-import.{service,timer} /etc/systemd/system/
sudo install -d -o biowatch-jobs -g biowatch-jobs -m 750 /var/lib/biowatch/jobs/osm
sudo systemctl daemon-reload
sudo systemctl enable --now biowatch-osm-import.timer
```

### Exploitation

```bash
systemctl list-timers biowatch-osm-import.timer   # prochain déclenchement
sudo systemctl start biowatch-osm-import.service  # lancer tout de suite
journalctl -u biowatch-osm-import.service -e      # logs JSON du dernier run
```

Import manuel d'une année passée (bootstrap historique, snapshots Geofabrik depuis 2014) :

```bash
# Même compte et même environnement que le service ; logs dans journalctl.
sudo systemd-run --wait --collect --unit=biowatch-osm-import-manual \
  -p User=biowatch-jobs -p EnvironmentFile=/etc/biowatch/jobs.env \
  -p WorkingDirectory=/opt/biowatch/app \
  /opt/biowatch/app/.venv/bin/biowatch-jobs osm_import_snapshot --aoi idf --bucket 2016
```

Le job est idempotent : relancer un import déjà réussi ne fait rien. Un schéma laissé par un
import qui a planté est supprimé puis réimporté au retry.
