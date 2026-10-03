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

## ADR-003 — Extraction Sentinel-2 : Statistical API synchrone (par zone) plutôt que Batch

**Statut** : actif

**Contexte**

Le job `extract_sentinel_2` (#36) a besoin, pour chaque cellule H3 d'une AOI et chaque bucket
mensuel, des indices agrégés (NDVI, NDWI, NDBI, SWIR) et de métriques qualité (nuages, pixels
valides). Copernicus Data Space Ecosystem propose deux façons d'obtenir ces statistiques sans
télécharger d'imagerie brute :

- la **Statistical API** synchrone : une requête HTTP = une géométrie = une réponse JSON
  immédiate avec les stats agrégées ;
- la **Batch Statistical API** : on soumet plusieurs géométries en un seul envoi, le calcul est
  fait de façon asynchrone côté Copernicus, le résultat est déposé dans du stockage objet
  (S3-compatible) à récupérer ensuite.

Pour une AOI comme `idf` en résolution H3 8 (`packages/core/aoi/aoi_registry.json`), interroger
cellule par cellule représente environ 16 000 requêtes synchrones par run mensuel (Île-de-France
≈ 12 000 km², une cellule H3 res-8 ≈ 0,74 km²).

**Décision**

On utilise la Statistical API synchrone, une requête HTTP par cellule H3 par bucket
(`packages/clients/sentinel_hub/statistics.py`), malgré le volume de requêtes que ça représente.
On écarte explicitement deux alternatives : récupérer l'imagerie brute de toute l'AOI en une
fois et calculer les statistiques zonales nous-mêmes (Process API), et la Batch Statistical API.

**Pourquoi**

- La Batch Statistical API est en **beta** chez Copernicus — pas de garantie de stabilité du
  contrat, pas une base fiable pour un job de production.
- Elle livre son résultat de façon **asynchrone vers du stockage objet (S3)**, ce qui
  réintroduit une dépendance S3 que le projet a explicitement choisi de ne pas ajouter sans
  besoin documenté (`CLAUDE.md` : stockage raster local VPS par défaut, migration S3 uniquement
  sur besoin documenté — aucun n'existe ici).
- Elle ajoute de la complexité opérationnelle (soumettre un batch, attendre/poller la fin du
  calcul, télécharger et parser un fichier de résultats) là où une requête synchrone par zone
  s'intègre naturellement dans le mécanisme d'idempotence et de reprise par zone déjà prévu
  (`job_run` + `job_run_zone_errors`).
- L'alternative "tout télécharger et trier nous-mêmes" (Process API + calcul raster maison)
  réintroduirait une stack de traitement raster lourde (`rasterio`/GDAL/`rasterstats`) que le
  choix initial de la Statistical API visait précisément à éviter (pipeline direct API →
  PostGIS, sans parquet ni raster intermédiaire), et transférerait beaucoup plus d'octets sur le
  réseau (pixels bruts vs. petit JSON de stats agrégées par requête).
- Le volume de requêtes (~16 000/run pour `idf` en résolution 8) est réel mais acceptable parce
  que c'est un **job batch mensuel** exécuté via systemd timer, sans contrainte de latence
  utilisateur — l'idempotence par zone rend une interruption ou une relance sans risque.

**Limites assumées**

- Durée d'exécution de l'ordre de 1 à 2h par run mensuel pour une AOI comme `idf` avec
  `max_workers=5` — acceptable pour un job de fond, pas pour un usage temps réel.
- Cette approche ne scale pas indéfiniment : ajouter beaucoup d'AOI supplémentaires, monter en
  résolution H3 (9, 10...), ou passer à une cadence plus fréquente que mensuelle
  multiplierait le nombre de requêtes et pourrait devenir un vrai goulot d'étranglement.
- Le job reste dépendant du rate-limit synchrone de Sentinel Hub, géré par retry + backoff sur
  429 (`packages/clients/sentinel_hub/statistics.py`), pas par une stratégie de récupération des
  données fondamentalement différente.

**Remise en cause**

À réévaluer si le volume de requêtes devient un vrai problème opérationnel (ajout d'AOI,
résolution H3 plus fine, cadence plus fréquente que mensuelle) — dans ce cas, réévaluer la Batch
Statistical API une fois sortie de beta (le stockage S3 deviendrait alors un besoin documenté et
justifié), plutôt que de construire un pipeline raster maison.
