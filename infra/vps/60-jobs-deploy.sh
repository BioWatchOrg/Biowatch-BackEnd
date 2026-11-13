#!/usr/bin/env bash
# 60-jobs-deploy.sh — issue #27.
#
# Prépare le VPS à recevoir les déploiements des jobs par la CI. À lancer une
# fois par un membre (puis à rejouer si ce script ou config.sh change) :
#
#   biowatch-ci     compte de la CI : une clé à commande forcée, deux règles sudo
#   uv              épinglé, pour construire les releases
#   releases/       une release par commit, app → releases/<sha>
#   biowatch-job@   unité systemd unique des jobs (identité, bac à sable)
#   jobs.env        réglages prod des jobs, lus par systemd — posé s'il manque
#                   (les identifiants DB sont dans db-jobs.env, posé par 55-postgres.sh)
#
# Ce script ne modifie pas sshd : le groupe CI doit être ajouté à AllowGroups
# en rejouant 20-harden-ssh.sh (filet armé). Il le vérifie et le signale.
#
#   ./60-jobs-deploy.sh --dry-run
#   ./60-jobs-deploy.sh

USAGE="Usage: ./60-jobs-deploy.sh [--dry-run]

Installe le compte CI, uv, les scripts de déploiement et l'unité des jobs,
puis PROUVE les droits du compte CI en testant ses accès. Idempotent. Aucun
accès SSH existant n'est modifié.

  --dry-run   affiche les changements sans les appliquer"

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
init_script "${BASH_SOURCE[0]}" "$@"
require_root
init_log
enable_error_trap

banner "#27 — Déploiement des jobs (CI → systemd)"

JOBS_ACCOUNT="biowatch-jobs"
short_name() { printf '%s' "${1#biowatch-}"; }
# DEPLOY_HOME et JOBS_LOG_DIR ne sont lues que par render (${!v}).
# shellcheck disable=SC2034
DEPLOY_HOME="${SERVICE_STATE_ROOT}/$(short_name "$DEPLOY_ACCOUNT")"
CI_HOME="${SERVICE_STATE_ROOT}/$(short_name "$CI_ACCOUNT")"
JOBS_STATE_DIR="${SERVICE_STATE_ROOT}/$(short_name "$JOBS_ACCOUNT")"
# shellcheck disable=SC2034
JOBS_LOG_DIR="${SERVICE_LOG_ROOT}/$(short_name "$JOBS_ACCOUNT")"
CI_KEY_PATH="${LIB_DIR}/${CI_KEY_FILE}"
INFRA_ROOT="$(cd "${LIB_DIR}/.." && pwd)"
CI_DEPLOY_BIN="${SBIN_DIR}/biowatch-ci-deploy"
RELEASE_BIN="${SBIN_DIR}/biowatch-release"
INSTALL_TIMERS_BIN="${SBIN_DIR}/biowatch-install-timers"

# Valeurs injectées dans les scripts et l'unité (@VAR@ → valeur).
TEMPLATE_VARS=(
  APP_DIR RELEASES_DIR APP_CURRENT PYTHON_DIR RELEASES_KEPT UV_BIN
  SERVICE_GROUP DEPLOY_ACCOUNT DEPLOY_HOME CI_HOME SBIN_DIR
  JOBS_UNIT_TEMPLATE JOBS_ENV_FILE JOBS_DB_ENV_FILE JOBS_STATE_DIR JOBS_LOG_DIR
)

# ── Préconditions ────────────────────────────────────────────────────────

step "Vérification des préconditions"

for cmd in curl tar sha256sum flock systemd-analyze visudo ssh-keygen runuser logger tac; do
  command -v "$cmd" >/dev/null 2>&1 || die "${cmd} introuvable"
done
# Les fréquences s'écrivent en heure de Paris : sans tzdata, systemd refuse
# tout OnCalendar qui nomme un fuseau, et chaque déploiement échouerait.
[[ -d /usr/share/zoneinfo/Europe ]] || die "tzdata absent : sudo apt install tzdata"
ok "outils disponibles"

for u in "$DEPLOY_ACCOUNT" "$JOBS_ACCOUNT"; do
  id "$u" >/dev/null 2>&1 || die "compte ${u} absent — lancer d'abord 40-service-users.sh"
done
[[ -d "$APP_DIR" && "$(stat -c '%U' "$APP_DIR")" == "$DEPLOY_ACCOUNT" ]] \
  || die "${APP_DIR} absent ou n'appartient pas à ${DEPLOY_ACCOUNT} — lancer 40-service-users.sh"
ok "comptes et répertoires de #25 en place"

# Un ancien déploiement manuel aurait pu poser app/ en vrai répertoire : le
# remplacer par un lien perdrait son contenu sans prévenir.
if [[ -e "$APP_CURRENT" && ! -L "$APP_CURRENT" ]]; then
  die "${APP_CURRENT} existe et n'est pas un lien symbolique — le déplacer avant de continuer"
fi

[[ -f "$CI_KEY_PATH" ]] || die "${CI_KEY_FILE} absent — voir keys/ci/README.md"
grep -q 'PRIVATE KEY' "$CI_KEY_PATH" \
  && die "${CI_KEY_FILE} contient une CLÉ PRIVÉE — la révoquer et la régénérer"
[[ "$(grep -c . "$CI_KEY_PATH")" -eq 1 ]] || die "${CI_KEY_FILE} doit contenir exactement une clé"
key_info="$(ssh-keygen -l -f "$CI_KEY_PATH" 2>/dev/null)" || die "${CI_KEY_FILE} illisible"
[[ "$key_info" == *"(ED25519)"* ]] || die "${CI_KEY_FILE} : ed25519 attendu (${key_info})"
ok "clé CI : ${key_info}"

# ── uv ───────────────────────────────────────────────────────────────────

install_uv() {
  step "uv ${UV_VERSION}"
  if [[ -x "$UV_BIN" ]] && [[ "$("$UV_BIN" --version 2>/dev/null)" == "uv ${UV_VERSION}"* ]]; then
    log "uv ${UV_VERSION} déjà installé"
    return 0
  fi

  local arch asset base tmp
  case "$(uname -m)" in
    x86_64)  arch="x86_64" ;;
    aarch64) arch="aarch64" ;;
    *) die "architecture $(uname -m) non prévue" ;;
  esac
  asset="uv-${arch}-unknown-linux-gnu"
  base="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"

  if (( DRY_RUN )); then
    dry "télécharger ${base}/${asset}.tar.gz, vérifier son sha256, installer ${UV_BIN}"
    return 0
  fi

  tmp="$(mktemp -d)"
  curl -fsSL --proto '=https' -o "${tmp}/${asset}.tar.gz" "${base}/${asset}.tar.gz"
  curl -fsSL --proto '=https' -o "${tmp}/${asset}.tar.gz.sha256" "${base}/${asset}.tar.gz.sha256"
  ( cd "$tmp" && sha256sum -c --quiet "${asset}.tar.gz.sha256" ) \
    || die "somme de contrôle de uv invalide — téléchargement corrompu"
  tar -xzf "${tmp}/${asset}.tar.gz" -C "$tmp"
  install -m 755 -o root -g root "${tmp}/${asset}/uv" "$UV_BIN"
  rm -rf -- "$tmp"
  ok "$("$UV_BIN" --version) installé dans ${UV_BIN}"
}

# ── Compte CI ────────────────────────────────────────────────────────────

ensure_ci_account() {
  step "Compte ${CI_ACCOUNT}"
  local uid

  if uid="$(id -u "$CI_ACCOUNT" 2>/dev/null)"; then
    (( uid < 1000 )) || die "${CI_ACCOUNT} existe déjà comme compte HUMAIN (uid ${uid})"
    log "compte ${CI_ACCOUNT} déjà présent (uid ${uid})"
  else
    local group_opt=(--user-group)
    getent group "$CI_ACCOUNT" >/dev/null && group_opt=(--gid "$CI_ACCOUNT")
    # Un VRAI shell : sshd lance la commande forcée via le shell du compte,
    # nologin la refuserait. La clé (restrict,command=) interdit toute session.
    run useradd --system "${group_opt[@]}" \
      --shell /bin/sh --home-dir "$CI_HOME" --no-create-home \
      --comment "BioWatch CI deploy" "$CI_ACCOUNT"
    done_ok "compte ${CI_ACCOUNT} créé"
  fi

  if ! getent group "$SSH_CI_GROUP" >/dev/null; then
    run groupadd --system "$SSH_CI_GROUP"
  fi
  run usermod --append --groups "$SSH_CI_GROUP" "$CI_ACCOUNT"
  if [[ "$(getent passwd "$CI_ACCOUNT" | cut -d: -f7 || true)" != "/bin/sh" ]]; then
    run usermod --shell /bin/sh "$CI_ACCOUNT"
  fi
  # « * » : aucun mot de passe valide, sans être « verrouillé » au sens de
  # sshd (« ! »), qui refuserait aussi la clé quand UsePAM est désactivé.
  run usermod --password '*' "$CI_ACCOUNT"
  run install -d -m 750 -o "$CI_ACCOUNT" -g "$CI_ACCOUNT" "$CI_HOME"
  done_ok "${CI_HOME}  ${CI_ACCOUNT} 750"
}

install_ci_key() {
  step "Clé SSH de la CI"
  local ak="${CI_HOME}/.ssh/authorized_keys" tmp

  if (( DRY_RUN )); then
    dry "installer ${CI_KEY_FILE} → ${ak} (root:${CI_ACCOUNT} 640)"
    dry "  préfixe : restrict,command=\"${CI_DEPLOY_BIN}\""
    return 0
  fi

  # Fichier et dossier à root : le compte CI ne peut ni ajouter une clé, ni
  # retirer la restriction de la sienne.
  install -d -m 750 -o root -g "$CI_ACCOUNT" "${CI_HOME}/.ssh"
  tmp="$(mktemp)"
  printf 'restrict,command="%s" %s\n' "$CI_DEPLOY_BIN" "$(cat "$CI_KEY_PATH")" > "$tmp"
  backup_file "$ak"
  install -m 640 -o root -g "$CI_ACCOUNT" "$tmp" "$ak"
  rm -f -- "$tmp"
  ok "clé installée, limitée à ${CI_DEPLOY_BIN}"
}

# ── Répertoires du code ──────────────────────────────────────────────────

ensure_release_dirs() {
  step "Répertoires des releases"
  local d
  # deploy doit être membre du groupe de service pour en garder le setgid :
  # le noyau l'efface au moindre chmod d'un propriétaire hors du groupe, et
  # tar comme uv en font. Aucun droit en plus : seul APP_DIR, qui appartient
  # déjà à deploy, utilise ce groupe.
  run usermod --append --groups "$SERVICE_GROUP" "$DEPLOY_ACCOUNT"
  done_ok "${DEPLOY_ACCOUNT} dans ${SERVICE_GROUP} (droits du code déployé)"
  for d in "$RELEASES_DIR" "$PYTHON_DIR"; do
    run install -d -m 2750 -o "$DEPLOY_ACCOUNT" -g "$SERVICE_GROUP" "$d"
    # Le bit setgid n'est pas posé par install -d sur toutes les versions.
    run chmod 2750 "$d"
    done_ok "${d}  ${DEPLOY_ACCOUNT}:${SERVICE_GROUP} 2750"
  done
}

# ── Scripts, sudo, unité ─────────────────────────────────────────────────

render() {
  local src="$1" v args=()
  for v in "${TEMPLATE_VARS[@]}"; do
    args+=(-e "s|@${v}@|${!v}|g")
  done
  sed "${args[@]}" "$src"
}

# install_rendered <source> <destination> <mode>
install_rendered() {
  local src="$1" dest="$2" mode="$3" tmp left
  tmp="$(mktemp)"
  render "$src" > "$tmp"
  left="$(grep -oE '@[A-Z_]+@' "$tmp" | sort -u | tr '\n' ' ' || true)"
  if [[ -n "$left" ]]; then
    rm -f -- "$tmp"
    die "$(basename "$src") : variable(s) non remplacée(s) : ${left}"
  fi
  if (( DRY_RUN )); then
    rm -f -- "$tmp"
    dry "installer ${dest} (root:root ${mode}) depuis ${src#"${INFRA_ROOT}"/}"
    return 0
  fi
  if cmp -s "$tmp" "$dest"; then
    rm -f -- "$tmp"
    log "${dest} à jour"
    return 0
  fi
  backup_file "$dest"
  install -m "$mode" -o root -g root "$tmp" "$dest"
  rm -f -- "$tmp"
  ok "écrit ${dest}"
}

install_scripts() {
  step "Scripts de déploiement"
  install_rendered "${LIB_DIR}/bin/biowatch-ci-deploy"       "$CI_DEPLOY_BIN"      755
  install_rendered "${LIB_DIR}/bin/biowatch-release"         "$RELEASE_BIN"        755
  install_rendered "${LIB_DIR}/bin/biowatch-install-timers"  "$INSTALL_TIMERS_BIN" 755
}

install_sudoers() {
  step "Droits sudo du compte CI"
  local tmp
  tmp="$(mktemp)"
  # Le "" final interdit tout argument : le manifeste arrive sur stdin, rien
  # d'autre ne peut être passé à un script qui tourne en root.
  cat > "$tmp" <<EOF
# Généré par infra/vps/60-jobs-deploy.sh — issue #27.
# Seuls droits du compte CI : construire/activer une release en tant que
# ${DEPLOY_ACCOUNT}, et synchroniser les timers des jobs en root.
${CI_ACCOUNT} ALL=(${DEPLOY_ACCOUNT}) NOPASSWD: ${RELEASE_BIN}
${CI_ACCOUNT} ALL=(root) NOPASSWD: ${INSTALL_TIMERS_BIN} ""
EOF
  visudo -cqf "$tmp" || { rm -f -- "$tmp"; die "règle sudoers invalide"; }

  if (( DRY_RUN )); then
    dry "installer ${SUDOERS_CI_FILE} (440) :"
    sed 's/^/            │ /' "$tmp"
    rm -f -- "$tmp"
    return 0
  fi
  if cmp -s "$tmp" "$SUDOERS_CI_FILE"; then
    rm -f -- "$tmp"
    log "${SUDOERS_CI_FILE} à jour"
    return 0
  fi
  backup_file "$SUDOERS_CI_FILE"
  install -m 440 -o root -g root "$tmp" "$SUDOERS_CI_FILE"
  rm -f -- "$tmp"
  ok "écrit ${SUDOERS_CI_FILE}"
}

install_unit() {
  step "Unité systemd des jobs"
  install_rendered "${INFRA_ROOT}/systemd/biowatch-job@.service" "$JOBS_UNIT_TEMPLATE" 644
  run systemctl daemon-reload
}

ensure_jobs_env() {
  step "Configuration prod (${JOBS_ENV_FILE}, ${JOBS_DB_ENV_FILE})"
  if [[ ! -f "$JOBS_ENV_FILE" ]]; then
    run install -m 600 -o root -g root "${INFRA_ROOT}/systemd/jobs.env.example" "$JOBS_ENV_FILE"
    done_ok "${JOBS_ENV_FILE} créé depuis le modèle"
  else
    # Jamais réécrit : il a pu être ajusté à la main. On ne corrige que ses droits.
    run chown root:root "$JOBS_ENV_FILE"
    run chmod 600 "$JOBS_ENV_FILE"
    grep -qx "BIOWATCH_DATA_ROOT=${JOBS_STATE_DIR}" "$JOBS_ENV_FILE" \
      || warn "BIOWATCH_DATA_ROOT=${JOBS_STATE_DIR} absent de ${JOBS_ENV_FILE} (le code est en lecture seule)"
    grep -q '^POSTGRES_' "$JOBS_ENV_FILE" \
      && warn "${JOBS_ENV_FILE} contient des POSTGRES_* : ignorés (écrasés par ${JOBS_DB_ENV_FILE}), à retirer"
    grep -q '^DATABASE_URL=' "$JOBS_ENV_FILE" \
      && warn "${JOBS_ENV_FILE} contient DATABASE_URL : elle prime sur ${JOBS_DB_ENV_FILE}, à retirer"
    ok "${JOBS_ENV_FILE} présent (root 600)"
  fi

  # Propriété de #23 : lu, jamais créé ni modifié ici. Sans lui, chaque job
  # échoue au démarrage (EnvironmentFile= obligatoire), ce qui est voulu.
  if [[ -f "$JOBS_DB_ENV_FILE" ]]; then
    ok "${JOBS_DB_ENV_FILE} présent (identifiants DB, #23)"
  else
    warn "${JOBS_DB_ENV_FILE} absent : lancer 55-postgres.sh (#23) — les jobs ne démarreront pas sans lui"
  fi
}

# ── Preuves ──────────────────────────────────────────────────────────────
# Comme 40-service-users.sh : on teste les accès sous l'identité du compte,
# plutôt que de lire des droits (ACL, umask, groupes peuvent les contredire).

FAILURES=0
fail() { err "$*"; FAILURES=$((FAILURES + 1)); }

can() {
  local u="$1" op="$2" dir="$3" probe
  case "$op" in
    read)  runuser -u "$u" -- ls -- "$dir" >/dev/null 2>&1 ;;
    write)
      probe="$(runuser -u "$u" -- mktemp -p "$dir" .biowatch-probe.XXXXXX 2>/dev/null)" \
        || return 1
      rm -f -- "$probe"
      ;;
  esac
}
expect_cannot() { if can "$1" "$2" "$3"; then fail "${1} ne devrait PAS pouvoir ${4}"; else ok "${1} ne peut pas ${4}"; fi; }

verify() {
  step "Preuves (tests exécutés sous l'identité de ${CI_ACCOUNT})"
  if (( DRY_RUN )); then
    dry "vérifier groupes, sudo, clé et accès de ${CI_ACCOUNT}, et l'absence de sudo pour ${DEPLOY_ACCOUNT}"
    return 0
  fi
  set +e
  trap - ERR

  local g forbidden="" sudo_out rules expected
  for g in $(id -nG "$CI_ACCOUNT"); do
    if [[ " ${SERVICE_FORBIDDEN_GROUPS[*]/$SSH_CI_GROUP/} ${SERVICE_GROUP} " == *" ${g} "* ]]; then
      forbidden+="${g} "
    fi
  done
  if [[ -z "$forbidden" ]]; then
    ok "${CI_ACCOUNT} : groupes $(id -nG "$CI_ACCOUNT")"
  else
    fail "${CI_ACCOUNT} : membre de ${forbidden}— à retirer"
  fi

  expect_cannot "$CI_ACCOUNT" read  "$APP_DIR"            "lire ${APP_DIR}"
  expect_cannot "$CI_ACCOUNT" read  "$SERVICE_CONFIG_DIR" "lire ${SERVICE_CONFIG_DIR}"
  expect_cannot "$CI_ACCOUNT" read  "$JOBS_STATE_DIR"     "lire l'état des jobs"
  expect_cannot "$CI_ACCOUNT" write "${CI_HOME}/.ssh"     "modifier ses clés SSH"

  # Exactement les deux règles attendues, rien de plus.
  sudo_out="$(LC_ALL=C sudo -n -l -U "$CI_ACCOUNT" 2>&1 || true)"
  rules="$(sed -n 's/^[[:space:]]*(\(.*\)) NOPASSWD: \(.*\)$/(\1) \2/p' <<<"$sudo_out" | sort)"
  expected="$(printf '(%s) %s\n(root) %s ""\n' "$DEPLOY_ACCOUNT" "$RELEASE_BIN" "$INSTALL_TIMERS_BIN" | sort)"
  if [[ "$rules" == "$expected" ]] && ! grep -q '(ALL' <<<"$sudo_out"; then
    ok "${CI_ACCOUNT} : sudo limité à ${RELEASE_BIN##*/} (${DEPLOY_ACCOUNT}) et ${INSTALL_TIMERS_BIN##*/} (root)"
  else
    fail "${CI_ACCOUNT} : droits sudo inattendus — voir « sudo -l -U ${CI_ACCOUNT} »"
    sed 's/^/            /' <<<"$sudo_out" >&2
  fi

  # Les garanties de #25 sur deploy tiennent toujours.
  if grep -q 'not allowed to run sudo' <<<"$(LC_ALL=C sudo -n -l -U "$DEPLOY_ACCOUNT" 2>&1 || true)"; then
    ok "${DEPLOY_ACCOUNT} : toujours aucun droit sudo"
  else
    fail "${DEPLOY_ACCOUNT} : dispose de droits sudo — contraire à #25"
  fi

  if grep -q "^restrict,command=\"${CI_DEPLOY_BIN}\" " "${CI_HOME}/.ssh/authorized_keys"; then
    ok "clé CI restreinte à ${CI_DEPLOY_BIN}"
  else
    fail "clé CI sans restriction dans ${CI_HOME}/.ssh/authorized_keys"
  fi

  if sshd -T 2>/dev/null | grep -qix "allowgroups ${SSH_CI_GROUP}"; then
    ok "sshd : AllowGroups admet ${SSH_CI_GROUP}"
  else
    warn "sshd : ${SSH_CI_GROUP} absent de AllowGroups — la CI sera refusée."
    warn "  Rejouer 20-harden-ssh.sh (il arme son filet) pour l'ajouter."
  fi

  # Dépendance système d'un job (#35), pas du déploiement : on prévient seulement.
  command -v osm2pgsql >/dev/null \
    || warn "osm2pgsql absent : osm_import_scheduled échouera (sudo apt install osm2pgsql)"

  enable_error_trap
  (( FAILURES == 0 )) || die "${FAILURES} vérification(s) en échec — droits NON conformes"
  ok "droits du compte CI conformes"
}

# ── Déroulé ──────────────────────────────────────────────────────────────

install_uv
ensure_ci_account
install_ci_key
ensure_release_dirs
install_scripts
install_sudoers
install_unit
ensure_jobs_env
verify

cat <<EOF

${C_GRN}${C_BLD}VPS prêt à recevoir les déploiements des jobs.${C_RST}

Reste à faire (voir « Déploiement des jobs » dans infra/vps/README.md) :

  1. Secrets GitHub (environnement « production ») : VPS_HOST, VPS_SSH_PORT,
     VPS_KNOWN_HOSTS, VPS_DEPLOY_KEY (clé PRIVÉE de ${CI_KEY_FILE}).
  2. Base prod : 55-postgres.sh (#23) doit avoir posé ${JOBS_DB_ENV_FILE}.
  3. Lancer le workflow « Deploy jobs » (Actions → Run workflow, branche main),
     puis initialiser le schéma : sudo systemctl start biowatch-job@init_db

EOF
