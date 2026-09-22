#!/usr/bin/env bash
# 00-diagnose.sh — état des lieux du VPS, en LECTURE SEULE.
#
# Ne modifie rien. À lancer avant chaque script, et après, pour comparer.
#
#   ./00-diagnose.sh

USAGE="Usage: ./00-diagnose.sh
État des lieux complet du VPS. Lecture seule, aucune modification."

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
init_script "${BASH_SOURCE[0]}" "$@"
require_root

set -uo pipefail   # pas de -e : un diagnostic doit aller au bout même si
                   # une sonde échoue (outil absent, service inexistant).

banner "Diagnostic VPS BioWatch"

step "Système"
. /etc/os-release && printf '  %s\n  noyau %s\n' "$PRETTY_NAME" "$(uname -r)"

step "Identité"
printf '  invoquant : %s\n  effectif  : %s\n' "${SUDO_USER:-inconnu}" "$(id -un)"

step "SSH — unités systemd"
for u in ssh.socket ssh.service; do
  printf '  %-12s enabled=%-10s active=%s\n' \
    "$u" "$(systemctl is-enabled "$u" 2>&1)" "$(systemctl is-active "$u" 2>&1)"
done
if [[ "$(systemctl is-enabled ssh.socket 2>/dev/null)" == "enabled" ]]; then
  printf '  %s→ activation par socket : le port vient du socket, pas de sshd_config%s\n' "$C_YEL" "$C_RST"
fi

step "SSH — configuration effective"
sshd -T 2>/dev/null | grep -iE '^(port|permitrootlogin|passwordauthentication|pubkeyauthentication|kbdinteractiveauthentication|permitemptypasswords|maxauthtries|logingracetime|allowgroups|allowusers)' \
  | sed 's/^/  /' || printf '  (sshd -T indisponible)\n'

step "SSH — drop-ins (ordre de lecture = ordre alphabétique, 1re valeur gagnante)"
if [[ -d /etc/ssh/sshd_config.d ]]; then
  for f in /etc/ssh/sshd_config.d/*.conf; do
    [[ -e "$f" ]] || continue
    printf '  %s\n' "$(basename "$f")"
    sed 's/^/      │ /' "$f"
  done
else
  printf '  (aucun)\n'
fi

step "Socket SSH — drop-ins"
if [[ -d /etc/systemd/system/ssh.socket.d ]]; then
  for f in /etc/systemd/system/ssh.socket.d/*.conf; do
    [[ -e "$f" ]] || continue
    printf '  %s\n' "$(basename "$f")"
    sed 's/^/      │ /' "$f"
  done
else
  printf '  (aucun — port par défaut)\n'
fi

step "Ports en écoute"
ss -tlnpH | sed 's/^/  /'

step "Port cible ${SSH_PORT}"
if ss -tlnH "( sport = :${SSH_PORT} )" | grep -q .; then
  printf '  %sdéjà en écoute%s\n' "$C_GRN" "$C_RST"
elif ss -tlnH "( sport = :${SSH_PORT} )" >/dev/null 2>&1; then
  printf '  libre\n'
fi

step "Comptes humains (uid >= 1000)"
awk -F: '$3>=1000 && $3<65534 {printf "  %-14s uid=%-6s shell=%s\n", $1, $3, $7}' /etc/passwd

step "Comptes attendus"
for u in "${MEMBERS[@]}"; do
  if id -u "$u" >/dev/null 2>&1; then
    local_keys="$(getent passwd "$u" | cut -d: -f6)/.ssh/authorized_keys"
    if [[ -s "$local_keys" ]]; then
      printf '  %-14s %sprésent, clé posée%s\n' "$u" "$C_GRN" "$C_RST"
    else
      printf '  %-14s %sprésent, SANS CLÉ%s\n' "$u" "$C_RED" "$C_RST"
    fi
  else
    printf '  %-14s %sabsent%s\n' "$u" "$C_YEL" "$C_RST"
  fi
done
for u in "${MEMBERS_PENDING[@]}"; do
  printf '  %-14s en attente de clé (non créé)\n' "$u"
done

step "Groupe ${SSH_GROUP}"
getent group "$SSH_GROUP" | sed 's/^/  /' || printf '  (inexistant)\n'

step "Sudo"
printf '  groupe sudo : %s\n' "$(getent group sudo)"
if [[ -d /etc/sudoers.d ]]; then
  for f in /etc/sudoers.d/*; do
    [[ -f "$f" ]] || continue
    printf '  %s\n' "$(basename "$f")"
    grep -vE '^\s*(#|$)' "$f" | sed 's/^/      │ /'
  done
fi

step "Clés SSH installées"
for h in /root /home/*; do
  ak="$h/.ssh/authorized_keys"
  [[ -f "$ak" ]] || continue
  if [[ -s "$ak" ]]; then
    printf '  %s\n' "$ak"
    while read -r line; do
      [[ -n "$line" ]] || continue
      printf '      │ %s\n' "$(ssh-keygen -l -f /dev/stdin <<<"$line" 2>/dev/null || echo "ILLISIBLE")"
    done < "$ak"
  else
    printf '  %s %s(vide)%s\n' "$ak" "$C_RED" "$C_RST"
  fi
done

step "Firewall"
if command -v ufw >/dev/null; then
  ufw status verbose | sed 's/^/  /'
else
  printf '  ufw non installé\n'
fi

step "fail2ban (écarté de #22, reporté sur #26)"
# Sonde conservée : si quelqu'un l'installe un jour, mieux vaut le voir ici
# que le découvrir en débuggant un membre banni sans comprendre pourquoi.
if command -v fail2ban-client >/dev/null; then
  printf '  %sinstallé alors qu'"'"'il a été écarté — vérifier l'"'"'intention%s\n' "$C_YEL" "$C_RST"
  printf '  service : %s\n' "$(systemctl is-active fail2ban)"
  fail2ban-client status 2>/dev/null | sed 's/^/  /'
  fail2ban-client status sshd 2>/dev/null | sed 's/^/  /'
else
  printf '  absent (conforme à la décision)\n'
fi

step "Filets armés"
found=0
for unit in "$SOCKET_ROLLBACK_UNIT" "$SSH_ROLLBACK_UNIT" "$UFW_ROLLBACK_UNIT"; do
  if systemctl is-active "${unit}.timer" >/dev/null 2>&1; then
    printf '  %s%s.timer ACTIF — restauration en attente%s\n' "$C_YEL" "$unit" "$C_RST"
    systemctl list-timers "${unit}.timer" --no-pager | sed -n '2p' | sed 's/^/      /'
    found=1
  fi
done
(( found )) || printf '  aucun\n'

step "Rollbacks déclenchés (24 h)"
# Un filet qui s'est déclenché a défait un durcissement en silence : sans
# cette trace on croit la machine configurée alors qu'elle est revenue en
# arrière.
if journalctl -t biowatch-rollback --since "-24 hours" --no-pager -q 2>/dev/null | grep -q .; then
  journalctl -t biowatch-rollback --since "-24 hours" --no-pager -q | sed 's/^/  /'
  printf '  %s→ une restauration a eu lieu : revérifier sshd et ufw%s\n' "$C_YEL" "$C_RST"
else
  printf '  aucun\n'
fi

step "Backups"
if [[ -d "$BACKUP_ROOT" ]]; then
  ls -1 "$BACKUP_ROOT" | sed 's/^/  /'
else
  printf '  aucun\n'
fi

printf '\n'
