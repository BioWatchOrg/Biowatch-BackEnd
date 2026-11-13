#!/usr/bin/env bash
# e2e-deploy.sh — test de bout en bout du déploiement des jobs (#27).
#
# Rejoue sur un Ubuntu 26.04 JETABLE ce que fait le VPS : 40-service-users.sh,
# 60-jobs-deploy.sh, puis de vrais déploiements par SSH (vrai sshd, vraie clé
# à commande forcée), retours arrière, manifestes hostiles et droits sudo.
# systemd n'est pas PID 1 dans un conteneur : systemctl est remplacé par un
# stub qui journalise ; tout le reste est réel (comptes, sudo, sshd, uv, venv).
#
# NE JAMAIS LANCER SUR UNE VRAIE MACHINE : crée des comptes, réécrit sshd,
# remplace systemctl. Le script refuse de démarrer hors d'un conteneur.
#
#   docker run --rm -e BIOWATCH_E2E=1 -v "$PWD:/src:ro" ubuntu:26.04 \
#     bash /src/infra/vps/tests/e2e-deploy.sh /src
#
# Lancé en CI par .github/workflows/infra-e2e.yml.

set -Eeuo pipefail

SRC="${1:-/src}"
FAILURES=0

section() { printf '\n\033[1m── %s\033[0m\n' "$*"; }
ok()      { printf '  OK  %s\n' "$*"; }
fail()    { printf '  KO  %s\n' "$*" >&2; FAILURES=$((FAILURES + 1)); }
die()     { printf 'ERREUR : %s\n' "$*" >&2; exit 1; }
check()   { local what="$1"; shift; if "$@"; then ok "$what"; else fail "$what"; fi; }
refused() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then fail "$what (accepté !)"; else ok "$what (refusé)"; fi; }

# ── Garde-fou ────────────────────────────────────────────────────────────

[[ "${BIOWATCH_E2E:-}" == 1 ]] || die "BIOWATCH_E2E=1 requis (conteneur jetable uniquement)"
[[ -f /.dockerenv || -f /run/.containerenv ]] || die "pas dans un conteneur : refus de démarrer"
(( EUID == 0 )) || die "doit tourner en root (dans le conteneur)"
[[ -f "${SRC}/infra/vps/60-jobs-deploy.sh" ]] || die "dépôt introuvable dans ${SRC}"

# ── Dépendances ──────────────────────────────────────────────────────────

section "Dépendances du conteneur"
if ! command -v sshd >/dev/null || ! command -v shellcheck >/dev/null; then
  apt-get update -qq >/dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    sudo curl ca-certificates systemd openssh-client openssh-server git shellcheck \
    util-linux passwd tar tzdata >/dev/null
fi
printf '#!/bin/sh\necho "[systemctl] $*" >&2\n' > /usr/local/bin/systemctl
chmod 755 /usr/local/bin/systemctl
ok "paquets installés, systemctl remplacé par un stub"

section "shellcheck"
( cd "${SRC}/infra/vps" && shellcheck -x -S warning \
    bin/biowatch-ci-deploy bin/biowatch-release bin/biowatch-install-timers \
    60-jobs-deploy.sh tests/e2e-deploy.sh )
ok "scripts de déploiement"

# ── Installation comme sur le VPS ────────────────────────────────────────

INFRA=/opt/biowatch-infra
rm -rf "$INFRA" && mkdir -p "$INFRA"
cp -a "${SRC}/infra/vps" "${SRC}/infra/systemd" "$INFRA/"
ssh-keygen -q -t ed25519 -N "" -C e2e-ci -f /root/ci_key <<<y >/dev/null
cp /root/ci_key.pub "${INFRA}/vps/keys/ci/biowatch-ci.pub"
cd "${INFRA}/vps"

section "40-service-users.sh puis 60-jobs-deploy.sh"
./40-service-users.sh >/tmp/40.log 2>&1 || { cat /tmp/40.log; die "40-service-users.sh"; }
./60-jobs-deploy.sh --dry-run >/tmp/60-dry.log 2>&1 || { cat /tmp/60-dry.log; die "60 --dry-run"; }
check "--dry-run ne crée pas le compte CI" bash -c '! id biowatch-ci >/dev/null 2>&1'
./60-jobs-deploy.sh >/tmp/60.log 2>&1 || { cat /tmp/60.log; die "60-jobs-deploy.sh"; }
check "droits du compte CI prouvés" grep -q "droits du compte CI conformes" /tmp/60.log
./60-jobs-deploy.sh >/tmp/60-bis.log 2>&1 || { cat /tmp/60-bis.log; die "60 rejoué"; }
check "60 rejoué : unité et sudoers inchangés" bash -c \
  "grep -q 'biowatch-job@.service à jour' /tmp/60-bis.log && grep -q '91-biowatch-ci à jour' /tmp/60-bis.log"
./40-service-users.sh >/tmp/40-bis.log 2>&1
check "40 rejoué : garanties de #25 intactes" grep -q "isolation conforme" /tmp/40-bis.log

# ── sshd réel, AllowGroups comme 20-harden-ssh.sh ────────────────────────

section "sshd réel"
mkdir -p /run/sshd
printf 'PasswordAuthentication no\nAllowGroups biowatch-ssh biowatch-ci\n' \
  > /etc/ssh/sshd_config.d/00-e2e.conf
ssh-keygen -A >/dev/null
/usr/sbin/sshd -p 2222
SSH=(ssh -i /root/ci_key -p 2222 -o IdentitiesOnly=yes -o StrictHostKeyChecking=no
     -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR biowatch-ci@127.0.0.1)
ok "sshd sur 2222"

# ── Commits de test ──────────────────────────────────────────────────────
# SHA1 = le dépôt tel quel. SHA2 = + un job planifié avec plafond mémoire.
# SHA3 = + un job dont la fréquence passe le filtre Python mais est refusée
# par systemd-analyze : son déploiement ne doit RIEN activer.
# SHA4 = SHA2 dont la génération du manifeste écrit une ligne puis plante :
# les timers en place ne doivent pas bouger.

section "Commits de test"
REPO=/tmp/repo
rm -rf "$REPO" && mkdir "$REPO"
tar -C "$SRC" --exclude=.git --exclude=.venv --exclude='.env' --exclude='.env.*' \
  --exclude=data -cf - . | tar -C "$REPO" -xf -
cd "$REPO"
git config --global user.email e2e@biowatch; git config --global user.name e2e
git config --global --add safe.directory '*'
git init -q && git add -A && git commit -qm "e2e: état du dépôt"
SHA1="$(git rev-parse HEAD)"

append_job() {
  cat >> apps/jobs/definitions.py <<PY


register(Job(name="$1", help="e2e", run=lambda _args: None, schedule="$2"${3:-}))
PY
  git commit -qam "e2e: job $1"
}
append_job e2e_probe "*-*-* 04:00:00 Europe/Paris" ', memory_max="512M"'
SHA2="$(git rev-parse HEAD)"
append_job e2e_bad_calendar "*-*-* 25:99:00"
SHA3="$(git rev-parse HEAD)"
git checkout -q "$SHA2"
# Une ligne valide, puis échec : sans garde, l'installeur retirerait tous les autres timers.
sed -i 's|^    main()$|    sys.stdout.write("e2e_probe\\t*-*-* 04:00:00 Europe/Paris\\t-\\n"); sys.stdout.flush(); sys.exit(1)|' \
  apps/jobs/systemd.py
grep -q 'sys.exit(1)' apps/jobs/systemd.py || die "SHA4 : main() de jobs.systemd introuvable"
git commit -qam "e2e: manifeste en échec"
SHA4="$(git rev-parse HEAD)"
ok "SHA1=${SHA1:0:7} SHA2=${SHA2:0:7} SHA3=${SHA3:0:7} SHA4=${SHA4:0:7}"

UNITS=/etc/systemd/system
active() { [[ "$(readlink /opt/biowatch/app)" == "releases/$1" ]]; }
deploy() { git archive --format=tar.gz "$1" | "${SSH[@]}" "deploy $1"; }

# ── Commandes refusées par la clé ────────────────────────────────────────

section "La clé CI n'exécute que la liste fermée"
refused "commande arbitraire"          "${SSH[@]}" id
refused "injection après deploy"       "${SSH[@]}" "deploy ${SHA1}; id"
refused "SHA invalide"                 "${SSH[@]}" "deploy abc"
refused "commande vide"                "${SSH[@]}" ""
# Relais TCP vers sshd lui-même : sans `restrict`, on lirait sa bannière.
refused "relais ssh -W" bash -c 'timeout 10 "$@" -W 127.0.0.1:2222 </dev/null | grep -q SSH-2.0' _ "${SSH[@]}"

# ── Déploiements ─────────────────────────────────────────────────────────

section "deploy SHA1"
deploy "$SHA1" >/tmp/d1.log 2>&1 || { cat /tmp/d1.log; die "deploy SHA1"; }
check "SHA1 actif" active "$SHA1"
check "marqueur .env.prod posé" test -f /opt/biowatch/app/.env.prod
check "timer Sentinel mensuel" grep -q "OnCalendar=\*-\*-05..10 01:30:00 Europe/Paris" \
  "${UNITS}/biowatch-job@extract_sentinel_monthly.timer"
check "plafond mémoire Sentinel" grep -q "MemoryMax=2200M" \
  "${UNITS}/biowatch-job@extract_sentinel_monthly.service.d/biowatch-limits.conf"

section "Le code déployé tourne sous biowatch-jobs"
set +e
out="$(runuser -u biowatch-jobs -- env -i PATH=/usr/bin:/bin \
  POSTGRES_USER=x POSTGRES_PASSWORD=x POSTGRES_DB=biowatch_prod \
  POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5999 BIOWATCH_DATA_ROOT=/var/lib/biowatch/jobs \
  /opt/biowatch/app/.venv/bin/biowatch-jobs --env prod init_db 2>&1)"
set -e
# Pas de base dans le conteneur : arriver jusqu'au refus de connexion prouve que
# le code est lisible et que le marqueur prod est accepté.
check "--env prod va jusqu'à la connexion DB" grep -q "Connection refused" <<<"$out"
check "dépendances natives importables (rasterio)" runuser -u biowatch-jobs -- env -i \
  /opt/biowatch/app/.venv/bin/python -c "import rasterio, jobs.sentinel"
refused "biowatch-jobs écrit dans le code" runuser -u biowatch-jobs -- touch /opt/biowatch/app/x

section "deploy SHA2 (job planifié + plafond)"
deploy "$SHA2" >/tmp/d2.log 2>&1 || { cat /tmp/d2.log; die "deploy SHA2"; }
check "SHA2 actif" active "$SHA2"
check "timer du job ajouté" test -f "${UNITS}/biowatch-job@e2e_probe.timer"
check "plafond du job ajouté" grep -q "MemoryMax=512M" \
  "${UNITS}/biowatch-job@e2e_probe.service.d/biowatch-limits.conf"
deploy "$SHA2" >/tmp/d2b.log 2>&1
check "redéployer un SHA réutilise la release" grep -q "réutilisée" /tmp/d2b.log

section "deploy SHA3 : manifeste refusé → rien n'est activé"
refused "deploy SHA3" bash -c 'git archive --format=tar.gz "$1" | "${@:2}" "deploy $1" >/tmp/d3.log 2>&1' _ "$SHA3" "${SSH[@]}"
# Le refus doit venir de l'installeur, APRÈS une construction réussie : c'est
# précisément le cas où l'ancien ordre aurait déjà activé le nouveau code.
check "release SHA3 construite, refusée à l'installation des timers" bash -c \
  "grep -q 'release ${SHA3} construite' /tmp/d3.log && grep -q 'OnCalendar refusé' /tmp/d3.log"
check "SHA2 toujours actif" active "$SHA2"
check "aucun timer pour le job refusé" test ! -e "${UNITS}/biowatch-job@e2e_bad_calendar.timer"
check "timers de SHA2 intacts" test -f "${UNITS}/biowatch-job@e2e_probe.timer"

section "deploy SHA4 : manifeste en échec → timers intacts"
sum_units() { find "$UNITS" -path '*biowatch-job@*' -type f -exec sha256sum {} + | sort; }
before="$(sum_units)"
refused "deploy SHA4" bash -c 'git archive --format=tar.gz "$1" | "${@:2}" "deploy $1" >/tmp/d4.log 2>&1' _ "$SHA4" "${SSH[@]}"
check "refus sur la génération du manifeste" grep -q "manifeste de ${SHA4} non généré" /tmp/d4.log
check "installeur jamais appelé" bash -c '! grep -q "\[timers\]" /tmp/d4.log'
check "SHA2 toujours actif" active "$SHA2"
check "timers présents avant SHA4" test -n "$before"
after="$(sum_units)"
check "timers et plafonds inchangés" test "$before" = "$after"

section "Retours arrière"
"${SSH[@]}" rollback </dev/null >/tmp/r1.log 2>&1 || { cat /tmp/r1.log; die "rollback"; }
check "rollback previous → SHA1" active "$SHA1"
check "timer du job retiré" test ! -e "${UNITS}/biowatch-job@e2e_probe.timer"
check "plafond du job retiré" test ! -e "${UNITS}/biowatch-job@e2e_probe.service.d"
# Le log est écrit par root (le script), pas par biowatch-ci : voulu.
# shellcheck disable=SC2024
sudo -u biowatch-ci /usr/local/sbin/biowatch-ci-deploy rollback "$SHA2" >/tmp/r2.log 2>&1 \
  || { cat /tmp/r2.log; die "rollback SHA2"; }
check "rollback explicite depuis le VPS → SHA2" active "$SHA2"
check "timer du job restauré" test -f "${UNITS}/biowatch-job@e2e_probe.timer"
check "status répond" bash -c '"$@" status </dev/null | grep -q "release active"' _ "${SSH[@]}"

# ── Manifestes hostiles et sudo ──────────────────────────────────────────

section "biowatch-install-timers rejette un manifeste hostile"
installer=/usr/local/sbin/biowatch-install-timers
refused "directive injectée"   bash -c "printf 'evil\tdaily\t-\nExecStart=/bin/sh\n' | $installer"
refused "colonne manquante"    bash -c "printf 'evil\tdaily\n' | $installer"
refused "colonne en trop"      bash -c "printf 'evil\tdaily\t-\tUser=root\n' | $installer"
refused "nom invalide"         bash -c "printf 'Evil\tdaily\t-\n' | $installer"
refused "calendrier invalide"  bash -c "printf 'evil\tnot a calendar\t-\n' | $installer"
refused "plafond invalide"     bash -c "printf 'evil\tdaily\tinfinity\n' | $installer"
refused "doublon"              bash -c "printf 'a\tdaily\t-\na\tdaily\t-\n' | $installer"

section "biowatch-ci ne peut rien lancer d'autre"
refused "installeur avec argument" runuser -u biowatch-ci -- sudo -n "$installer" /etc/shadow
refused "shell en deploy"          runuser -u biowatch-ci -- sudo -n -u deploy /bin/sh -c id
refused "shell en root"            runuser -u biowatch-ci -- sudo -n /bin/sh -c id

section "Droits du code déployé"
check "tout est au groupe biowatch" bash -c \
  '[[ -z "$(find /opt/biowatch ! -group biowatch ! -type l -print -quit)" ]]'
check "répertoires setgid" bash -c \
  '[[ -z "$(find /opt/biowatch/releases /opt/biowatch/python -type d ! -perm -g+s -print -quit)" ]]'
cd "${INFRA}/vps" && ./40-service-users.sh >/tmp/40-ter.log 2>&1
check "isolation de #25 conforme après déploiements" grep -q "isolation conforme" /tmp/40-ter.log

# ── Verdict ──────────────────────────────────────────────────────────────

echo
if (( FAILURES > 0 )); then
  printf '\033[1;31m%d vérification(s) en échec\033[0m\n' "$FAILURES"
  exit 1
fi
printf '\033[1;32mDéploiement des jobs : tout est vert\033[0m\n'
