# shellcheck shell=bash
# lib.sh — garde-fous communs. Sourcé par les scripts, jamais exécuté.
#
# Fournit : élévation root, dry-run, backups horodatés, restauration
# automatique sur erreur, journalisation, et les vérifications qui
# empêchent de se verrouiller hors du serveur.

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "${LIB_DIR}/config.sh"

DRY_RUN=0
BACKUP_DIR=""
SCRIPT_NAME=""
SCRIPT_PATH=""
USAGE="${USAGE:-}"
declare -a SCRIPT_ARGS=()

# Passé en clair à travers sudo, qui purge l'environnement. Sert d'indice
# complémentaire pour identifier la session courante ; jamais de preuve
# à lui seul.
#
# Nommé par utilisateur invoquant, et non par un chemin fixe : /tmp est en
# sticky bit, donc un fichier laissé par un autre membre serait à la fois
# impossible à écraser et lu tel quel — l'indice porterait sur SA session.
# ${SUDO_USER:-...} donne le même nom avant et après l'élévation.
SSH_CONN_HINT="/tmp/.biowatch-ssh-conn-${SUDO_USER:-$(id -un)}"

# --- sortie ------------------------------------------------------------------

if [[ -t 1 ]]; then
  C_RED=$'\033[0;31m'; C_GRN=$'\033[0;32m'; C_YEL=$'\033[0;33m'
  C_BLU=$'\033[0;34m'; C_BLD=$'\033[1m';    C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_BLD=""; C_RST=""
fi

_ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log()  { printf '%s [  ] %s\n'     "$(_ts)" "$*"; }
ok()   { printf '%s [%sOK%s] %s\n' "$(_ts)" "$C_GRN" "$C_RST" "$*"; }
warn() { printf '%s [%s!!%s] %s\n' "$(_ts)" "$C_YEL" "$C_RST" "$*"; }
err()  { printf '%s [%sKO%s] %s\n' "$(_ts)" "$C_RED" "$C_RST" "$*" >&2; }
dry()  { printf '%s [%sDRY%s] %s\n' "$(_ts)" "$C_BLU" "$C_RST" "$*"; }
step() { printf '\n%s── %s %s\n' "$C_BLD" "$*" "$C_RST"; }
die()  { err "$*"; exit 1; }

banner() {
  # Pas de cadre à largeur fixe : %-60s compte les octets, pas les
  # colonnes, et chaque accent décalerait la bordure droite.
  printf '\n%s%s%s\n' "$C_BLD" "$1" "$C_RST"
  printf '%s%s%s\n\n' "$C_BLD" "$(printf '═%.0s' $(seq 1 60))" "$C_RST"
}

# --- amorçage ----------------------------------------------------------------

init_script() {
  local src="$1"; shift
  SCRIPT_PATH="$(cd "$(dirname "$src")" && pwd)/$(basename "$src")"
  SCRIPT_NAME="$(basename "$src")"
  SCRIPT_ARGS=("$@")

  local a
  for a in "$@"; do
    case "$a" in
      --dry-run) DRY_RUN=1 ;;
      -h|--help) printf '%s\n' "$USAGE"; exit 0 ;;
      *) die "argument inconnu : $a — voir --help" ;;
    esac
  done

  if (( DRY_RUN )); then
    warn "MODE DRY-RUN — rien ne sera modifié sur ce serveur."
  fi
  return 0
}

# Se relance sous root. SSH_CONNECTION ne survivant pas à sudo (env_reset),
# on le dépose sur disque avant l'élévation plutôt que de dépendre d'une
# politique sudoers permissive qui ferait échouer l'appel.
require_root() {
  if [[ $EUID -ne 0 ]]; then
    printf '%s' "${SSH_CONNECTION:-}" > "$SSH_CONN_HINT" 2>/dev/null || true
    chmod 600 "$SSH_CONN_HINT" 2>/dev/null || true
    log "élévation via sudo…"
    exec sudo bash "$SCRIPT_PATH" "${SCRIPT_ARGS[@]}"
  fi
}

init_log() {
  (( DRY_RUN )) && return 0
  touch "$LOG_FILE" 2>/dev/null || true
  chmod 640 "$LOG_FILE" 2>/dev/null || true
  exec > >(tee -a "$LOG_FILE") 2>&1
  log "── ${SCRIPT_NAME} lancé par ${SUDO_USER:-${USER:-root}} ──"
}

# --- backups et restauration -------------------------------------------------

ensure_backup_dir() {
  [[ -n "$BACKUP_DIR" ]] && return 0
  BACKUP_DIR="${BACKUP_ROOT}/$(date '+%Y%m%dT%H%M%S')"
  # Un dry-run qui crée un répertoire n'est plus un dry-run. Le chemin est
  # tout de même calculé : il apparaît dans le script de restauration
  # affiché, qui doit refléter ce qui serait réellement écrit.
  if (( DRY_RUN )); then
    dry "créer ${BACKUP_DIR}"
    return 0
  fi
  mkdir -p "${BACKUP_DIR}"
  chmod 700 "$BACKUP_ROOT" "$BACKUP_DIR"
  : > "${BACKUP_DIR}/.created"
  log "backups → ${BACKUP_DIR}"
}

# Sauvegarde un fichier avant modification. Les fichiers qui n'existaient
# pas sont notés dans .created pour être SUPPRIMÉS à la restauration —
# une simple copie inverse ne les enlèverait pas.
backup_file() {
  local path="$1"
  (( DRY_RUN )) && return 0
  ensure_backup_dir
  if [[ -e "$path" ]]; then
    local dest="${BACKUP_DIR}${path}"
    mkdir -p "$(dirname "$dest")"
    cp -a "$path" "$dest"
  else
    printf '%s\n' "$path" >> "${BACKUP_DIR}/.created"
  fi
}

restore_backups() {
  [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]] || { warn "aucun backup à restaurer"; return 0; }
  warn "restauration depuis ${BACKUP_DIR}"

  local f
  while IFS= read -r f; do
    [[ -n "$f" ]] && rm -f "$f" && log "supprimé (créé par ce script) : $f"
  done < "${BACKUP_DIR}/.created"

  # Rejoue l'arborescence sauvegardée par-dessus la racine.
  ( cd "$BACKUP_DIR" && find . -path ./.created -prune -o -type f -print ) \
    | sed 's|^\./||' \
    | while IFS= read -r rel; do
        [[ -n "$rel" ]] || continue
        cp -a "${BACKUP_DIR}/${rel}" "/${rel}"
        log "restauré : /${rel}"
      done

  systemctl daemon-reload || true
  systemctl restart ssh.socket 2>/dev/null || true
  systemctl try-restart ssh.service 2>/dev/null || true
  warn "restauration terminée"
}

# set -E propage le trap dans les fonctions ; sans lui une erreur au fond
# d'un appel sortirait sans jamais restaurer quoi que ce soit.
enable_error_trap() {
  set -Eeuo pipefail
  trap '_on_err $? $LINENO' ERR
}

_on_err() {
  local code="$1" line="$2"
  err "échec (code ${code}) à la ligne ${line} de ${SCRIPT_NAME}"
  restore_backups || true
  err "état restauré au mieux — relire ${LOG_FILE} avant de rejouer"
  exit "$code"
}

# --- exécution ---------------------------------------------------------------

run() {
  if (( DRY_RUN )); then
    # printf %q préserve les arguments contenant des espaces : sans lui
    # --comment "BioWatch simonr" s'affiche en deux arguments distincts et
    # la ligne montrée n'est pas celle qui serait exécutée.
    local quoted; quoted="$(printf '%q ' "$@")"
    dry "${quoted% }"
    return 0
  fi
  "$@"
}

# Confirme un changement d'état. Muet en dry-run, où annoncer « créé »
# serait faux : la ligne [DRY] précédente dit déjà ce qui se passerait.
done_ok() {
  (( DRY_RUN )) || ok "$@"
}

# Écrit un fichier depuis stdin, après backup, avec mode explicite.
write_file() {
  local path="$1" mode="$2" content
  content="$(cat)"

  if (( DRY_RUN )); then
    dry "écrire ${path} (mode ${mode}) :"
    sed 's/^/            │ /' <<<"$content"
    return 0
  fi

  backup_file "$path"
  install -D -m "$mode" /dev/null "$path"
  printf '%s\n' "$content" > "$path"
  ok "écrit ${path} (mode ${mode})"
}

# --- vérifications réseau ----------------------------------------------------

# Ouvre une vraie connexion TCP. /dev/tcp est une primitive bash : pas de
# dépendance à nc, absent des images minimales. timeout évite de pendre
# sur un port filtré ; son absence ne doit pas faire échouer la sonde,
# sans quoi la vérification deviendrait un refus systématique.
tcp_probe() {
  local host="$1" port="$2"
  if command -v timeout >/dev/null 2>&1; then
    timeout 3 bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null
  else
    bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null
  fi
}

# Attend que sshd écoute, PUIS prouve qu'une connexion TCP aboutit.
#
# La présence d'un socket dans ss ne prouve rien : un socket IPv6 non
# dual-stack s'y affiche exactement comme un socket joignable, et laisse
# croire au succès alors que tout client IPv4 est refusé. On ouvre donc
# une vraie connexion sur la boucle locale, dans les deux familles.
assert_listening() {
  local port="$1" tries=20
  if (( DRY_RUN )); then dry "vérifier l'écoute et la joignabilité sur ${port}"; return 0; fi

  while (( tries-- > 0 )); do
    ss -tlnH "( sport = :${port} )" 2>/dev/null | grep -q . && break
    sleep 0.5
  done
  ss -tlnH "( sport = :${port} )" 2>/dev/null | grep -q . \
    || die "aucun socket en écoute sur le port ${port}"

  if ! tcp_probe 127.0.0.1 "$port"; then
    err "socket présent sur ${port} mais AUCUNE connexion IPv4 n'aboutit"
    err "cause typique : ListenStream en port nu → socket IPv6 seul"
    ss -tlnH "( sport = :${port} )" | sed 's/^/            /' >&2
    die "port ${port} injoignable en IPv4"
  fi
  ok "port ${port} joignable en IPv4"

  if tcp_probe ::1 "$port"; then
    ok "port ${port} joignable en IPv6"
  else
    warn "port ${port} non joignable en IPv6 (acceptable si le VPS n'a pas d'IPv6)"
  fi
}

assert_not_listening() {
  local port="$1"
  if (( DRY_RUN )); then dry "vérifier l'absence d'écoute sur ${port}"; return 0; fi
  if ss -tlnH "( sport = :${port} )" 2>/dev/null | grep -q .; then
    die "le port ${port} écoute encore alors qu'il devrait être fermé"
  fi
  ok "port ${port} fermé"
}

# Refuse d'avancer sans PREUVE qu'une session passe déjà par le nouveau
# port. Une session ouverte avant le changement survit au redémarrage de
# sshd : « ça marche encore dans mon terminal » ne prouve rien.
require_session_on_port() {
  local port="$1" conns hint=""
  if (( DRY_RUN )); then dry "vérifier une session établie sur ${port}"; return 0; fi

  conns="$(ss -tnH state established "( sport = :${port} )" 2>/dev/null || true)"

  if [[ -z "$conns" ]]; then
    err "Aucune connexion établie sur le port ${port}."
    cat >&2 <<EOF

  Ce script retire des accès. Il refuse de s'exécuter tant qu'il n'a pas
  constaté lui-même que le nouveau chemin fonctionne.

  Depuis un SECOND terminal, sur ton poste (pas sur le VPS) :

      ssh -p ${port} ${MEMBERS[0]}@<IP_DU_VPS>

  Garde cette session ouverte, puis relance ce script DEPUIS ELLE.

EOF
    die "preuve d'accès manquante — aucun accès n'a été retiré."
  fi

  ok "connexion(s) établie(s) sur le port ${port} :"
  sed 's/^/            /' <<<"$conns"

  [[ -r "$SSH_CONN_HINT" ]] && hint="$(cat "$SSH_CONN_HINT" 2>/dev/null || true)"
  if [[ -n "$hint" ]]; then
    local srv_port; srv_port="$(awk '{print $4}' <<<"$hint")"
    if [[ "$srv_port" == "$port" ]]; then
      ok "la session qui lance ce script passe bien par ${port}"
    else
      warn "la session qui lance ce script semble passer par le port ${srv_port:-?}"
      warn "une autre session utilise ${port} — vérifie que c'est bien la tienne ci-dessus"
    fi
  else
    warn "session courante non identifiable — relis la liste ci-dessus avant de continuer"
  fi
}

# --- systemd -----------------------------------------------------------------

reload_ssh_socket() {
  run systemctl daemon-reload
  run systemctl restart ssh.socket
  # ssh.service détient le descripteur hérité du socket : sans ce
  # redémarrage il continue de servir l'ancien port.
  run systemctl try-restart ssh.service
}

# Arme une restauration différée. Appelée AVANT l'opération risquée :
# armée après, elle ne protégerait rien si l'opération coupe l'accès.
arm_deadman() {
  local unit="$1" script="$2"
  if (( DRY_RUN )); then
    dry "armer ${unit}.timer (déclenchement dans ${ROLLBACK_DELAY})"
    return 0
  fi
  systemctl stop "${unit}.timer" 2>/dev/null || true
  systemd-run --quiet \
    --unit="$unit" \
    --on-active="$ROLLBACK_DELAY" \
    --description="Restauration automatique BioWatch si non confirmée" \
    "$script"
  # Heure limite explicite : « dans 10min » oblige à calculer de tête au
  # moment précis où l'on a le moins envie de se tromper.
  local mins deadline
  mins="${ROLLBACK_DELAY%min}"
  deadline="$(date -d "+${mins} minutes" '+%H:%M:%S' 2>/dev/null || echo '?')"
  ok "filet armé : restauration automatique à ${deadline} (dans ${ROLLBACK_DELAY})"
}

announce_deadman() {
  local unit="$1" test_cmd="$2"
  # Pas de cadre : la largeur se calcule en octets, les accents la faussent.
  cat <<EOF

${C_YEL}${C_BLD}▄▄▄ ACTION REQUISE DANS LES ${ROLLBACK_DELAY} ▄▄▄${C_RST}

  1. Depuis un NOUVEAU terminal sur ton poste :

         ${test_cmd}

  2. Seulement si ça fonctionne, désarme le filet :

         sudo systemctl stop ${unit}.timer

  Sans cette confirmation, la configuration précédente sera restaurée
  automatiquement et tu récupéreras l'accès. Ne fais rien d'autre en
  attendant.

  Note : « Unit not loaded » au désarmement signifie que le minuteur
  n'existe plus — soit il a déjà été arrêté, soit il s'est déclenché.
  Pour lever le doute :  journalctl -t biowatch-rollback -n 20

EOF
}
