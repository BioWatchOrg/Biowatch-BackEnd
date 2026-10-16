# Décisions d'architecture

Historique des choix d'architecture non triviaux, avec leur raisonnement. Objectif : éviter
de rouvrir un débat déjà tranché sans repartir des mêmes arguments. Une décision reste valable
tant que son contexte ne change pas — voir la section "Remise en cause" de chaque entrée.

---

## ADR-001 — Frontend → Firestore en direct pour les données utilisateur non sensibles

**Statut** : actif

**Contexte**

Une partie des besoins produit ne concerne pas la donnée géospatiale calculée (PostGIS) mais
des données propres à chaque utilisateur : préférences d'affichage, layout de dashboard, feed
d'activité, quotas d'abonnement. `apps/api` est dédiée à la lecture de PostGIS
(`packages/clients/db`) — la question était de savoir si ces données utilisateur devaient
transiter par `apps/api` (frontend → back → Firestore) ou si le frontend pouvait écrire/lire
Firestore directement via le SDK client (frontend → Firestore).

**Décision**

Le frontend accède à Firestore directement pour toute donnée dont la falsification par
l'utilisateur n'a aucune conséquence (préférences, layout). Le contrôle d'accès est fait par
`firestore.rules`, pas par du code FastAPI. `apps/api` ne sert jamais de proxy vers Firestore.

Les opérations qui nécessitent une garantie de confiance (attribution de rôle, décompte de
quota) ne passent ni par le frontend en direct, ni par `apps/api` : elles sont isolées dans des
Cloud Functions dédiées (Admin SDK, qui contourne `firestore.rules`). Voir la doc Notion
[Accès Firebase direct (frontend) & rôles via Firestore Rules](https://app.notion.com/p/3e450bea018d80d38d1bf719d5d2c0ac)
et la page WBS [🌐 cloud function (gcp)](https://app.notion.com/p/3e450bea018d80369f1fd889a2b3038c)
pour le détail des tickets.

**Pourquoi**

- `firestore.rules` fait déjà, de façon déclarative, exactement ce qu'un endpoint FastAPI
  ferait pour ce type de contrôle (vérifier l'identité/le rôle avant lecture/écriture). Ajouter
  `apps/api` entre les deux ne rajoute aucune sécurité, juste une deuxième source de vérité à
  maintenir en synchronisation avec les rules.
- Firestore apporte du temps réel natif (listeners `onSnapshot`) gratuitement — recoder cette
  synchronisation dans `apps/api` (polling ou SSE fait main) serait une réimplémentation pure
  perte de temps pour un projet à capacité d'équipe limitée.
- Cohérent avec la séparation déjà actée dans `CLAUDE.md` : PostGIS pour la donnée
  géospatiale calculée par les jobs, Firebase pour la donnée client.

**Limite de la règle**

`firestore.rules` ne sait exprimer que des contrôles déclaratifs (identité, rôle, égalité de
champ). Dès qu'une opération a un état ou un effet de bord non trivial — décrément atomique
d'un compteur, vérification de signature d'un webhook externe — elle sort du domaine des rules
et doit passer par une Cloud Function avec l'Admin SDK, jamais par une écriture client directe.

**Remise en cause**

À revoir si un jour une donnée utilisateur a besoin d'être croisée avec une donnée PostGIS dans
une même réponse (aujourd'hui, aucun cas identifié) — dans ce cas, une agrégation côté
`apps/api` redeviendrait justifiée pour cette donnée précise.

---

## ADR-002 — Auth et données utilisateur : Firebase managé plutôt qu'auto-hébergé

**Statut** : actif

**Contexte**

L'authentification (login, register, reset de mot de passe, refresh de token) et le stockage
des données propres à chaque utilisateur pouvaient soit être auto-hébergés (table `users` dans
le PostgreSQL existant, JWT fait maison), soit délégués à un service managé (Firebase
Auth + Firestore).

**Décision**

Auth et données custom utilisateur sont déléguées à Firebase. `apps/api` ne gère aucune
logique d'authentification — elle consomme une identité déjà validée par Firebase quand c'est
nécessaire (cf. `CLAUDE.md`, section Architecture : "Auth : déléguée").

**Pourquoi**

- Rolling your own auth (hash de mot de passe, gestion de session, reset de mot de passe,
  protection brute-force, refresh de token) est une des zones à plus haut risque de sécurité
  qui existent (OWASP Top 10). Une équipe étudiante à capacité limitée qui code ça elle-même
  prend un risque réel pour une fonctionnalité qui n'est pas le cœur du produit (le score
  écologique, pas l'auth).
- Coût quasi nul au stade POC/MVP (palier gratuit Firebase), contre un coût de développement
  et de maintenance certain en auto-hébergé.
- Cohérent avec le principe d'organisation horizontale du projet (pas de dépendance à un
  expert unique) : Firebase Auth est standard et documenté, un système maison devient vite la
  spécialité d'une seule personne de l'équipe.

**Limites assumées**

- **Vendor lock-in** : migrer hors de Firebase plus tard impliquerait une vraie migration de
  données utilisateur, pas un simple changement de configuration.
- **Résidence des données / RGPD** : à vérifier explicitement avant toute mise en production
  réelle — Firestore permet de choisir une région de stockage européenne, mais certaines
  métadonnées Firebase Auth sont stockées par défaut aux États-Unis. Point de conformité
  pertinent si BioWatch vise des collectivités publiques.
- Complexité de fait à gérer deux bases de données dans le projet (PostGIS + Firestore),
  chacune avec son propre modèle mental pour l'équipe.

**Remise en cause**

À réévaluer si le projet dépasse le stade POC/MVP pour viser une mise en production
commerciale avec des exigences de conformité RGPD strictes — pas avant.

---

## ADR-003 — Garde-fous dev / prod : marqueur `.env.prod` et suffixe `_<env>` du nom de base

**Statut** : actif

**Contexte**

Un seul VPS de prod et des postes de dev qui, pour inspecter la prod, ouvrent un tunnel SSH
vers sa base (issue #18). Le risque à couvrir est l'**erreur** : un job lancé depuis un laptop
qui atteint `biowatch_prod` parce qu'un `DATABASE_URL` traîne dans le shell, ou parce qu'on a
tapé `--env prod` sur la mauvaise machine. Le risque à ne pas couvrir ici est l'action
délibérée d'un membre de l'équipe : contre ça, seuls les droits réseau et les comptes
PostgreSQL comptent, pas du code Python.

**Décision**

1. **`dev` est le défaut**, résolu à un seul endroit (`core.config.resolve_env` :
   `--env` > `BIOWATCH_ENV` > `dev`).
2. **Prod exige la présence de `<racine du dépôt>/.env.prod`** (`ENV_FILE_REQUIRED`). Sur le
   VPS, c'est un **marqueur** posé par le déploiement à côté du code, sans secret : les secrets
   prod restent dans `/etc/biowatch/jobs.env`, lus par systemd, jamais dans un fichier lisible
   par le code. Sur un poste de dev, le fichier n'existe pas, donc `--env prod` s'arrête avant
   toute lecture de variable de connexion.
3. **Le nom de la base doit finir par `_<env>`** (`biowatch_dev` / `biowatch_prod`), vérifié par
   `clients.db` avant d'ouvrir la connexion, quelle que soit l'origine de la variable (fichier,
   shell, systemd). C'est le filet qui couvre le cas « dev par défaut + tunnel vers la prod ».
4. **`load_env_file(env)` est l'unique point d'activation d'un environnement** : il fige
   `BIOWATCH_ENV` puis charge `<racine>/.env.<env>` (jamais le cwd ni ses parents). C'est un
   effet de bord sur `os.environ`, assumé et localisé : tout entrypoint (CLI aujourd'hui, API
   demain) l'appelle au démarrage, et `clients.db` le rappelle en filet (idempotent) pour les
   usages en librairie. Partout ailleurs, l'env est lu via `resolve_env()`, jamais passé en
   paramètre : c'est un état de process, pas un argument métier.
5. **`env` entre dans la clé d'idempotence** : la ligne `job_runs` dit dans quel environnement
   le travail a été fait, et un `success` de dev ne vaut jamais pour prod (copie de prod en
   dev, restauration). Conséquence assumée au merge : les runs déjà en `success` se relancent
   une fois.

**Pourquoi**

- Deux filets indépendants plutôt qu'un seul : le marqueur bloque `--env prod` hors VPS, le
  suffixe bloque une base prod atteinte *sans* `--env prod`. Aucun des deux ne dépend de ce
  qui traîne dans le shell.
- Pas de mécanisme de confirmation (`--yes`, prompt) : une case à cocher s'automatise et se
  copie-colle ; l'absence d'un fichier et une convention de nommage, non.
- Les secrets prod ne descendent jamais dans un `.env.prod` lisible par `biowatch-jobs` :
  cohérent avec le modèle du VPS (`EnvironmentFile=` lu par systemd en root, cf.
  `infra/vps/README.md`).

**Limites assumées**

- Un `.env.prod` créé volontairement sur un laptop, ou une base prod renommée `*_dev`,
  contourne ces garde-fous : ils protègent contre l'erreur, pas contre l'intention. La
  protection contre l'intention est réseau (DB jamais exposée, tunnel nominatif) et
  PostgreSQL (comptes, droits).
- `load_env_file` mute `os.environ` : acceptable tant que chaque entrypoint est explicite sur
  son env. L'API devra appeler `load_env_file(resolve_env())` à son démarrage, et ne jamais
  pouvoir tomber sur `prod` par défaut.

**Remise en cause**

À revoir si un troisième environnement apparaît (préprod, staging), ou si la prod quitte le
modèle « un VPS, secrets via systemd » (conteneurs, secret manager) : le marqueur `.env.prod`
n'aurait alors plus de sens et devrait être remplacé par un signal propre à cette plateforme.

---

## ADR-004 — Référentiel IUCN : API v4 par pays plutôt qu'export complet sur S3

**Statut** : actif

**Contexte**

`update_iucn_taxonomy` (#34) alimente `staging.iucn_taxa` (statut de menace par espèce),
utilisé ensuite par `compute_species_features_by_zone`. Le ticket initial prévoyait
d'ingérer un fichier d'export versionné déposé sur S3
(`s3://.../raw/iucn/{source_version}/`).

**Décision**

Le job lit l'API IUCN Red List v4, route `/countries/FR?latest=true&scope_code=1`
(évaluations globales des espèces présentes en France, environ 5000 espèces, 51 pages,
environ 30 s). La réponse est gardée telle quelle en copie brute sur le disque du VPS
(`BIOWATCH_DATA_ROOT/<env>/raw/iucn/<version>/FR.json`). `source_version` est la version
de la Red List renvoyée par l'API (ex. `2026-1`).

**Pourquoi**

- L'API v4 n'expose aucune route d'export complet. L'export complet n'existe que via le
  site, manuellement, avec un compte utilisateur : impossible à automatiser proprement
  dans un job.
- Le périmètre MVP est la France : la route par pays ne ramène que les espèces utiles,
  sans filtrer 175 000 espèces mondiales.
- Stockage local sur le VPS par défaut (CLAUDE.md) : S3 n'est justifié par aucun besoin
  documenté. La copie brute suffit à la reproductibilité (recalcul sans rappeler l'API).
- La version de la Red List lue sur l'API donne une `source_version` fiable et une clé
  d'idempotence qui relance le job à chaque nouvelle édition.

**Limites assumées**

- Dépendance à un jeton API (`IUCN_API_TOKEN`, compte gratuit).
- La route par pays ne fournit pas la tendance de population (`trend`).
- France uniquement : `staging.iucn_taxa` n'a pas de colonne pays.

**Remise en cause**

À réévaluer si BioWatch couvre d'autres pays (ajouter une colonne pays, ou repasser par
un export mondial), si le score a besoin de `trend`, ou si l'IUCN publie une route
d'export complet.
