#!/usr/bin/env bash
# 55-postgres.sh — issue #23.
#
# Installe PostgreSQL + PostGIS de PRODUCTION, dans Docker, avec la même image
# que le poste de dev (infra/docker) :
#
#   docker      docker.io + docker compose (paquets Ubuntu)
#   conteneur   biowatch-postgis, publié sur 127.0.0.1:5432 UNIQUEMENT
#   unité       biowatch-postgres.service (les jobs l'attendent par ce nom)
#   base        biowatch_prod, extensions postgis + pgcrypto
#   rôles       biowatch_admin (superuser, administration seulement)
#               biowatch_jobs  (crée et écrit les tables métier)
#   secrets     générés ICI, sur le VPS, jamais dans le dépôt :
#               /etc/biowatch/postgres.env  (superuser, root 600)
#               /etc/biowatch/db-jobs.env   (rôle des jobs, root 600,
#                                            EnvironmentFile= des jobs)
#
# Ne touche ni à SSH ni à UFW. Docker contourne UFW pour les ports qu'il
# publie : c'est la liaison sur 127.0.0.1 qui protège le 5432, et le script le
# PROUVE en fin d'exécution.
#
#   ./55-postgres.sh --dry-run
#   ./55-postgres.sh

USAGE="Usage: ./55-postgres.sh [--dry-run]

Installe Docker et PostgreSQL + PostGIS (base biowatch_prod) sur la boucle
locale, crée le rôle des jobs, puis PROUVE le résultat (écoute locale
uniquement, connexion du rôle des jobs, PostGIS actif). Idempotent : les
mots de passe existants ne sont jamais régénérés.

  --dry-run   affiche les changements sans les appliquer"

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
init_script "${BASH_SOURCE[0]}" "$@"
require_root
init_log
enable_error_trap

banner "#23 — PostgreSQL + PostGIS (prod)"

POSTGRES_SRC="${LIB_DIR}/postgres"
COMPOSE_FILE="${POSTGRES_CONF_DIR}/compose.yaml"
UNIT_FILE="/etc/systemd/system/${POSTGRES_UNIT}.service"

# ── Préconditions ────────────────────────────────────────────────────────

step "Vérification des préconditions"
[[ -d "$SERVICE_CONFIG_DIR" ]] \
  || die "${SERVICE_CONFIG_DIR} absent : lancer 40-service-users.sh d'abord"
[[ -f "${POSTGRES_SRC}/compose.yaml" && -f "${POSTGRES_SRC}/${POSTGRES_UNIT}.service" ]] \
  || die "fichiers source absents dans ${POSTGRES_SRC}"
command -v openssl >/dev/null 2>&1 || die "openssl introuvable (génération des mots de passe)"
ok "préconditions remplies"

# ── Docker ───────────────────────────────────────────────────────────────

step "Paquets Docker"
missing=()
command -v docker >/dev/null 2>&1 || missing+=(docker.io)
docker compose version >/dev/null 2>&1 || missing+=(docker-compose-v2)
if (( ${#missing[@]} == 0 )); then
  log "docker et docker compose déjà installés"
else
  run apt-get update -qq
  # via env : run exécute "$@", une affectation en tête serait prise pour le
  # nom de la commande.
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  done_ok "installé : ${missing[*]}"
fi
run systemctl enable --now docker.service

# Le groupe docker équivaut à root : personne ne doit y être (README, #25).
if getent group docker >/dev/null; then
  members="$(getent group docker | cut -d: -f4)"
  if [[ -n "$members" ]]; then
    warn "groupe docker NON vide (${members}) : il équivaut à root, à vider"
  else
    ok "groupe docker vide"
  fi
fi

# ── Secrets (générés une seule fois) ─────────────────────────────────────

step "Mots de passe et fichiers d'environnement"

# Écrit un fichier de secrets s'il manque ; ne régénère JAMAIS un fichier
# valide : un nouveau mot de passe désynchroniserait la base existante.
# Un fichier présent mais sans mot de passe valide (écriture interrompue) est
# traité comme absent.
ensure_secret_file() {
  local path="$1" content="$2" tmp
  if [[ -f "$path" ]] && grep -qE '^POSTGRES_PASSWORD=[0-9a-f]{64}$' "$path"; then
    log "${path} déjà présent : conservé tel quel"
    return 0
  fi
  [[ -f "$path" ]] && warn "${path} présent mais sans mot de passe valide : régénéré"
  if (( DRY_RUN )); then
    dry "créer ${path} (root 600), mot de passe généré :"
    sed -E 's/(PASSWORD=).*/\1<généré, 64 caractères hexadécimaux>/; s/^/            │ /' <<<"$content"
    return 0
  fi
  # Écriture atomique : fichier temporaire (déjà en 600) dans le même dossier,
  # puis renommage. Une interruption ne laisse jamais de fichier vide en place.
  tmp="$(mktemp "${path}.XXXXXX")"
  chmod 600 "$tmp"
  printf '%s\n' "$content" > "$tmp"
  mv -f "$tmp" "$path"
  ok "créé ${path} (root 600)"
}

ensure_secret_file "$POSTGRES_ADMIN_ENV" "\
# Superutilisateur PostgreSQL de prod (#23) — administration uniquement.
POSTGRES_USER=${POSTGRES_ADMIN_USER}
POSTGRES_PASSWORD=$(openssl rand -hex 32)
POSTGRES_DB=${POSTGRES_DB_NAME}"

ensure_secret_file "$POSTGRES_JOBS_ENV" "\
# Connexion des jobs à la base de prod (#23). Lu par systemd (EnvironmentFile=)
# avant le changement d'identité : le compte biowatch-jobs ne peut pas le lire.
POSTGRES_USER=${POSTGRES_JOBS_USER}
POSTGRES_PASSWORD=$(openssl rand -hex 32)
POSTGRES_DB=${POSTGRES_DB_NAME}
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=${POSTGRES_PORT}"

# ── Conteneur et unité ───────────────────────────────────────────────────

step "Conteneur PostgreSQL + PostGIS"
run install -d -m 700 -o root -g root "$POSTGRES_CONF_DIR"
write_file "$COMPOSE_FILE" 600 <"${POSTGRES_SRC}/compose.yaml"
write_file "$UNIT_FILE" 644 <"${POSTGRES_SRC}/${POSTGRES_UNIT}.service"
run systemctl daemon-reload
# enable --now : démarre au premier passage, sans effet ensuite (pas de
# coupure de la base quand on rejoue le script).
run systemctl enable --now "${POSTGRES_UNIT}.service"
# Applique une config modifiée : compose ne recrée le conteneur que si sa
# définition a changé. --wait attend le healthcheck (pg_isready).
run docker compose -f "$COMPOSE_FILE" up -d --wait
done_ok "${POSTGRES_UNIT}.service actif, base prête"

# ── Extensions et rôle des jobs ──────────────────────────────────────────

step "Extensions et rôle ${POSTGRES_JOBS_USER}"

# Lit une variable d'un fichier KEY=value sans le sourcer.
env_value() { sed -n "s/^$2=//p" "$1" | head -n1; }

if (( DRY_RUN )); then
  dry "CREATE EXTENSION postgis, pgcrypto dans ${POSTGRES_DB_NAME}"
  dry "créer ou mettre à jour le rôle ${POSTGRES_JOBS_USER} (LOGIN, mot de passe de ${POSTGRES_JOBS_ENV})"
  dry "GRANT CONNECT sur ${POSTGRES_DB_NAME}, USAGE + CREATE sur le schéma public"
else
  jobs_password="$(env_value "$POSTGRES_JOBS_ENV" POSTGRES_PASSWORD)"
  [[ "$jobs_password" =~ ^[0-9a-f]{64}$ ]] \
    || die "mot de passe inattendu dans ${POSTGRES_JOBS_ENV} (64 hexadécimaux attendus)"
  # Le mot de passe passe par stdin (\set), jamais en argument : il
  # apparaîtrait dans la liste des processus.
  docker exec -i "$POSTGRES_CONTAINER" \
    psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB_NAME" <<SQL
\\set jobs_password '${jobs_password}'
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
SELECT format(
  CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${POSTGRES_JOBS_USER}')
       THEN 'ALTER ROLE %I LOGIN PASSWORD %L'
       ELSE 'CREATE ROLE %I LOGIN PASSWORD %L' END,
  '${POSTGRES_JOBS_USER}', :'jobs_password') \\gexec
GRANT CONNECT ON DATABASE ${POSTGRES_DB_NAME} TO ${POSTGRES_JOBS_USER};
GRANT USAGE, CREATE ON SCHEMA public TO ${POSTGRES_JOBS_USER};
SQL
  ok "extensions postgis + pgcrypto, rôle ${POSTGRES_JOBS_USER} à jour"
fi

# ── Preuves ──────────────────────────────────────────────────────────────

step "Vérifications"
if (( DRY_RUN )); then
  dry "prouver : 5432 lié à 127.0.0.1 uniquement, connexion de ${POSTGRES_JOBS_USER}, PostGIS actif"
else
  # Liaison : aucune écoute 5432 hors boucle locale (ni 0.0.0.0, ni [::]).
  exposed="$(ss -tlnH "( sport = :${POSTGRES_PORT} )" | awk '{print $4}' \
    | grep -v -E '^127\.0\.0\.1:' || true)"
  [[ -z "$exposed" ]] || die "le ${POSTGRES_PORT} écoute hors boucle locale : ${exposed}"
  tcp_probe 127.0.0.1 "$POSTGRES_PORT" || die "aucune connexion sur 127.0.0.1:${POSTGRES_PORT}"
  ok "${POSTGRES_PORT} joignable sur 127.0.0.1, et seulement là"

  # Connexion réelle du rôle des jobs, par le même chemin que l'application :
  # depuis l'hôte, sur le port publié. Pas de « docker exec … psql » : DANS le
  # conteneur, l'image fait confiance à 127.0.0.1 (trust) et accepterait
  # n'importe quel mot de passe — le test ne prouverait rien. Le client psql
  # vient de l'image déjà présente, lancée sur le réseau de l'hôte.
  image="$(docker inspect -f '{{.Config.Image}}' "$POSTGRES_CONTAINER")"
  # Mot de passe transmis par l'environnement (-e sans valeur), jamais en
  # argument : il apparaîtrait dans la liste des processus.
  host_psql() {
    PGPASSWORD="$1" docker run --rm --network host -e PGPASSWORD "$image" \
      psql -h 127.0.0.1 -p "$POSTGRES_PORT" -U "$POSTGRES_JOBS_USER" -d "$POSTGRES_DB_NAME" \
      -tA -c "$2"
  }
  if host_psql "mauvais-mot-de-passe" "SELECT 1" >/dev/null 2>&1; then
    die "connexion acceptée avec un MAUVAIS mot de passe : authentification inopérante"
  fi
  ok "mauvais mot de passe refusé (authentification scram-sha-256 active)"
  result="$(host_psql "$(env_value "$POSTGRES_JOBS_ENV" POSTGRES_PASSWORD)" \
    "SELECT current_user || ' / ' || current_database() || ' / PostGIS ' || postgis_lib_version()")"
  ok "connexion par 127.0.0.1:${POSTGRES_PORT} : ${result}"
fi

# ── Bilan ────────────────────────────────────────────────────────────────

cat <<EOF

${C_GRN}${C_BLD}PostgreSQL + PostGIS en place, sur la boucle locale uniquement.${C_RST}

Depuis ton poste, la preuve que le port reste fermé :

    nc -zv -w5 <IP_DU_VPS> ${POSTGRES_PORT}        # attendu : refusé / délai dépassé

Pour les jobs (#27), dans leur unité systemd :

    EnvironmentFile=${POSTGRES_JOBS_ENV}
    After=${POSTGRES_UNIT}.service
    Wants=${POSTGRES_UNIT}.service

Créer les tables métier, une fois le code déployé par #27 :

    sudo systemctl start biowatch-job@init_db

Administration (psql en superutilisateur) :

    sudo docker exec -it ${POSTGRES_CONTAINER} psql -U ${POSTGRES_ADMIN_USER} -d ${POSTGRES_DB_NAME}

EOF
