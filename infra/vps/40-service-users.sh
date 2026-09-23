#!/usr/bin/env bash
# 40-service-users.sh — issue #25.
#
# Crée les comptes système qui exécutent et déploient BioWatch, et pose
# l'arborescence qui limite chacun à ce dont il a besoin :
#
#   deploy          écrit le code dans APP_DIR, n'exécute rien
#   biowatch-api    lit APP_DIR, écrit dans son état et ses logs
#   biowatch-jobs   idem, dans des répertoires distincts de l'API
#
# Aucun de ces comptes n'a de shell, de mot de passe, de clé, de sudo, ni
# ne figure dans le groupe SSH. Ce script ne touche ni à SSH ni au réseau :
# il ne peut pas couper l'accès, d'où l'absence de filet armé.
#
#   ./40-service-users.sh --dry-run
#   ./40-service-users.sh

USAGE="Usage: ./40-service-users.sh [--dry-run]

Crée les comptes de service (${SERVICE_RUNTIME_ACCOUNTS[*]:-} ${DEPLOY_ACCOUNT:-})
et leurs répertoires, puis PROUVE l'isolation en testant les accès sous
chaque identité. Idempotent. Aucun accès SSH n'est modifié.

  --dry-run   affiche les changements sans les appliquer"

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
init_script "${BASH_SOURCE[0]}" "$@"
require_root
init_log
enable_error_trap

banner "#25 — Comptes de service et isolation"

NOLOGIN="/usr/sbin/nologin"
ALL_SERVICE_ACCOUNTS=("${SERVICE_RUNTIME_ACCOUNTS[@]}" "$DEPLOY_ACCOUNT")

# biowatch-api → api : le préfixe est déjà dans le répertoire parent.
short_name() { printf '%s' "${1#biowatch-}"; }

# ── Préconditions ────────────────────────────────────────────────────────

step "Vérification des préconditions"
[[ -x "$NOLOGIN" ]] || die "${NOLOGIN} introuvable — shell de refus indispensable"
command -v runuser >/dev/null 2>&1 || die "runuser introuvable (util-linux)"
ok "nologin et runuser disponibles"

# ensure_directories fixe propriétaire et mode de APP_DIR : sur un
# répertoire déjà occupé, il en prendrait possession en silence et en
# retirerait l'accès à ses utilisateurs actuels. Vide, il est sans enjeu.
if [[ -d "$APP_DIR" ]]; then
  app_owner="$(stat -c '%U:%G' "$APP_DIR")"
  if [[ "${app_owner%%:*}" != "$DEPLOY_ACCOUNT" && -n "$(ls -A "$APP_DIR")" ]]; then
    err "${APP_DIR} existe déjà, appartient à ${app_owner} et n'est pas vide :"
    find "$APP_DIR" -mindepth 1 -maxdepth 1 -printf '            %M %u:%g %p\n' >&2
    die "déplacer son contenu ou changer APP_DIR dans config.sh — rien n'a été modifié"
  fi
fi

# ── Groupe partagé ───────────────────────────────────────────────────────

ensure_service_group() {
  step "Groupe ${SERVICE_GROUP}"
  if getent group "$SERVICE_GROUP" >/dev/null; then
    log "groupe ${SERVICE_GROUP} déjà présent"
  else
    run groupadd --system "$SERVICE_GROUP"
    done_ok "groupe ${SERVICE_GROUP} créé"
  fi
}

# ── Comptes ──────────────────────────────────────────────────────────────

create_service_account() {
  local u="$1" home uid
  home="${SERVICE_STATE_ROOT}/$(short_name "$u")"

  if uid="$(id -u "$u" 2>/dev/null)"; then
    # Un compte humain portant ce nom (un « deploy » créé à la main, par
    # exemple) a peut-être un shell, des clés, un usage qu'on ignore. Le
    # convertir en silence casserait quelque chose ; c'est à trancher.
    if (( uid >= 1000 )); then
      die "${u} existe déjà comme compte HUMAIN (uid ${uid}) — à examiner à la main"
    fi
    log "compte ${u} déjà présent (uid ${uid})"
  else
    # Un groupe homonyme orphelin fait échouer --user-group : on le réutilise.
    local group_opt=(--user-group)
    getent group "$u" >/dev/null && group_opt=(--gid "$u")
    # Home hors de /home, non créé ici : ensure_directories le pose avec
    # le bon mode. Sans home inscriptible, uv et pip n'ont pas de cache.
    run useradd --system "${group_opt[@]}" \
      --shell "$NOLOGIN" --home-dir "$home" --no-create-home \
      --comment "BioWatch service ${u}" "$u"
    done_ok "compte ${u} créé"
  fi

  # Appliqué même aux comptes existants : c'est l'état voulu, pas une
  # étape de création. Rejouer le script corrige toute dérive.
  # Condition sur le shell : usermod affiche « no changes » quand il n'y a
  # rien à faire, bruit qui masquerait une vraie correction.
  if [[ "$(getent passwd "$u" | cut -d: -f7 || true)" != "$NOLOGIN" ]]; then
    run usermod --shell "$NOLOGIN" "$u"
  fi
  run usermod --lock "$u"
}

# ── Répertoires ──────────────────────────────────────────────────────────

# install -d fixe mode et propriétaire, y compris sur un répertoire existant.
ensure_dir() {
  local path="$1" mode="$2" owner="$3" group="$4"
  run install -d -m "$mode" -o "$owner" -g "$group" "$path"
  done_ok "${path}  ${owner}:${group} ${mode}"
}

ensure_directories() {
  step "Répertoires"
  local u

  ensure_dir "$SERVICE_STATE_ROOT" 755 root root
  ensure_dir "$SERVICE_LOG_ROOT"   755 root root

  for u in "${ALL_SERVICE_ACCOUNTS[@]}"; do
    ensure_dir "${SERVICE_STATE_ROOT}/$(short_name "$u")" 750 "$u" "$u"
  done
  for u in "${SERVICE_RUNTIME_ACCOUNTS[@]}"; do
    ensure_dir "${SERVICE_LOG_ROOT}/$(short_name "$u")" 750 "$u" "$u"
  done

  # Setgid : tout ce que deploy crée dessous hérite du groupe de service,
  # lisible par l'API et les jobs sans chgrp. Aucun droit d'écriture pour
  # le groupe : un service compromis ne peut pas réécrire le code.
  ensure_dir "$APP_DIR" 2750 "$DEPLOY_ACCOUNT" "$SERVICE_GROUP"
  # Le bit setgid n'est pas posé par install -d sur toutes les versions.
  run chmod 2750 "$APP_DIR"

  # Secrets lus par systemd (EnvironmentFile), qui s'exécute en root AVANT
  # de changer d'identité : les services reçoivent leurs variables sans
  # jamais pouvoir lire le fichier, ni celui d'un autre service.
  ensure_dir "$SERVICE_CONFIG_DIR" 700 root root
}

# ── Preuves ──────────────────────────────────────────────────────────────
# Lire /etc/passwd ne prouve pas l'isolation : un ACL, un umask ou un
# répertoire préexistant peuvent la contredire. On teste donc les accès en
# se mettant réellement à la place de chaque compte.

FAILURES=0
fail() { err "$*"; FAILURES=$((FAILURES + 1)); }

# can <compte> <read|write> <répertoire> — tente l'opération réelle plutôt
# que « test -r/-w » : sur Ubuntu 26.04, test vient de rust-coreutils et
# ignore les groupes secondaires, ce qui fausse le verdict sur APP_DIR.
# runuser n'invoque pas le shell du compte : nologin ne gêne pas.
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

expect_can()    { if can "$1" "$2" "$3"; then ok "${1} peut ${4}"; else fail "${1} devrait pouvoir ${4}"; fi; }
expect_cannot() { if can "$1" "$2" "$3"; then fail "${1} ne devrait PAS pouvoir ${4}"; else ok "${1} ne peut pas ${4}"; fi; }

verify_account() {
  local u="$1" shell home g forbidden sudo_out
  shell="$(getent passwd "$u" | cut -d: -f7)"
  home="$(getent passwd "$u" | cut -d: -f6)"

  if [[ "$shell" == "$NOLOGIN" ]]; then
    ok "${u} : shell ${shell}"
  else
    fail "${u} : shell ${shell} au lieu de ${NOLOGIN}"
  fi

  if [[ "$(passwd -S "$u" | awk '{print $2}')" == "L" ]]; then
    ok "${u} : mot de passe verrouillé"
  else
    fail "${u} : mot de passe NON verrouillé"
  fi

  # nologin bloque le shell, pas un « ssh -N » de tunnel : l'absence de
  # clé et de groupe SSH est ce qui interdit réellement la connexion.
  if [[ ! -e "${home}/.ssh/authorized_keys" ]]; then
    ok "${u} : aucune clé SSH"
  else
    fail "${u} : ${home}/.ssh/authorized_keys existe — à supprimer"
  fi

  forbidden=""
  for g in $(id -nG "$u"); do
    if [[ " ${SERVICE_FORBIDDEN_GROUPS[*]} " == *" ${g} "* ]]; then
      forbidden+="${g} "
    fi
  done
  if [[ -z "$forbidden" ]]; then
    ok "${u} : aucun groupe privilégié ($(id -nG "$u"))"
  else
    fail "${u} : membre de ${forbidden}— retirer avec : gpasswd -d ${u} <groupe>"
  fi

  # LC_ALL=C : le message est traduit selon la locale.
  sudo_out="$(LC_ALL=C sudo -n -l -U "$u" 2>&1 || true)"
  if ! command -v sudo >/dev/null 2>&1; then
    ok "${u} : sudo non installé"
  elif grep -q 'not allowed to run sudo' <<<"$sudo_out"; then
    ok "${u} : aucun droit sudo"
  else
    fail "${u} : dispose de droits sudo — voir « sudo -l -U ${u} »"
  fi
}

verify_isolation() {
  step "Preuves d'isolation (tests exécutés sous chaque identité)"

  if (( DRY_RUN )); then
    dry "vérifier shell, verrou, clés, groupes et sudo de chaque compte"
    dry "tester sous chaque identité les droits sur ${APP_DIR}, ${SERVICE_CONFIG_DIR} et les répertoires des autres"
    return 0
  fi

  # Désactivé le temps des tests : un échec attendu ne doit pas déclencher
  # le trap d'erreur, on veut la liste complète avant de conclure.
  set +e
  trap - ERR

  local u other
  for u in "${ALL_SERVICE_ACCOUNTS[@]}"; do
    verify_account "$u"
  done

  for u in "${SERVICE_RUNTIME_ACCOUNTS[@]}"; do
    expect_can    "$u" read "$APP_DIR" "lire ${APP_DIR}"
    expect_cannot "$u" write "$APP_DIR" "écrire dans ${APP_DIR}"
    expect_can    "$u" write "${SERVICE_STATE_ROOT}/$(short_name "$u")" "écrire son état"
    expect_can    "$u" write "${SERVICE_LOG_ROOT}/$(short_name "$u")" "écrire ses logs"
    for other in "${ALL_SERVICE_ACCOUNTS[@]}"; do
      [[ "$other" == "$u" ]] && continue
      expect_cannot "$u" read "${SERVICE_STATE_ROOT}/$(short_name "$other")" \
        "lire l'état de ${other}"
    done
  done

  expect_can "$DEPLOY_ACCOUNT" write "$APP_DIR" "écrire dans ${APP_DIR}"
  for u in "${SERVICE_RUNTIME_ACCOUNTS[@]}"; do
    expect_cannot "$DEPLOY_ACCOUNT" read "${SERVICE_STATE_ROOT}/$(short_name "$u")" \
      "lire l'état de ${u}"
  done

  for u in "${ALL_SERVICE_ACCOUNTS[@]}"; do
    expect_cannot "$u" read "$SERVICE_CONFIG_DIR" "lire ${SERVICE_CONFIG_DIR}"
  done

  # Ceinture après les bretelles : si 20-harden-ssh.sh n'a pas encore été
  # appliqué, rien au niveau de sshd ne filtre ces comptes.
  if sshd -T 2>/dev/null | grep -qix "allowgroups ${SSH_GROUP}"; then
    ok "sshd : AllowGroups ${SSH_GROUP} actif — aucun compte de service ne peut s'y connecter"
  else
    warn "sshd : AllowGroups ${SSH_GROUP} absent — lancer 20-harden-ssh.sh"
  fi

  enable_error_trap
  (( FAILURES == 0 )) || die "${FAILURES} vérification(s) en échec — isolation NON conforme"
  ok "isolation conforme"
}

# ── Déroulé ──────────────────────────────────────────────────────────────

ensure_service_group

step "Comptes de service"
for account in "${ALL_SERVICE_ACCOUNTS[@]}"; do
  create_service_account "$account"
done
for account in "${SERVICE_RUNTIME_ACCOUNTS[@]}"; do
  run usermod --append --groups "$SERVICE_GROUP" "$account"
done
done_ok "${SERVICE_RUNTIME_ACCOUNTS[*]} dans ${SERVICE_GROUP} (lecture du code)"

ensure_directories
verify_isolation

# ── Sortie ───────────────────────────────────────────────────────────────

cat <<EOF

${C_GRN}${C_BLD}Comptes de service en place.${C_RST}

Toute écriture dans ${APP_DIR} passe par ${DEPLOY_ACCOUNT}, depuis une session membre :

    sudo -u ${DEPLOY_ACCOUNT} <commande de déploiement>

Chaque service systemd DOIT déclarer son compte — modèle d'unité dans la
section « Comptes de service » du README. C'est ce qui ferme le dernier
critère de #25 (« services exécutés avec le bon user »), une fois les
unités de l'API et des jobs écrites.

EOF
