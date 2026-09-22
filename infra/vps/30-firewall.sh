#!/usr/bin/env bash
# 30-firewall.sh — issue #28.
#
# UFW en politique « deny incoming », avec SSH, 80 et 443 ouverts.
#
# L'ORDRE EST CRITIQUE : activer UFW avant d'avoir autorisé le port SSH
# coupe la session en cours et verrouille le serveur instantanément. Les
# règles sont donc posées AVANT l'activation, jamais après.
#
#   ./30-firewall.sh --dry-run
#   ./30-firewall.sh

USAGE="Usage: ./30-firewall.sh [--dry-run]

Configure et active UFW : deny incoming, SSH + 80 + 443 autorisés.
Exige une session établie sur le port SSH cible. Arme une désactivation
automatique si la règle n'est pas confirmée.

  --dry-run   affiche les changements sans les appliquer"

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
init_script "${BASH_SOURCE[0]}" "$@"
require_root
init_log
enable_error_trap

banner "#28 — Firewall UFW"

# ── Préconditions ────────────────────────────────────────────────────────

step "Vérification des préconditions"
require_session_on_port "$SSH_PORT"

if ! command -v ufw >/dev/null 2>&1; then
  log "ufw absent — installation"
  run apt-get update -qq
  # via env : run exécute "$@", une affectation en tête serait prise pour
  # le nom de la commande.
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw
fi
# En dry-run l'installation n'a fait qu'être affichée : interroger la
# version d'un binaire absent tuerait le script.
if command -v ufw >/dev/null 2>&1; then
  ok "ufw disponible : $(ufw version 2>/dev/null | head -1)"
else
  dry "ufw serait installé puis interrogé ici"
fi

# Sans IPV6=yes, UFW ne génère AUCUNE règle ip6tables : tout le trafic IPv6
# suit la politique par défaut du noyau, généralement ACCEPT. Le firewall
# annoncerait « deny incoming » alors que chaque port resterait atteignable
# en IPv6 — le critère « tous les autres ports bloqués » (#28) serait faux
# sans que rien ne le signale. Une erreur, pas un avertissement.
if grep -qE '^\s*IPV6\s*=\s*yes' /etc/default/ufw 2>/dev/null; then
  ok "UFW gère IPv6"
else
  err "IPV6 n'est pas à yes dans /etc/default/ufw."
  err "UFW ne filtrerait alors que l'IPv4, en laissant tous les ports"
  err "ouverts en IPv6. Corrige /etc/default/ufw puis relance."
  die "filtrage IPv6 absent — rien n'a été activé"
fi

# ── Filet ────────────────────────────────────────────────────────────────

create_rollback_script() {
  step "Préparation du filet de sécurité"
  ensure_backup_dir
  write_file "$UFW_ROLLBACK_SCRIPT" 750 <<EOF
#!/usr/bin/env bash
# Généré par 30-firewall.sh. Désactive UFW si la règle n'a pas été
# confirmée par une nouvelle connexion.
set -uo pipefail
logger -t biowatch-rollback "désactivation UFW (non confirmée)"
ufw --force disable
logger -t biowatch-rollback "UFW désactivé"
EOF
}

# ── Règles ───────────────────────────────────────────────────────────────

apply_rules() {
  step "Règles"

  # SSH EN PREMIER, avant toute politique restrictive. Toute autre
  # séquence expose à une coupure pendant l'exécution du script.
  run ufw allow "${SSH_PORT}/tcp" comment "SSH BioWatch"
  done_ok "port ${SSH_PORT}/tcp autorisé (SSH)"

  local p
  for p in "${FIREWALL_TCP_PORTS[@]}"; do
    run ufw allow "${p}/tcp" comment "BioWatch"
    done_ok "port ${p}/tcp autorisé"
  done

  # Le port 22 a été fermé par 20-harden-ssh.sh ; on retire explicitement
  # toute règle résiduelle plutôt que de laisser une autorisation morte.
  if ufw status 2>/dev/null | grep -qE "^${SSH_PORT_LEGACY}/tcp"; then
    run ufw delete allow "${SSH_PORT_LEGACY}/tcp"
    done_ok "règle résiduelle sur ${SSH_PORT_LEGACY}/tcp retirée"
  fi

  run ufw default deny incoming
  run ufw default allow outgoing
  done_ok "politique par défaut : deny incoming / allow outgoing"
}

enable_ufw() {
  step "Activation"
  # --force évite l'invite « may disrupt existing ssh connections », qui
  # bloquerait indéfiniment un script non interactif.
  run ufw --force enable

  # Surtout PAS assert_listening ici : elle sonde la boucle locale, que
  # /etc/ufw/before.rules accepte inconditionnellement (-i lo -j ACCEPT).
  # Elle réussirait donc même si la règle SSH externe était absente, et
  # afficherait un OK qui ne prouve rien du firewall.
  #
  # Ce qu'on peut vérifier localement, c'est la présence des règles dans
  # les deux familles. La joignabilité réelle ne se prouve que depuis
  # l'extérieur — d'où le nc affiché en fin de script.
  if (( DRY_RUN )); then
    dry "vérifier la présence des règles ${SSH_PORT}/tcp en v4 et v6"
    return 0
  fi
  local rules
  rules="$(ufw status | grep -c "^${SSH_PORT}/tcp")"
  (( rules >= 2 )) \
    || die "règles SSH incomplètes (${rules} trouvée(s), 2 attendues : v4 + v6)"
  ok "règles ${SSH_PORT}/tcp présentes en v4 et v6 — joignabilité à confirmer depuis l'extérieur"
}

# ── Déroulé ──────────────────────────────────────────────────────────────

create_rollback_script
arm_deadman "$UFW_ROLLBACK_UNIT" "$UFW_ROLLBACK_SCRIPT"

apply_rules
enable_ufw

if (( ! DRY_RUN )); then
  step "État final"
  ufw status verbose | sed 's/^/            /'
fi

announce_deadman "$UFW_ROLLBACK_UNIT" \
  "ssh -p ${SSH_PORT} -i ~/.ssh/biowatch_ed25519 ${MEMBERS[0]}@<IP_DU_VPS>"

cat <<EOF
${C_BLD}Validation externe (#28 — « test d'accès externe validé ») :${C_RST}

Depuis ton poste, HORS du VPS. Le premier doit répondre, les autres non :

    nc -zv -w5 <IP_DU_VPS> ${SSH_PORT}      # attendu : succeeded
    nc -zv -w5 <IP_DU_VPS> ${SSH_PORT_LEGACY}         # attendu : échec / timeout
    nc -zv -w5 <IP_DU_VPS> 5432             # attendu : échec / timeout

C'était le dernier script. #22 et #28 sont couvertes.

fail2ban a été écarté délibérément et reporté sur #26 — voir la section
dédiée du README.

EOF
