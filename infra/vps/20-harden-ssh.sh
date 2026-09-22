#!/usr/bin/env bash
# 20-harden-ssh.sh — issue #22, phase de RETRAIT. Porte de non-retour.
#
# Coupe l'authentification par mot de passe, l'accès root, et ferme le
# port 22. À partir d'ici, seule une clé valide sur le nouveau port entre.
#
# Deux garde-fous :
#   1. le script refuse de démarrer sans constater lui-même une session
#      établie sur le nouveau port ;
#   2. un filet systemd restaure la configuration précédente après
#      ROLLBACK_DELAY, sauf annulation explicite de ta part.
#
#   ./20-harden-ssh.sh --dry-run
#   ./20-harden-ssh.sh

USAGE="Usage: ./20-harden-ssh.sh [--dry-run]

Retire les accès par mot de passe, root et port 22. Exige une session
déjà établie sur le nouveau port. Arme une restauration automatique.

  --dry-run   affiche les changements sans les appliquer"

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
init_script "${BASH_SOURCE[0]}" "$@"
require_root
init_log
enable_error_trap

banner "#22 — Durcissement SSH (irréversible sans le filet)"

# ── Préconditions ────────────────────────────────────────────────────────

step "Vérification des préconditions"

# 1. Le nouveau port doit être prouvé fonctionnel de bout en bout.
require_session_on_port "$SSH_PORT"

# 2. Au moins un compte doit avoir une clé exploitable, sinon couper le
#    mot de passe ne laisse plus personne entrer.
usable=0
for member in "${MEMBERS[@]}"; do
  home="$(getent passwd "$member" 2>/dev/null | cut -d: -f6)" || continue
  [[ -n "$home" && -s "${home}/.ssh/authorized_keys" ]] || continue
  if ssh-keygen -l -f "${home}/.ssh/authorized_keys" >/dev/null 2>&1; then
    ok "${member} possède une clé exploitable"
    usable=$((usable + 1))
  fi
done
(( usable > 0 )) || die "aucun compte membre n'a de clé valide — abandon"

# 3. Les membres doivent être dans le groupe filtré par AllowGroups,
#    faute de quoi la directive les exclurait tous.
for member in "${MEMBERS[@]}"; do
  if id -nG "$member" 2>/dev/null | tr ' ' '\n' | grep -qx "$SSH_GROUP"; then
    ok "${member} appartient à ${SSH_GROUP}"
  else
    die "${member} n'est pas dans ${SSH_GROUP} — rejoue 10-users-and-keys.sh"
  fi
done

if [[ -n "$BREAKGLASS_ACCOUNT" ]] && id -u "$BREAKGLASS_ACCOUNT" >/dev/null 2>&1; then
  bg_home="$(getent passwd "$BREAKGLASS_ACCOUNT" | cut -d: -f6)"
  if [[ ! -s "${bg_home}/.ssh/authorized_keys" ]]; then
    die "${BREAKGLASS_ACCOUNT} est conservé comme secours mais n'a pas de clé —
     il serait injoignable après durcissement. Rejoue 10-users-and-keys.sh."
  fi
  id -nG "$BREAKGLASS_ACCOUNT" | tr ' ' '\n' | grep -qx "$SSH_GROUP" \
    || die "${BREAKGLASS_ACCOUNT} absent de ${SSH_GROUP} — AllowGroups l'exclurait"
  ok "${BREAKGLASS_ACCOUNT} utilisable comme accès de secours"
fi

# ── Script de restauration ───────────────────────────────────────────────
# Généré avant toute modification : il doit exister et pointer sur un
# backup complet au moment où le filet se déclenche.

create_rollback_script() {
  step "Préparation du filet de sécurité"
  ensure_backup_dir

  # Sauvegarde préalable de tout ce que le durcissement va toucher.
  backup_file /etc/ssh/sshd_config
  for f in /etc/ssh/sshd_config.d/*.conf; do
    [[ -e "$f" ]] && backup_file "$f"
  done
  backup_file "$SSH_SOCKET_DROPIN"

  write_file "$SSH_ROLLBACK_SCRIPT" 750 <<EOF
#!/usr/bin/env bash
# Généré par 20-harden-ssh.sh.
# Restaure la configuration SSH antérieure au durcissement.
# Déclenché automatiquement si le durcissement n'a pas été confirmé.
set -uo pipefail
BACKUP="${BACKUP_DIR}"
logger -t biowatch-rollback "restauration SSH depuis \${BACKUP}"

[[ -d "\${BACKUP}/etc/ssh" ]] && cp -a "\${BACKUP}/etc/ssh/." /etc/ssh/
[[ -d "\${BACKUP}/etc/systemd/system/ssh.socket.d" ]] \\
  && cp -a "\${BACKUP}/etc/systemd/system/ssh.socket.d/." /etc/systemd/system/ssh.socket.d/

# Les fichiers créés par le durcissement ne sont dans aucun backup :
# les recopier ne les enlèverait pas.
rm -f "${SSH_HARDENING_DROPIN}" "${CLOUD_INIT_DROPIN}"

systemctl daemon-reload
systemctl restart ssh.socket
systemctl try-restart ssh.service
logger -t biowatch-rollback "restauration SSH terminée"
EOF
}

# ── Durcissement ─────────────────────────────────────────────────────────

write_hardening() {
  step "Configuration SSH durcie"

  # Préfixe 00- délibéré. sshd retient la PREMIÈRE valeur rencontrée et
  # lit /etc/ssh/sshd_config.d/*.conf en ordre alphabétique. Le
  # 50-cloud-init.conf de l'image impose PasswordAuthentication yes : un
  # fichier nommé 99-* serait lu après, donc ignoré, et ce script
  # annoncerait un succès sur un serveur resté ouvert au mot de passe.
  write_file "$SSH_HARDENING_DROPIN" 644 <<EOF
# Généré par infra/vps/20-harden-ssh.sh — issue #22.
#
# Préfixe 00- obligatoire : première valeur lue = valeur retenue.
# Renommer ce fichier en 99-* le rendrait silencieusement inopérant.

PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
PermitEmptyPasswords no

# Seuls les membres du groupe entrent. Les comptes de service de #25
# (biowatch-api, biowatch-jobs, deploy) n'auront jamais d'accès SSH sans
# qu'aucune règle supplémentaire soit nécessaire.
AllowGroups ${SSH_GROUP}

MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
EOF

  # cloud-init réécrit son propre drop-in au réamorçage et réactiverait
  # l'authentification par mot de passe.
  write_file "$CLOUD_INIT_DROPIN" 644 <<EOF
# Généré par infra/vps/20-harden-ssh.sh.
# Empêche cloud-init de réactiver PasswordAuthentication au réamorçage.
ssh_pwauth: false
EOF

  # Validation AVANT redémarrage : une erreur de syntaxe empêcherait sshd
  # de repartir, sur un serveur auquel on n'a plus accès autrement.
  if (( DRY_RUN )); then
    dry "valider la configuration avec sshd -t"
  else
    sshd -t || die "configuration sshd invalide — restauration déclenchée"
    ok "configuration sshd validée"
  fi
}

close_legacy_port() {
  step "Fermeture du port ${SSH_PORT_LEGACY}"
  write_file "$SSH_SOCKET_DROPIN" 644 <<EOF
# Généré par infra/vps/20-harden-ssh.sh
# Phase de retrait : seul ${SSH_PORT} écoute désormais.
#
# Adresses explicites : un port nu ne produirait qu'un socket IPv6 et
# couperait tous les clients IPv4.
[Socket]
ListenStream=
ListenStream=0.0.0.0:${SSH_PORT}
ListenStream=[::]:${SSH_PORT}
EOF

  reload_ssh_socket
  assert_listening "$SSH_PORT"
  assert_not_listening "$SSH_PORT_LEGACY"
}

# ── Déroulé ──────────────────────────────────────────────────────────────

create_rollback_script

# Armé AVANT les opérations risquées. Armé après, il ne protégerait de
# rien si l'une d'elles coupait déjà l'accès.
arm_deadman "$SSH_ROLLBACK_UNIT" "$SSH_ROLLBACK_SCRIPT"

write_hardening
close_legacy_port

if (( ! DRY_RUN )); then
  step "Configuration effective"
  sshd -T 2>/dev/null | grep -iE '^(passwordauthentication|permitrootlogin|pubkeyauthentication|allowgroups|maxauthtries)' \
    | sed 's/^/            /'
fi

announce_deadman "$SSH_ROLLBACK_UNIT" \
  "ssh -p ${SSH_PORT} -i ~/.ssh/biowatch_ed25519 ${MEMBERS[0]}@<IP_DU_VPS>"

cat <<EOF
Une fois le filet désarmé, passe au firewall :

    ./30-firewall.sh --dry-run
    ./30-firewall.sh

EOF
