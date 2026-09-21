#!/usr/bin/env bash
# 10-users-and-keys.sh — issue #22, phase ADDITIVE.
#
# Crée les comptes membres, installe leurs clés publiques, accorde sudo,
# et ouvre le port cible EN PLUS du port 22.
#
# Ce script ne retire RIEN : le port 22 et l'authentification par mot de
# passe restent actifs. Il ne peut pas te verrouiller dehors. C'est
# 20-harden-ssh.sh qui ferme les accès, et seulement après preuve.
#
#   ./10-users-and-keys.sh --dry-run    # toujours en premier
#   ./10-users-and-keys.sh

USAGE="Usage: ./10-users-and-keys.sh [--dry-run]

Crée les comptes, installe les clés, ouvre le port ${SSH_PORT:-cible} en
plus du 22. Purement additif : aucun accès n'est retiré.

  --dry-run   affiche les changements sans les appliquer"

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
init_script "${BASH_SOURCE[0]}" "$@"
require_root
init_log
enable_error_trap

banner "#22 — Comptes, clés et ouverture du port ${SSH_PORT}"

# ── Validation des clés ──────────────────────────────────────────────────
# Tout est vérifié AVANT la moindre écriture. Une clé tronquée découverte
# à mi-parcours laisserait des comptes à moitié configurés.

validate_keys() {
  step "Validation des clés publiques"
  local bad=0 m kf line type bits

  for m in "${MEMBERS[@]}"; do
    kf="${LIB_DIR}/keys/${m}.pub"

    if [[ ! -f "$kf" ]]; then
      err "keys/${m}.pub absent"
      bad=1; continue
    fi

    # Garde-fou contre l'erreur irrattrapable : une clé PRIVÉE commitée.
    if grep -q 'PRIVATE KEY' "$kf"; then
      err "keys/${m}.pub contient une CLÉ PRIVÉE — à révoquer immédiatement"
      bad=1; continue
    fi

    if ! line="$(ssh-keygen -l -f "$kf" 2>/dev/null)"; then
      err "keys/${m}.pub illisible ou malformé"
      bad=1; continue
    fi

    bits="$(awk '{print $1}' <<<"$line")"
    type="$(sed -n 's/.*(\([A-Z0-9]*\))[[:space:]]*$/\1/p' <<<"$line")"
    case "$type" in
      ED25519) ok "${m} : ${line}" ;;
      RSA)
        if (( bits < 3072 )); then
          err "${m} : clé RSA de ${bits} bits, trop courte (minimum 3072)"
          bad=1
        else
          warn "${m} : RSA ${bits} bits accepté, ed25519 préférable"
        fi
        ;;
      *) warn "${m} : type ${type:-inconnu} inhabituel — ${line}" ;;
    esac
  done

  # Deux membres partageant une clé, c'est presque toujours quelqu'un qui a
  # renvoyé la clé reçue au lieu de la sienne. Les actions deviennent alors
  # inattribuables, et le compte de l'un ouvre celui de l'autre.
  local dupes
  dupes="$(awk '{print $2}' "${LIB_DIR}"/keys/*.pub 2>/dev/null | sort | uniq -d)"
  if [[ -n "$dupes" ]]; then
    err "clé publique identique partagée par plusieurs membres :"
    sed 's/^/            /' <<<"$dupes" >&2
    bad=1
  else
    ok "empreintes toutes distinctes"
  fi

  if [[ -n "$BREAKGLASS_ACCOUNT" ]]; then
    if [[ ! -f "${LIB_DIR}/keys/${BREAKGLASS_KEY_OWNER}.pub" ]]; then
      err "compte de secours '${BREAKGLASS_ACCOUNT}' configuré mais"
      err "keys/${BREAKGLASS_KEY_OWNER}.pub est absent"
      bad=1
    fi
  fi

  (( bad == 0 )) || die "corrige les clés — aucun compte n'a été touché."

  if (( ${#MEMBERS_PENDING[@]} > 0 )); then
    warn "${#MEMBERS_PENDING[@]} membre(s) sans clé : ${MEMBERS_PENDING[*]}"
    warn "#22 restera partiellement ouverte tant qu'ils ne sont pas créés"
  fi
}

# ── Groupe SSH ───────────────────────────────────────────────────────────

ensure_group() {
  step "Groupe ${SSH_GROUP}"
  if getent group "$SSH_GROUP" >/dev/null; then
    log "groupe ${SSH_GROUP} déjà présent"
  else
    run groupadd --system "$SSH_GROUP"
    done_ok "groupe ${SSH_GROUP} créé"
  fi
}

# ── Comptes ──────────────────────────────────────────────────────────────

create_member() {
  local u="$1"

  if id -u "$u" >/dev/null 2>&1; then
    log "compte ${u} déjà présent"
  else
    # useradd et non adduser : adduser applique NAME_REGEX et refuserait
    # tout login hors de ^[a-z][-a-z0-9_]*$.
    run useradd --create-home --shell /bin/bash --comment "BioWatch ${u}" "$u"
    done_ok "compte ${u} créé"
  fi

  # Aucun mot de passe : même pendant la fenêtre où PasswordAuthentication
  # vaut encore yes, ces comptes ne sont joignables que par clé.
  run usermod --lock "$u"
  run usermod --append --groups "$SSH_GROUP" "$u"
}

install_key() {
  local u="$1" src="$2" home ak
  # getent sort en code 2 si le compte est absent. En dry-run il l'est
  # forcément, puisque useradd n'a fait qu'être affiché : sans ce garde-fou
  # le pipefail remonte et tue le script au premier membre.
  home="$(getent passwd "$u" 2>/dev/null | cut -d: -f6 || true)"

  if [[ -z "$home" ]]; then
    if (( DRY_RUN )); then
      home="/home/${u}"   # ce que useradd --create-home produira
    else
      die "répertoire personnel introuvable pour ${u}"
    fi
  fi
  ak="${home}/.ssh/authorized_keys"

  if (( DRY_RUN )); then
    dry "installer keys/$(basename "$src") → ${ak} (600, ${u}:${u})"
    return 0
  fi

  # Permissions strictes : sshd refuse silencieusement une clé dont le
  # répertoire ou le fichier est trop ouvert. Cause n°1 des « ma clé ne
  # marche pas » alors que la config est correcte.
  install -d -m 700 -o "$u" -g "$u" "${home}/.ssh"
  backup_file "$ak"
  install -m 600 -o "$u" -g "$u" "$src" "$ak"
  ok "clé installée pour ${u} (${ak})"
}

# ── Sudo ─────────────────────────────────────────────────────────────────

configure_sudo() {
  step "Sudo sans mot de passe"
  local tmp; tmp="$(mktemp)"
  {
    echo "# Généré par infra/vps/10-users-and-keys.sh — ne pas éditer à la main."
    echo "# Comptes clé-seule : la règle %sudo par défaut exige un mot de passe"
    echo "# qu'ils n'ont pas, ce qui les rendrait incapables d'administrer."
    local u
    for u in "${MEMBERS[@]}"; do
      printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$u"
    done
  } > "$tmp"

  # Un sudoers malformé casse sudo sur toute la machine. Avec
  # PermitRootLogin no à venir, plus aucune escalade ne serait possible.
  # La validation n'est pas optionnelle.
  if ! visudo --check --quiet --file="$tmp"; then
    rm -f "$tmp"
    die "fichier sudoers généré invalide — rien n'a été installé"
  fi
  ok "syntaxe sudoers validée par visudo"

  if (( DRY_RUN )); then
    dry "installer ${SUDOERS_FILE} (440) :"
    sed 's/^/            │ /' "$tmp"
    rm -f "$tmp"
    return 0
  fi

  backup_file "$SUDOERS_FILE"
  install -m 440 -o root -g root "$tmp" "$SUDOERS_FILE"
  rm -f "$tmp"
  ok "écrit ${SUDOERS_FILE}"
}

# ── Port ─────────────────────────────────────────────────────────────────

# Restaure le socket d'origine (port 22, IPv4 + IPv6) en supprimant notre
# drop-in. C'est l'état livré par l'image, donc un point de repli sûr.
create_socket_rollback() {
  ensure_backup_dir
  write_file "$SOCKET_ROLLBACK_SCRIPT" 750 <<EOF
#!/usr/bin/env bash
# Généré par 10-users-and-keys.sh.
# Supprime le drop-in de port et rend la main à la configuration d'origine.
set -uo pipefail
logger -t biowatch-rollback "restauration du socket SSH d'origine"
rm -f "${SSH_SOCKET_DROPIN}"
systemctl daemon-reload
systemctl restart ssh.socket
systemctl try-restart ssh.service
logger -t biowatch-rollback "socket SSH restauré"
EOF
}

open_target_port() {
  step "Ouverture du port ${SSH_PORT} (en plus du ${SSH_PORT_LEGACY})"

  # Rejouer ce script après le durcissement (pour ajouter un membre) ne
  # doit PAS rouvrir le port 22 et défaire silencieusement #22.
  # -xF : ligne entière, chaîne littérale. Sans -F les points de 0.0.0.0
  # seraient des jokers ; sans -x le port 22 matcherait dans 50022.
  if [[ -f "$SSH_SOCKET_DROPIN" ]] \
     && grep -qxF "ListenStream=0.0.0.0:${SSH_PORT}" "$SSH_SOCKET_DROPIN" \
     && ! grep -qxF "ListenStream=0.0.0.0:${SSH_PORT_LEGACY}" "$SSH_SOCKET_DROPIN"; then
    ok "durcissement déjà appliqué — le port ${SSH_PORT_LEGACY} reste fermé"
    assert_listening "$SSH_PORT"
    return 0
  fi

  # Ce script modifie le socket d'écoute de SSH : il PEUT couper l'accès,
  # même s'il n'enlève aucun droit. Le filet n'est donc pas réservé aux
  # scripts de retrait.
  create_socket_rollback
  arm_deadman "$SOCKET_ROLLBACK_UNIT" "$SOCKET_ROLLBACK_SCRIPT"

  # Sur cette machine sshd est démarré par socket : la directive Port de
  # sshd_config est ignorée. Le port se règle ici, et nulle part ailleurs.
  write_file "$SSH_SOCKET_DROPIN" 644 <<EOF
# Généré par infra/vps/10-users-and-keys.sh
#
# Phase additive : les deux ports écoutent en parallèle, le temps de
# prouver que ${SSH_PORT} fonctionne. 20-harden-ssh.sh fermera le
# ${SSH_PORT_LEGACY}.
#
# Le ListenStream vide efface la valeur par défaut de l'unité ; sans lui
# on ajoute aux ports existants sans jamais pouvoir en retirer.
#
# Adresses EXPLICITES, jamais un port nu. Un « ListenStream=22 » seul ne
# crée ici qu'un socket IPv6 non dual-stack : l'IPv4 disparaît
# silencieusement, ss affiche bien un LISTEN, et on se retrouve avec un
# serveur injoignable depuis un client IPv4.
[Socket]
ListenStream=
ListenStream=0.0.0.0:${SSH_PORT_LEGACY}
ListenStream=[::]:${SSH_PORT_LEGACY}
ListenStream=0.0.0.0:${SSH_PORT}
ListenStream=[::]:${SSH_PORT}
EOF

  reload_ssh_socket
  assert_listening "$SSH_PORT_LEGACY"
  assert_listening "$SSH_PORT"
}

# ── Déroulé ──────────────────────────────────────────────────────────────

validate_keys
ensure_group

step "Comptes membres"
for member in "${MEMBERS[@]}"; do
  create_member "$member"
  install_key "$member" "${LIB_DIR}/keys/${member}.pub"
done

if [[ -n "$BREAKGLASS_ACCOUNT" ]]; then
  step "Compte de secours ${BREAKGLASS_ACCOUNT}"
  if id -u "$BREAKGLASS_ACCOUNT" >/dev/null 2>&1; then
    # Sans clé, ce compte deviendra inutilisable dès que le mot de passe
    # sera coupé — un filet de sécurité qui ne rattrape rien.
    install_key "$BREAKGLASS_ACCOUNT" "${LIB_DIR}/keys/${BREAKGLASS_KEY_OWNER}.pub"
    run usermod --append --groups "$SSH_GROUP" "$BREAKGLASS_ACCOUNT"
    done_ok "${BREAKGLASS_ACCOUNT} conservé comme accès de secours"
  else
    warn "compte de secours ${BREAKGLASS_ACCOUNT} introuvable — ignoré"
  fi
fi

configure_sudo
open_target_port

# ── Sortie ───────────────────────────────────────────────────────────────

cat <<EOF

${C_GRN}${C_BLD}Phase additive terminée.${C_RST} Aucun accès n'a été retiré.

${C_BLD}À faire maintenant, depuis un NOUVEAU terminal sur ton poste :${C_RST}

EOF

for member in "${MEMBERS[@]}"; do
  printf '    ssh -p %s -i ~/.ssh/biowatch_ed25519 %s@<IP_DU_VPS>\n' "$SSH_PORT" "$member"
done

cat <<EOF

Chaque membre doit réussir SA connexion avant de passer à la suite. Le
port ${SSH_PORT_LEGACY} reste ouvert : en cas de problème tu gardes ton accès actuel.

${C_YEL}${C_BLD}Un filet est armé.${C_RST} Ce script a modifié le socket d'écoute de SSH :
les vérifications locales ne prouvent pas que le serveur reste joignable
depuis l'extérieur. Si tu ne confirmes pas, la configuration d'origine
sera restaurée dans ${ROLLBACK_DELAY} et tu récupéreras l'accès sur le
port ${SSH_PORT_LEGACY}.

Teste explicitement les DEUX familles d'adresses — une IPv4 muette est
invisible depuis le serveur :

    ssh -4 -p ${SSH_PORT} -i ~/.ssh/biowatch_ed25519 ${MEMBERS[0]}@<IP_DU_VPS>
    ssh -6 -p ${SSH_PORT} -i ~/.ssh/biowatch_ed25519 ${MEMBERS[0]}@<IPv6_DU_VPS>

Puis seulement :

    sudo systemctl stop ${SOCKET_ROLLBACK_UNIT}.timer

Quand c'est validé, depuis une session ouverte sur le port ${SSH_PORT} :

    ./20-harden-ssh.sh --dry-run
    ./20-harden-ssh.sh

EOF
