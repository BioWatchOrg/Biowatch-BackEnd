# Jobs planifiés (systemd)

| Fichier | Rôle |
|---|---|
| `biowatch-job@.service` | Unité commune à tous les jobs (`biowatch-job@<job>`), sous `biowatch-jobs`. Installée par `infra/vps/60-jobs-deploy.sh`. |
| `jobs.env.example` | Modèle de `/etc/biowatch/jobs.env` (réglages des jobs, hors dépôt). Pose `BIOWATCH_ENV=prod` : c'est lui qui choisit l'environnement (#18). |

Aucun timer n'est écrit à la main ici : un job est planifié en renseignant `schedule` dans
`apps/jobs/definitions.py`, et le déploiement CI génère `biowatch-job@<job>.timer` (#27).
Mise en place, exploitation et jobs planifiés : voir `infra/vps/README.md`, section
« Déploiement des jobs ».
