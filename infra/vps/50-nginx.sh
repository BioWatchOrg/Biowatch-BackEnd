#!/usr/bin/env bash
# 50-nginx.sh — issue #26.
#
# Installe Nginx comme unique point d'entrée HTTP(S) du VPS :
#
#   socle      80 → redirection HTTPS (+ challenge ACME), 443 inclut
#              NGINX_SERVICES_DIR/*.conf
#   frontend   sert FRONTEND_DIR, écrit par deploy, lu par www-data
#   api        /v1/* et /health réservés (503) jusqu'à #80
#
# Tant que domaine et certificat ne sont pas tranchés, nginx écoute sur la
# BOUCLE LOCALE avec un certificat auto-signé (config.sh) : rien n'est
# exposé, même si UFW autorise 80 et 443. Test par tunnel SSH.
#
# Ne touche ni à SSH ni à UFW : il ne peut pas couper l'accès, d'où
# l'absence de filet armé. Une configuration invalide est refusée par
# « nginx -t » AVANT tout démarrage, et les fichiers sont restaurés.
#
#   ./50-nginx.sh --dry-run
#   ./50-nginx.sh

USAGE="Usage: ./50-nginx.sh [--dry-run]

Installe Nginx et la configuration BioWatch (socle HTTPS + un fichier par
service), pose la page placeholder du frontend, puis PROUVE le résultat
par de vraies requêtes. Idempotent. Aucun accès SSH ni règle UFW modifié.

  --dry-run   affiche les changements sans les appliquer"

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
init_script "${BASH_SOURCE[0]}" "$@"
require_root
init_log
enable_error_trap

banner "#26 — Nginx + HTTPS"

NGINX_SRC="${LIB_DIR}/nginx"
SNAKEOIL_CERT="/etc/ssl/certs/ssl-cert-snakeoil.pem"
POLICY_RC="/usr/sbin/policy-rc.d"

# Exposé = écoute sur autre chose que la boucle locale.
nginx_exposed() { [[ "$NGINX_LISTEN_V4" != 127.* || "$NGINX_LISTEN_V6" != "[::1]" ]]; }

# ── Préconditions ────────────────────────────────────────────────────────

step "Vérification des préconditions"

[[ -f "${NGINX_SRC}/biowatch.conf" && -d "${NGINX_SRC}/biowatch.d" \
   && -f "${NGINX_SRC}/placeholder/index.html" ]] \
  || die "${NGINX_SRC} incomplet — copier infra/vps/ en entier sur le VPS"
ok "modèles présents dans ${NGINX_SRC}"

id -u "$DEPLOY_ACCOUNT" >/dev/null 2>&1 \
  || die "compte ${DEPLOY_ACCOUNT} absent — lancer 40-service-users.sh d'abord"
id -u "$NGINX_USER" >/dev/null 2>&1 \
  || die "compte ${NGINX_USER} absent — inattendu sur Ubuntu"
command -v curl >/dev/null 2>&1 || die "curl introuvable — indispensable aux vérifications"
ok "comptes ${DEPLOY_ACCOUNT} et ${NGINX_USER} présents, curl disponible"

if nginx_exposed; then
  warn "écoute PUBLIQUE (${NGINX_LISTEN_V4}, ${NGINX_LISTEN_V6}) — nginx sera joignable depuis Internet"
  [[ "$NGINX_TLS_CERT" != "$SNAKEOIL_CERT" ]] \
    || die "exposition demandée avec le certificat auto-signé — poser d'abord un certificat Let's Encrypt"
  if ufw status 2>/dev/null | grep -qE '^443/tcp\s+ALLOW'; then
    ok "UFW autorise 443/tcp"
  else
    warn "UFW n'autorise pas 443/tcp (ou est inactif) — vérifier avec 30-firewall.sh"
  fi
else
  ok "écoute sur la boucle locale uniquement (${NGINX_LISTEN_V4}, ${NGINX_LISTEN_V6}) — rien d'exposé"
fi

# ── Paquets ──────────────────────────────────────────────────────────────

# Le paquet nginx démarre son site par défaut sur 0.0.0.0:80 dès
# l'installation. UFW autorisant 80, il serait joignable depuis Internet
# jusqu'à ce que notre configuration le remplace. policy-rc.d (mécanisme
# Debian, respecté par invoke-rc.d et deb-systemd-invoke) l'interdit le
# temps de l'apt install. Le fichier bloque TOUT démarrage de service par
# dpkg : il est retiré dès l'installation finie, et à la sortie du script
# quoi qu'il arrive.
block_service_autostart() {
  if (( DRY_RUN )); then
    dry "écrire ${POLICY_RC} (exit 101) : aucun service démarré pendant l'installation"
    return 0
  fi
  [[ ! -e "$POLICY_RC" ]] \
    || die "${POLICY_RC} existe déjà — à examiner à la main, rien n'a été installé"
  trap 'rm -f "$POLICY_RC"' EXIT
  printf '#!/bin/sh\n# Posé par 50-nginx.sh le temps de apt install.\nexit 101\n' > "$POLICY_RC"
  chmod 755 "$POLICY_RC"
  ok "démarrage automatique des services suspendu"
}

unblock_service_autostart() {
  if (( DRY_RUN )); then dry "supprimer ${POLICY_RC}"; return 0; fi
  rm -f "$POLICY_RC"
  trap - EXIT
  ok "démarrage automatique des services rétabli"
}

install_packages() {
  step "Paquets nginx et ssl-cert"
  local missing=()
  command -v nginx >/dev/null 2>&1 || missing+=(nginx)
  # ssl-cert fournit le certificat auto-signé (snakeoil) et le génère à
  # l'installation.
  dpkg -s ssl-cert >/dev/null 2>&1 || missing+=(ssl-cert)

  if (( ${#missing[@]} == 0 )); then
    log "nginx et ssl-cert déjà installés"
  else
    block_service_autostart
    run apt-get update -qq
    # via env : run exécute "$@", une affectation en tête serait prise pour
    # le nom de la commande.
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
    unblock_service_autostart
  fi

  # En dry-run l'installation n'a fait qu'être affichée.
  if ! command -v nginx >/dev/null 2>&1; then
    dry "nginx serait installé puis interrogé ici"
    return 0
  fi
  ok "$(nginx -v 2>&1)"

  # Ceinture : si nginx tourne avec une autre configuration que la nôtre
  # (site par défaut sur 0.0.0.0:80), on l'arrête avant d'aller plus loin.
  if systemctl is-active --quiet nginx && [[ ! -L "$NGINX_SITE_LINK" ]]; then
    warn "nginx tourne déjà avec la configuration du paquet — arrêt"
    run systemctl stop nginx
  fi
}

ensure_certificate() {
  step "Certificat TLS"
  if [[ -f "$NGINX_TLS_CERT" && -f "$NGINX_TLS_KEY" ]]; then
    ok "certificat présent : ${NGINX_TLS_CERT}"
  elif [[ "$NGINX_TLS_CERT" == "$SNAKEOIL_CERT" ]]; then
    run make-ssl-cert generate-default-snakeoil
    done_ok "certificat auto-signé généré : ${NGINX_TLS_CERT}"
  else
    die "certificat ${NGINX_TLS_CERT} ou clé ${NGINX_TLS_KEY} introuvable"
  fi
  if [[ "$NGINX_TLS_CERT" == "$SNAKEOIL_CERT" ]]; then
    warn "certificat AUTO-SIGNÉ : valable pour tester par tunnel, pas pour exposer"
  fi
}

# ── Site par défaut ──────────────────────────────────────────────────────

disable_default_site() {
  step "Site par défaut du paquet"
  # Il se déclare lui aussi default_server sur 80 : nginx -t refuserait
  # les deux. Le fichier reste dans sites-available, seul le lien part.
  if (( DRY_RUN )) && ! command -v nginx >/dev/null 2>&1; then
    dry "rm -f ${NGINX_DEFAULT_SITE_LINK}  (posé par l'installation du paquet)"
    return 0
  fi
  if [[ -L "$NGINX_DEFAULT_SITE_LINK" || -e "$NGINX_DEFAULT_SITE_LINK" ]]; then
    backup_file "$NGINX_DEFAULT_SITE_LINK"
    run rm -f "$NGINX_DEFAULT_SITE_LINK"
    done_ok "site par défaut désactivé"
  else
    log "site par défaut déjà désactivé"
  fi
}

# ── Répertoires ──────────────────────────────────────────────────────────

ensure_directories() {
  step "Répertoires"
  run install -d -m 755 -o root -g root "$NGINX_SERVICES_DIR"
  done_ok "${NGINX_SERVICES_DIR}  root:root 755"

  run install -d -m 755 -o root -g root "$NGINX_ACME_ROOT"
  done_ok "${NGINX_ACME_ROOT}  root:root 755 (challenge Let's Encrypt)"

  run install -d -m 755 -o root -g root "$(dirname "$FRONTEND_DIR")"
  # Setgid : tout ce que deploy dépose hérite du groupe de nginx, donc
  # lisible par lui sans chgrp. Aucun droit d'écriture pour le groupe : un
  # nginx compromis ne peut pas réécrire le site.
  run install -d -m 2750 -o "$DEPLOY_ACCOUNT" -g "$NGINX_USER" "$FRONTEND_DIR"
  # Le bit setgid n'est pas posé par install -d sur toutes les versions.
  run chmod 2750 "$FRONTEND_DIR"
  done_ok "${FRONTEND_DIR}  ${DEPLOY_ACCOUNT}:${NGINX_USER} 2750"
}

install_placeholder() {
  step "Page placeholder"
  # Uniquement sur un répertoire vide : rejouer le script après le premier
  # déploiement du frontend ne doit pas l'écraser.
  if [[ -d "$FRONTEND_DIR" && -n "$(ls -A "$FRONTEND_DIR")" ]]; then
    log "${FRONTEND_DIR} non vide — placeholder non posé (frontend déjà déployé ?)"
    return 0
  fi
  run install -m 640 -o "$DEPLOY_ACCOUNT" -g "$NGINX_USER" \
    "${NGINX_SRC}/placeholder/index.html" "${FRONTEND_DIR}/index.html"
  done_ok "placeholder posé : ${FRONTEND_DIR}/index.html"
}

# ── Configuration ────────────────────────────────────────────────────────

# Remplace les marqueurs des modèles par les valeurs de config.sh. Un
# marqueur oublié ferait échouer nginx -t avec un message obscur : on le
# nomme ici. Appelé en substitution de commande : le die fait échouer
# l'affectation, donc le trap d'erreur du script principal.
render() {
  local src="$1" out left
  out="$(sed -e "s|__SERVER_NAME__|${NGINX_SERVER_NAME}|g" \
             -e "s|__LISTEN_V4__|${NGINX_LISTEN_V4}|g" \
             -e "s|__LISTEN_V6__|${NGINX_LISTEN_V6}|g" \
             -e "s|__TLS_CERT__|${NGINX_TLS_CERT}|g" \
             -e "s|__TLS_KEY__|${NGINX_TLS_KEY}|g" \
             -e "s|__ACME_ROOT__|${NGINX_ACME_ROOT}|g" \
             -e "s|__SERVICES_DIR__|${NGINX_SERVICES_DIR}|g" \
             -e "s|__FRONTEND_DIR__|${FRONTEND_DIR}|g" "$src")"
  left="$(grep -o '__[A-Z0-9_]*__' <<<"$out" | sort -u | tr '\n' ' ' || true)"
  [[ -z "$left" ]] || die "marqueur(s) non remplacé(s) dans ${src} : ${left}"
  printf '%s\n' "$out"
}

install_config() {
  step "Configuration"
  ensure_backup_dir

  local content src f
  content="$(render "${NGINX_SRC}/biowatch.conf")"
  write_file "$NGINX_SITE" 644 <<<"$content"

  for src in "${NGINX_SRC}"/biowatch.d/*.conf; do
    content="$(render "$src")"
    write_file "${NGINX_SERVICES_DIR}/$(basename "$src")" 644 <<<"$content"
  done

  # Non supprimés : quelqu'un les a posés pour une raison. Mais nginx les
  # charge, et ils échappent à la relecture du dépôt.
  for f in "${NGINX_SERVICES_DIR}"/*.conf; do
    [[ -e "$f" ]] || continue
    [[ -e "${NGINX_SRC}/biowatch.d/$(basename "$f")" ]] \
      || warn "${f} n'est pas versionné dans le dépôt — nginx le charge quand même"
  done

  if [[ -L "$NGINX_SITE_LINK" ]]; then
    log "site déjà activé"
  else
    backup_file "$NGINX_SITE_LINK"
    run ln -s "$NGINX_SITE" "$NGINX_SITE_LINK"
    done_ok "site activé : ${NGINX_SITE_LINK}"
  fi
}

reload_nginx() {
  step "Validation et (re)démarrage"
  if (( DRY_RUN )); then
    dry "nginx -t, puis systemctl enable --now nginx et systemctl reload nginx"
    return 0
  fi
  # Un refus ici déclenche le trap : fichiers restaurés, et nginx n'a été
  # ni démarré ni rechargé avec la configuration refusée.
  nginx -t
  run systemctl enable --quiet --now nginx
  run systemctl reload nginx
  ok "nginx actif"
}

# ── Preuves ──────────────────────────────────────────────────────────────
# Une configuration acceptée par nginx -t peut encore servir la mauvaise
# chose. On interroge donc nginx comme le ferait un client.

FAILURES=0
fail() { err "$*"; FAILURES=$((FAILURES + 1)); }

# Toujours en boucle locale : c'est la même instance qui répond en public.
# -k : le certificat auto-signé ne serait pas reconnu, et ce n'est pas lui
# qu'on teste ici mais ce que nginx sert.
expect_status() {
  local url="$1" want="$2" got
  got="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "$url")"
  if [[ "$got" == "$want" ]]; then
    ok "GET ${url} → ${got}"
  else
    fail "GET ${url} → ${got:-aucune réponse}, attendu ${want}"
  fi
}

expect_redirect() {
  local url="$1" want="$2" got
  got="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 5 "$url")"
  if [[ "$got" == "301 ${want}" ]]; then
    ok "GET ${url} → 301 ${want}"
  else
    fail "GET ${url} → ${got:-aucune réponse}, attendu 301 ${want}"
  fi
}

# Même sonde que 40-service-users.sh : l'opération réelle plutôt que
# « test -w », que rust-coreutils évalue sans les groupes secondaires.
can_write() {
  local u="$1" dir="$2" probe
  probe="$(runuser -u "$u" -- mktemp -p "$dir" .biowatch-probe.XXXXXX 2>/dev/null)" \
    || return 1
  rm -f -- "$probe"
}

# Tout port en écoute hors boucle locale est joignable sans passer par
# nginx, à UFW près. Admis : SSH, et les ports web seulement si nginx est
# volontairement exposé.
check_public_listeners() {
  local allowed=" ${SSH_PORT} " addr host port bad=0
  nginx_exposed && allowed+="${FIREWALL_TCP_PORTS[*]} "
  while read -r _ _ _ addr _; do
    host="${addr%:*}"; port="${addr##*:}"
    case "$host" in 127.*|"[::1]"|*%lo) continue ;; esac
    if [[ "$allowed" != *" ${port} "* ]]; then
      fail "port ${port} en écoute sur ${host} — joignable depuis l'extérieur"
      bad=1
    fi
  done < <(ss -tlnH)
  (( bad )) || ok "hors boucle locale, seuls écoutent des ports admis :${allowed}"
}

verify() {
  step "Preuves (requêtes réelles)"

  if (( DRY_RUN )); then
    dry "interroger le port 80 (redirection, ACME) et le 443 (frontend, SPA, assets, /v1/, /health)"
    dry "vérifier TLS, les en-têtes, les droits sur ${FRONTEND_DIR} et les ports en écoute"
    return 0
  fi

  # Désactivé le temps des tests : on veut la liste complète des écarts.
  set +e
  trap - ERR

  local http="http://127.0.0.1" https="https://127.0.0.1"

  expect_redirect "${http}/carte?x=1"                    "${https}/carte?x=1"
  expect_status   "${http}/.well-known/acme-challenge/x" 404   # servi, pas redirigé

  expect_status "${https}/"                     200
  expect_status "${https}/carte/une-route-spa"  200   # fallback vers index.html
  expect_status "${https}/assets/absent.js"     404   # pas de fallback sur les assets
  expect_status "${https}/v1/zones"             503   # réservé à #80
  expect_status "${https}/health"               503   # réservé à #80

  local headers server
  headers="$(curl -skI --max-time 5 "${https}/" | tr -d '\r')"
  server="$(grep -i '^server:' <<<"$headers")"
  if [[ "$server" == */* ]]; then
    fail "en-tête « ${server} » : la version de nginx est exposée"
  else
    ok "en-tête « ${server:-Server absent} » : version masquée"
  fi
  if grep -qi '^x-content-type-options: nosniff' <<<"$headers"; then
    ok "en-têtes de sécurité présents"
  else
    fail "en-têtes de sécurité absents de ${https}/"
  fi

  # Pas de test « TLS 1.1 refusé » : l'OpenSSL d'Ubuntu 26.04 le refuse au
  # niveau système, même si nginx l'autorisait. Le test afficherait un OK
  # qui ne prouve rien de notre configuration.
  local v
  for v in 1_2 1_3; do
    if openssl s_client -connect 127.0.0.1:443 "-tls${v}" </dev/null >/dev/null 2>&1; then
      ok "TLS ${v/_/.} accepté"
    else
      fail "TLS ${v/_/.} refusé — profil TLS incorrect"
    fi
  done

  if can_write "$DEPLOY_ACCOUNT" "$FRONTEND_DIR"; then
    ok "${DEPLOY_ACCOUNT} peut écrire dans ${FRONTEND_DIR}"
  else
    fail "${DEPLOY_ACCOUNT} devrait pouvoir écrire dans ${FRONTEND_DIR}"
  fi
  if can_write "$NGINX_USER" "$FRONTEND_DIR"; then
    fail "${NGINX_USER} ne devrait PAS pouvoir écrire dans ${FRONTEND_DIR}"
  else
    ok "${NGINX_USER} ne peut pas écrire dans ${FRONTEND_DIR}"
  fi

  check_public_listeners

  enable_error_trap
  (( FAILURES == 0 )) || die "${FAILURES} vérification(s) en échec — configuration NON conforme"
  ok "nginx conforme"
}

# ── Déroulé ──────────────────────────────────────────────────────────────

install_packages
ensure_certificate
disable_default_site
ensure_directories
install_placeholder
install_config
reload_nginx
verify

# ── Sortie ───────────────────────────────────────────────────────────────

if nginx_exposed; then
  cat <<EOF

${C_GRN}${C_BLD}Nginx en place, EXPOSÉ.${C_RST}

Validation externe, depuis ton poste (HORS du VPS) :

    curl -I http://<domaine>/              # attendu : 301 vers https://
    curl -I https://<domaine>/             # attendu : 200, certificat valide

EOF
else
  cat <<EOF

${C_GRN}${C_BLD}Nginx en place, sur la boucle locale uniquement.${C_RST}

Rien n'est exposé. Depuis ton poste, la preuve que les ports restent fermés :

    nc -zv -w5 <IP_DU_VPS> 443             # attendu : refusé
    nc -zv -w5 <IP_DU_VPS> 80              # attendu : refusé

Et la chaîne HTTPS, par tunnel SSH :

    ssh -N -L 8443:127.0.0.1:443 biowatch
    curl -k https://localhost:8443/         # dans un autre terminal : placeholder

-k car le certificat est auto-signé. Exposer : voir la section Nginx du
README (domaine, Let's Encrypt, puis NGINX_LISTEN_* dans config.sh).

EOF
fi

cat <<EOF
Brancher l'API (#80) : remplacer nginx/biowatch.d/api.conf dans le dépôt,
puis rejouer ce script. Déployer le frontend (Biowatch-Frontend#76) : écrire
dans ${FRONTEND_DIR} sous l'identité ${DEPLOY_ACCOUNT}, sans toucher à nginx.

EOF
