# Configuration VPS — SSH, firewall, comptes de service et Nginx

Scripts couvrant les issues **#22** (accès SSH sécurisés), **#28** (firewall UFW),
**#25** (comptes de service et isolation) et **#26** (Nginx).

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
| `50-nginx.sh` | #26 | Nginx HTTPS, socle + un fichier par service, placeholder du frontend. Boucle locale tant que non exposé | aucun pour l'accès : ne touche ni SSH ni UFW |

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

Le critère « fail2ban installé et actif » a été **retiré de la DoD de #22**.
Envisagé ensuite pour #26, il n'y figure pas non plus : il est réexaminé avec
#80.

Fail2ban répond au brute-force d'authentification. Cette menace n'existe pas
ici : l'accès est exclusivement par clé ed25519, `AllowGroups` rejette tout
compte hors du groupe, le port n'est plus le 22, et UFW n'expose que trois
ports. La jail `sshd` ne bannirait quasiment jamais rien.

Elle introduirait en revanche un risque réel : avec `MaxAuthTries 3`, un membre
dont le client propose plusieurs clés (absence de `IdentitiesOnly yes`) épuise
ses tentatives et se fait bannir alors qu'il est légitime.

**#26 ne change pas ce constat.** Nginx n'y sert qu'une page statique et des
503 : rien à deviner, rien à forcer. **Il deviendra caduc avec #80**, quand
l'API exposera derrière Nginx une surface authentifiable et scannable. fail2ban
y retrouve son utilité, avec des jails `nginx-*`, pas la jail `sshd`.

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

## Nginx (#26)

```bash
./50-nginx.sh --dry-run
./50-nginx.sh
```

Nginx est le **seul point d'entrée HTTP(S)** du VPS. Le frontend comme l'API
passent par lui ; aucun process applicatif n'écoute hors de `127.0.0.1`.
Le port 80 ne sert jamais de contenu : il redirige vers HTTPS et répond au
challenge Let's Encrypt.

**État actuel : rien d'exposé.** Domaine et certificat ne sont pas encore
tranchés. Nginx écoute donc **uniquement sur la boucle locale**
(`NGINX_LISTEN_V4/V6` dans `config.sh`), avec le certificat auto-signé
d'Ubuntu. UFW autorise 80 et 443, mais rien n'y écoute côté public : une
connexion depuis l'extérieur est refusée. La chaîne HTTPS complète se teste
par tunnel SSH :

```bash
ssh -N -L 8443:127.0.0.1:443 biowatch
curl -k https://localhost:8443/          # -k : certificat auto-signé
```

Exposer est ce qui reste pour fermer #26 : voir « Exposer » plus bas.

**Installation sans exposition.** Le paquet nginx démarre son site par
défaut sur `0.0.0.0:80` dès l'`apt install`, et UFW autorise 80 : il serait
joignable le temps que le script le remplace. `50-nginx.sh` pose donc un
`/usr/sbin/policy-rc.d` (`exit 101`, mécanisme Debian) pendant
l'installation, puis le retire, y compris en cas d'échec. Si le script est
tué brutalement (`kill -9`, coupure), vérifier que ce fichier a disparu : il
bloque le démarrage de **tout** service installé ou mis à jour par apt.

### Organisation

Les fichiers versionnés sont dans [`nginx/`](nginx/). `50-nginx.sh` les
installe en remplaçant les marqueurs `__XXX__` par les valeurs de `config.sh`.
**Ne jamais éditer la configuration directement sur le serveur** : la
prochaine exécution du script l'écraserait.

| Dépôt | Serveur | Rôle | Qui le modifie |
|---|---|---|---|
| `nginx/biowatch.conf` | `/etc/nginx/sites-available/biowatch.conf` | Socle : 80 → HTTPS, 443 (TLS, en-têtes de sécurité, `include` des services) | #26 seulement |
| `nginx/biowatch.d/frontend.conf` | `/etc/nginx/biowatch.d/frontend.conf` | Sert le frontend (SPA) | rarement : le déploiement ne le touche pas |
| `nginx/biowatch.d/api.conf` | `/etc/nginx/biowatch.d/api.conf` | `/v1/*` et `/health`, réservés (503) | #80 |
| `nginx/placeholder/index.html` | `/var/www/biowatch/frontend/index.html` | Prouve la chaîne avant tout déploiement | posé une fois, sur répertoire vide |

Le site `default` du paquet est désactivé : il se déclare lui aussi
`default_server` sur le port 80.

**Piège `add_header`.** Les en-têtes de sécurité du socle ne sont hérités
par une `location` que si elle ne déclare **aucun** `add_header`. Un seul
suffit à les effacer tous, sans erreur. Pour le cache, utiliser `expires`,
qui n'est pas un `add_header`.

### Brancher l'API (#80)

1. Le process API écoute **uniquement** en `127.0.0.1:<port>` (ou socket
   unix). `50-nginx.sh` échoue s'il trouve un port en écoute hors boucle
   locale autre que SSH, 80 et 443.
2. Remplacer le contenu de `nginx/biowatch.d/api.conf` dans le dépôt, puis
   rejouer `./50-nginx.sh` :

   ```nginx
   location /v1/ {
       proxy_pass http://127.0.0.1:<port>;
       proxy_set_header Host              $host;
       proxy_set_header X-Real-IP         $remote_addr;
       proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
   }

   location = /health {
       proxy_pass http://127.0.0.1:<port>;
   }
   ```

   Sans barre finale après le port, `proxy_pass` transmet le chemin complet
   (`/v1/zones` reste `/v1/zones`).
3. Les vérifications de `50-nginx.sh` attendent alors 200 au lieu de 503 sur
   `/health` : mettre à jour `verify()` dans le même PR.

### Déployer le frontend (Biowatch-Frontend#76)

Le déploiement **ne touche pas à Nginx** : il remplace le contenu de
`/var/www/biowatch/frontend/`, rien d'autre.

| Répertoire | Propriétaire | Mode | Effet |
|---|---|---|---|
| `/var/www/biowatch/frontend` | `deploy:www-data` | `2750` | `deploy` écrit, nginx (`www-data`) lit, personne d'autre |

Le bit setgid fait hériter du groupe `www-data` tout ce que `deploy` dépose :
nginx peut le lire sans `chgrp`. Les fichiers doivent rester lisibles par le
groupe (umask `022` par défaut, ou `rsync --chmod=D2750,F640`).

```bash
# depuis une session membre, build Vite dans ./dist
sudo -u deploy rsync -r --delete --chmod=D2750,F640 dist/ /var/www/biowatch/frontend/
```

Le frontend appelle l'API en chemin relatif (`/v1/...`), jamais en
`localhost:<port>` : Nginx reste le seul point d'entrée, y compris en
same-VPS.

> **À trancher dans #76.** `deploy` écrit aussi dans `/opt/biowatch` (code
> backend, #25). Une clé CI du frontend qui agirait comme `deploy` pourrait
> donc réécrire le backend. #76 demande un rôle limité au répertoire du
> frontend : créer un compte dédié et changer le propriétaire de
> `FRONTEND_DIR` dans `config.sh`.

### Exposer (reste à faire pour fermer #26)

**À trancher par l'équipe** : le domaine, et l'adresse email du compte
Let's Encrypt (avertissements d'expiration, de préférence une adresse
d'équipe).

Prérequis : un enregistrement DNS `A` (et `AAAA` si le VPS a de l'IPv6) qui
pointe vers le VPS.

Le challenge HTTP-01 de Let's Encrypt passe par le port 80 public : il faut
exposer **avant** d'avoir le vrai certificat. `50-nginx.sh` refuse
d'exposer avec le certificat auto-signé, d'où l'ordre suivant.

1. `config.sh` : `NGINX_SERVER_NAME` = le domaine.
2. Première émission, nginx arrêté :
   `certbot certonly --standalone -d <domaine>`. Certbot écoute lui-même
   sur le port 80 public le temps du challenge, puis le libère. Jamais le
   plugin `--nginx` : il réécrirait les fichiers gérés par le dépôt.
3. `config.sh` : `NGINX_TLS_CERT` / `NGINX_TLS_KEY` vers
   `/etc/letsencrypt/live/<domaine>/fullchain.pem` et `privkey.pem`, puis
   `NGINX_LISTEN_V4="0.0.0.0"` et `NGINX_LISTEN_V6="[::]"`. Rejouer
   `./50-nginx.sh`.
4. Dans le socle : `Strict-Transport-Security`, et un `default_server` 443
   en `ssl_reject_handshake on` pour ne pas servir le site sur l'IP nue.
5. Renouvellement : basculer le certificat en webroot, nginx étant
   désormais exposé sur 80 (`certbot certonly --webroot -w /var/www/letsencrypt
   -d <domaine> --deploy-hook "systemctl reload nginx"`). Le `certbot.timer`
   du paquet renouvelle ensuite seul. Tester avec `certbot renew --dry-run`.

Les fichiers de `biowatch.d/` n'ont rien à changer : c'est l'intérêt du
découpage.

## Portée

Ces scripts traitent #22, #28, #25 et #26 (hors exposition). `AllowGroups biowatch-ssh`
(posé par `20-harden-ssh.sh`) interdit d'avance toute connexion SSH aux
comptes de service ; `40-service-users.sh` le vérifie et prévient s'il est
absent.

Les ports 80 et 443 sont ouverts dans UFW par anticipation. Tant que nginx
n'écoute que sur la boucle locale, aucun processus n'y répond côté public :
un port ouvert sans rien derrière n'offre aucune surface d'attaque.
`50-nginx.sh` le vérifie à chaque exécution.
