# Configuration VPS — durcissement SSH, firewall et comptes de service

Scripts couvrant les issues **#22** (accès SSH sécurisés), **#28** (firewall UFW)
et **#25** (comptes de service et isolation).

Cible : Ubuntu 26.04 LTS, `sshd` démarré par **activation socket**.

## La règle qui prime sur toutes les autres

**Une session SSH déjà ouverte survit au redémarrage de `sshd`.** Ton terminal
courant continuera de fonctionner même si la nouvelle configuration est
totalement cassée. « Ça marche toujours chez moi » ne prouve donc rien.

La seule preuve valable est une **nouvelle connexion depuis un second
terminal**. Les scripts qui retirent des accès refusent de démarrer sans
cette preuve, et arment une restauration automatique en cas d'oubli.

## Ordre d'exécution

| Script | Issue | Effet | Risque |
|---|---|---|---|
| `00-diagnose.sh` | — | État des lieux | aucun, lecture seule |
| `10-users-and-keys.sh` | #22 | Comptes, clés, sudo, ouvre 50022 **en plus** du 22 | aucun, purement additif |
| `20-harden-ssh.sh` | #22 | Coupe mot de passe, root, port 22 | ⚠️ porte de non-retour |
| `30-firewall.sh` | #28 | UFW deny incoming | ⚠️ |
| `40-service-users.sh` | #25 | Comptes `biowatch-api`, `biowatch-jobs`, `deploy` et leurs répertoires | aucun, ne touche ni SSH ni réseau |

Chaque script accepte `--dry-run`. **Toujours l'utiliser en première passe.**

Entre `10` et `20`, ouvrir un second terminal et vérifier la connexion par clé
sur le nouveau port. `20` refusera de s'exécuter sinon.

## Étape zéro

Avant tout, chaque membre génère sa paire de clés et fournit sa clé publique :
voir [`keys/README.md`](keys/README.md). Sans au moins une clé déposée,
`10-users-and-keys.sh` s'arrête avant d'avoir touché quoi que ce soit.

## Déroulé

Sur le VPS, les scripts sont copiés dans `/opt/biowatch-infra/vps/`
(`root:biowatch-ssh`, 750). `/opt/biowatch` est réservé au code déployé (#25).

```bash
# sur le VPS, dans /opt/biowatch-infra/vps/
./00-diagnose.sh

./10-users-and-keys.sh --dry-run
./10-users-and-keys.sh
```

Depuis un **second terminal**, sur ton poste :

```bash
ssh -p 50022 -i ~/.ssh/biowatch_ed25519 simonr@<IP_DU_VPS>
```

Puis, **depuis cette nouvelle session** :

```bash
./20-harden-ssh.sh --dry-run
./20-harden-ssh.sh
```

Le script arme une restauration automatique. Vérifier une nouvelle connexion,
puis seulement ensuite la désarmer :

```bash
sudo systemctl stop biowatch-ssh-rollback.timer
```

Même schéma pour le firewall :

```bash
./30-firewall.sh --dry-run
./30-firewall.sh
# vérifier depuis l'extérieur, puis :
sudo systemctl stop biowatch-ufw-rollback.timer
```

## Fail2ban : écarté délibérément

Le critère « fail2ban installé et actif » a été **retiré de la DoD de #22** et
reporté sur #26.

Fail2ban répond au brute-force d'authentification. Cette menace n'existe pas
ici : l'accès est exclusivement par clé ed25519, `AllowGroups` rejette tout
compte hors du groupe, le port n'est plus le 22, et UFW n'expose que trois
ports. La jail `sshd` ne bannirait quasiment jamais rien.

Elle introduirait en revanche un risque réel : avec `MaxAuthTries 3`, un membre
dont le client propose plusieurs clés (absence de `IdentitiesOnly yes`) épuise
ses tentatives et se fait bannir alors qu'il est légitime.

**Ce raisonnement devient caduc avec #26.** Nginx exposera une surface
authentifiable et scannable, où fail2ban retrouve son utilité — avec des jails
`nginx-*`, pas la jail `sshd`.

## Si l'accès est perdu

Ne rien faire pendant 10 minutes. Le filet restaure la configuration
précédente et redémarre `sshd` tout seul. Reconnecte-toi ensuite sur
l'ancien port, relis `/var/log/biowatch-vps-setup.log`, corrige, recommence.

### Limite du filet

La restauration ne défait que ce qui est passé par les sauvegardes de fichiers.
Elle **ne supprime pas les comptes déjà créés**. Si `10-users-and-keys.sh`
échoue après avoir créé des comptes — par exemple à l'étape sudoers — le
message d'erreur annonce un retour en arrière, mais les comptes créés
subsistent avec leur clé et un accès SSH fonctionnel.

Ce n'est pas une faille : ces comptes sont ceux qu'on voulait créer, et rejouer
le script termine le travail. Mais ne lis pas « état restauré » comme « rien
n'a changé » — vérifie avec `./00-diagnose.sh`.

Les sauvegardes sont dans `/root/biowatch-vps-backups/<horodatage>/`, en
arborescence miroir. Restauration manuelle :

```bash
sudo cp -a /root/biowatch-vps-backups/<ts>/etc/ssh/. /etc/ssh/
sudo rm -f /etc/ssh/sshd_config.d/00-biowatch-hardening.conf
sudo systemctl daemon-reload && sudo systemctl restart ssh.socket
```

En dernier recours, la console de secours de l'hébergeur (KVM / VNC) reste le
seul chemin qui ne dépend pas de SSH.

## Trois pièges traités par ces scripts

**Activation socket.** `sshd` est démarré par `ssh.socket` : la directive `Port`
de `sshd_config` est **ignorée**. Le port se règle par un drop-in sur le
socket. Beaucoup d'instructions trouvées en ligne échouent silencieusement
sur ce point.

**Ordre des drop-ins.** `sshd` retient la **première** valeur rencontrée, en
lisant `/etc/ssh/sshd_config.d/*.conf` par ordre alphabétique. Le
`50-cloud-init.conf` de l'image impose `PasswordAuthentication yes` : un
fichier de durcissement nommé `99-*` serait lu après, donc ignoré. D'où le
préfixe `00-`. **Ne pas renommer ce fichier.**

**Comptes sans mot de passe et `sudo`.** Le groupe `sudo` exige un mot de passe
que des comptes clé-seule n'ont pas. Sans le drop-in `NOPASSWD`, les membres
seraient incapables d'administrer quoi que ce soit.

## Ajouter les membres restants

Déposer la clé dans `keys/`, déplacer le login de `MEMBERS_PENDING` vers
`MEMBERS` dans `config.sh`, puis rejouer `10-users-and-keys.sh`. Idempotent :
les comptes en place ne sont pas modifiés, aucun accès n'est retiré.

**#22 ne peut pas être fermée** tant que `MEMBERS_PENDING` n'est pas vide : le
critère « connexion SSH testée pour chaque utilisateur » ne serait pas atteint.

## Comptes de service (#25)

```bash
./40-service-users.sh --dry-run
./40-service-users.sh
```

| Compte | Rôle | Écrit dans | Lit |
|---|---|---|---|
| `deploy` | Dépose le code | `/opt/biowatch`, `/var/lib/biowatch/deploy` | — |
| `biowatch-api` | Exécute l'API | `/var/lib/biowatch/api`, `/var/log/biowatch/api` | `/opt/biowatch` |
| `biowatch-jobs` | Exécute les jobs | `/var/lib/biowatch/jobs`, `/var/log/biowatch/jobs` | `/opt/biowatch` |

Aucun de ces comptes n'a de shell (`nologin`), de mot de passe, de clé SSH, de
sudo, ni n'appartient à `biowatch-ssh`, `sudo`, `adm`, `docker` ou `lxd`.
**Ne jamais les ajouter au groupe `docker`** : il équivaut à root.

`/opt/biowatch` est en `2750 deploy:biowatch` : le bit setgid fait hériter du
groupe `biowatch` tout ce que `deploy` y crée, lisible par l'API et les jobs,
jamais inscriptible. Un service compromis ne peut pas réécrire le code.

Déployer se fait depuis une session membre : `sudo -u deploy <commande>`.

Le script ne se contente pas de créer : il **prouve** l'isolation en tentant
réellement chaque accès sous chaque identité (lister, créer un fichier), et
échoue si un seul résultat diffère de l'attendu. Pas de `test -r` : sur
Ubuntu 26.04, rust-coreutils l'évalue sans les groupes secondaires et le
verdict serait faux.

### Secrets

`/etc/biowatch/` est en `700 root`. Les secrets y sont lus par systemd
(`EnvironmentFile=`), qui s'exécute en root **avant** de changer d'identité :
chaque service reçoit ses variables sans pouvoir lire son fichier, ni celui
d'un autre.

### Modèle d'unité systemd

Le critère « services exécutés avec le bon user » se ferme quand les unités de
l'API et des jobs existent et déclarent leur compte :

```ini
[Service]
User=biowatch-api
Group=biowatch-api
EnvironmentFile=/etc/biowatch/api.env
WorkingDirectory=/opt/biowatch/app
# Le venv est construit par deploy : pas de « uv run », qui tenterait
# d'écrire dans /opt/biowatch.
ExecStart=/opt/biowatch/app/.venv/bin/python -m ...

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/biowatch/api /var/log/biowatch/api
```

Contrôle sur la machine : `systemctl show -p User <unité>` et
`ps -o user,cmd -C python`.

## Portée

Ces scripts traitent #22, #28 et #25. `AllowGroups biowatch-ssh` (posé par
`20-harden-ssh.sh`) interdit d'avance toute connexion SSH aux comptes de
service ; `40-service-users.sh` le vérifie et prévient s'il est absent.

Les ports 80 et 443 sont ouverts par anticipation pour #26. Sans processus à
l'écoute derrière, un port ouvert n'offre aucune surface d'attaque.
