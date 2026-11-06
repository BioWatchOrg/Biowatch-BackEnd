# shellcheck shell=bash
# config.sh — paramètres partagés par tous les scripts VPS.
#
# C'est le SEUL fichier à éditer pour faire évoluer le parc : ajouter un
# membre, changer le port, ouvrir un service. Aucun script ne code une
# valeur en dur.

# --- SSH ---------------------------------------------------------------------

# Port SSH cible. Doit être libre : 00-diagnose.sh le vérifie.
SSH_PORT=50022

# Port d'origine. Conservé en parallèle par 10-users-and-keys.sh (phase
# additive), fermé par 20-harden-ssh.sh une fois le nouveau port prouvé.
SSH_PORT_LEGACY=22

# Seuls les membres de ce groupe peuvent ouvrir une session SSH
# (directive AllowGroups). Garantit par construction que les comptes de
# service de #25 (biowatch-api, biowatch-jobs, deploy) n'auront jamais
# d'accès SSH, sans qu'on ait à y penser.
SSH_GROUP="biowatch-ssh"

# --- Comptes -----------------------------------------------------------------

# Comptes à créer. Une clé publique keys/<login>.pub DOIT exister pour
# chacun. Ajouter un membre = déposer sa clé + l'ajouter ici + rejouer
# 10-users-and-keys.sh (idempotent, ne touche pas aux comptes existants).
MEMBERS=(
  simonr
  nicolasd
  julieng
  terencen
  yarongm
  laurickb
  sagithanm
)

# Membres prévus dont la clé n'est pas encore fournie. Purement
# documentaire : aucun compte n'est créé à partir de cette liste.
# #22 ne pourra pas être fermée tant qu'elle n'est pas vide.
MEMBERS_PENDING=(
)

# Compte de secours conservé (image cloud). Il entre aujourd'hui par mot de
# passe et n'a AUCUNE clé : sans l'entrée ci-dessous il deviendrait
# inutilisable après durcissement, donc inutile comme filet.
# Mettre BREAKGLASS_ACCOUNT="" pour ne pas conserver de compte de secours.
BREAKGLASS_ACCOUNT="ubuntu"

# Clés autorisées sur le compte de secours. En lister PLUSIEURS : un accès
# de secours utilisable par une seule personne ne sauve l'équipe que si
# cette personne est disponible — exactement la dépendance à un expert
# unique que proscrit le CLAUDE.md.
BREAKGLASS_KEY_OWNERS=(
  simonr
  nicolasd
)

# --- Comptes de service (#25) ------------------------------------------------

# Comptes qui EXÉCUTENT le code. Lecture seule sur le code déployé, écriture
# uniquement dans leur propre répertoire d'état et de logs.
SERVICE_RUNTIME_ACCOUNTS=(
  biowatch-api
  biowatch-jobs
)

# Seul compte autorisé à écrire le code déployé. N'exécute aucun service et
# n'a pas sudo : un membre déploie via « sudo -u deploy ».
DEPLOY_ACCOUNT="deploy"

# Groupe des comptes d'exécution : c'est lui qui donne la lecture sur APP_DIR.
SERVICE_GROUP="biowatch"

APP_DIR="/opt/biowatch"                 # code déployé   deploy:biowatch 2750
SERVICE_STATE_ROOT="/var/lib/biowatch"  # home + état    <compte> 750
SERVICE_LOG_ROOT="/var/log/biowatch"    # logs           <compte> 750
SERVICE_CONFIG_DIR="/etc/biowatch"      # secrets        root 700, lus par systemd

# Groupes qui donnent root (sudo, docker, lxd), la lecture des logs système
# (adm) ou un accès SSH. Aucun compte de service ne doit y figurer :
# 40-service-users.sh refuse de terminer s'il en trouve un.
SERVICE_FORBIDDEN_GROUPS=(sudo adm docker lxd "$SSH_GROUP")

# --- Firewall ----------------------------------------------------------------

# Ouverts en plus de SSH. 80/443 sont posés dès maintenant pour #26 : un
# port ouvert sans processus à l'écoute n'offre aucune surface d'attaque.
FIREWALL_TCP_PORTS=(80 443)

# fail2ban : volontairement absent. Retiré de la DoD de #22, toujours sans
# objet avec #26 (aucune surface authentifiable), réexaminé avec #80 —
# motif et condition de réexamen dans le README.

# --- Nginx (#26) -------------------------------------------------------------

# Noms servis. « _ » = toute requête : il n'y a pas encore de domaine. À
# remplacer par le domaine avec le certificat Let's Encrypt.
NGINX_SERVER_NAME="_"

# Adresses d'écoute. BOUCLE LOCALE tant que l'équipe n'a pas tranché domaine
# et certificat : nginx tourne mais n'est joignable que par tunnel SSH, bien
# que UFW autorise 80 et 443. Exposer = passer à "0.0.0.0" et "[::]", et
# seulement avec un certificat Let's Encrypt (README).
NGINX_LISTEN_V4="127.0.0.1"
NGINX_LISTEN_V6="[::1]"

# Certificat TLS. En attendant le domaine : l'auto-signé d'Ubuntu (paquet
# ssl-cert), qui suffit à prouver la chaîne HTTPS par tunnel. Remplacé par
# /etc/letsencrypt/live/<domaine>/{fullchain,privkey}.pem.
NGINX_TLS_CERT="/etc/ssl/certs/ssl-cert-snakeoil.pem"
NGINX_TLS_KEY="/etc/ssl/private/ssl-cert-snakeoil.key"

# Webroot du challenge HTTP-01 de Let's Encrypt (certbot --webroot).
NGINX_ACME_ROOT="/var/www/letsencrypt"

# Compte du paquet Ubuntu. Lecture seule sur FRONTEND_DIR, rien d'autre.
NGINX_USER="www-data"

# Socle : un seul server, qui inclut un fichier par service. Brancher un
# service = déposer son fichier dans NGINX_SERVICES_DIR, jamais éditer le socle.
NGINX_SITE="/etc/nginx/sites-available/biowatch.conf"
NGINX_SITE_LINK="/etc/nginx/sites-enabled/biowatch.conf"
NGINX_DEFAULT_SITE_LINK="/etc/nginx/sites-enabled/default"
NGINX_SERVICES_DIR="/etc/nginx/biowatch.d"

# Build du frontend, déposé par la CI (Biowatch-Frontend#76). Hors APP_DIR :
# nginx n'a pas à pouvoir lire le code backend.
FRONTEND_DIR="/var/www/biowatch/frontend"   # deploy:www-data 2750

# --- Garde-fous --------------------------------------------------------------

# Délai avant restauration automatique si une opération risquée n'est pas
# confirmée. Doit laisser le temps de changer de machine, d'ouvrir une
# nouvelle session et de vérifier — 10 min s'étaient révélées trop justes.
ROLLBACK_DELAY="20min"

BACKUP_ROOT="/root/biowatch-vps-backups"
LOG_FILE="/var/log/biowatch-vps-setup.log"

# Chemins générés (ne pas modifier sans raison).
SSH_SOCKET_DROPIN="/etc/systemd/system/ssh.socket.d/10-biowatch-port.conf"
# Préfixe 00- délibéré : sshd retient la PREMIÈRE valeur rencontrée et lit
# les drop-ins en ordre alphabétique. Un 99- serait écrasé par le
# 50-cloud-init.conf de l'image et le durcissement serait sans effet.
SSH_HARDENING_DROPIN="/etc/ssh/sshd_config.d/00-biowatch-hardening.conf"
SUDOERS_FILE="/etc/sudoers.d/90-biowatch-members"
CLOUD_INIT_DROPIN="/etc/cloud/cloud.cfg.d/99-biowatch.cfg"
SSH_ROLLBACK_SCRIPT="/usr/local/sbin/biowatch-ssh-rollback.sh"
UFW_ROLLBACK_SCRIPT="/usr/local/sbin/biowatch-ufw-rollback.sh"
SOCKET_ROLLBACK_SCRIPT="/usr/local/sbin/biowatch-socket-rollback.sh"
SSH_ROLLBACK_UNIT="biowatch-ssh-rollback"
UFW_ROLLBACK_UNIT="biowatch-ufw-rollback"
SOCKET_ROLLBACK_UNIT="biowatch-socket-rollback"
