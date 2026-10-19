# Jobs planifiés (systemd)

## Import OSM mensuel (#35)

| Fichier | Rôle |
|---|---|
| `biowatch-osm-import.timer` | Déclenche le service le 5 de chaque mois à 03:00 (Europe/Paris). |
| `biowatch-osm-import.service` | Lance `biowatch-jobs osm_import_scheduled --env prod` sous `biowatch-jobs`. |
| `jobs.env.example` | Modèle de `/etc/biowatch/jobs.env` (secrets, hors dépôt). |

Chaque déclenchement importe **une** région : la première AOI du registre
(`packages/core/aoi/aoi_registry.json`) qui n'a pas encore d'import réussi pour l'année en
cours. Une région en échec est retentée au déclenchement suivant avant de passer à la
suivante. Quand toutes les régions sont importées, le job ne fait rien jusqu'à l'année
suivante.

Résultat : schéma `osm_tmp_{aoi}_{année}` + ligne `job_runs` en `success` avec
`output_ref` = nom du schéma. C'est le signal de fin d'import consommé par #38. Un
message SQS est publié en plus si `OSM_IMPORT_DONE_QUEUE_URL` est renseigné (désactivé tant
que BioWatch n'a pas de compte AWS).

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
  /opt/biowatch/app/.venv/bin/biowatch-jobs osm_import_snapshot --aoi idf --bucket 2016 --env prod
```

Le job est idempotent : relancer un import déjà réussi ne fait rien. Un schéma laissé par un
import qui a planté est supprimé puis réimporté au retry.
