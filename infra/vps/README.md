# Configuration VPS — durcissement SSH et firewall

Scripts couvrant les issues **#22** (accès SSH sécurisés) et **#28** (firewall UFW).

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

Chaque script accepte `--dry-run`. **Toujours l'utiliser en première passe.**

Entre `10` et `20`, ouvrir un second terminal et vérifier la connexion par clé
sur le nouveau port. `20` refusera de s'exécuter sinon.

## Étape zéro

Avant tout, chaque membre génère sa paire de clés et fournit sa clé publique :
voir [`keys/README.md`](keys/README.md). Sans au moins une clé déposée,
`10-users-and-keys.sh` s'arrête avant d'avoir touché quoi que ce soit.

## Déroulé

```bash
# sur le VPS, dans infra/vps/
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

Le filet n'est pas armé pendant `10-users-and-keys.sh` : ce script ne retire
aucun accès, il ne peut pas verrouiller.

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

## Portée

Ces scripts ne traitent **que** #22 et #28. Les comptes de service de #25
(`biowatch-api`, `biowatch-jobs`, `deploy`) ne sont pas créés ici — mais la
directive `AllowGroups biowatch-ssh` garantit d'avance qu'ils n'auront aucun
accès SSH, ce qui satisfait par construction l'un des critères de #25.

Les ports 80 et 443 sont ouverts par anticipation pour #26. Sans processus à
l'écoute derrière, un port ouvert n'offre aucune surface d'attaque.
