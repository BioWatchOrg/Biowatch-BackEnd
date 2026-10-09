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

- La Batch Statistical API est réservée aux comptes **"Copernicus Service account"** — un type de
  compte à part, avec des critères d'éligibilité, pas accessible par défaut sur un compte
  gratuit/étudiant (confirmé sur la doc officielle, au-delà du simple statut beta évoqué
  initialement).
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

- Durée d'exécution initialement estimée à 1-2h par run mensuel pour une AOI comme `idf` avec
  `max_workers=5` — **revue à la baisse après test réel** (voir update 2026-10-06 ci-dessous) :
  le vrai débit autorisé par le compte CDSE gratuit est beaucoup plus restrictif que prévu.
- Cette approche ne scale pas indéfiniment : ajouter beaucoup d'AOI supplémentaires, monter en
  résolution H3 (9, 10...), ou passer à une cadence plus fréquente que mensuelle
  multiplierait le nombre de requêtes et pourrait devenir un vrai goulot d'étranglement.
- Le job reste dépendant du rate-limit synchrone de Sentinel Hub, géré par retry + backoff sur
  429 (`packages/clients/sentinel_hub/statistics.py`), pas par une stratégie de récupération des
  données fondamentalement différente.

**Update 2026-10-06 — le goulot d'étranglement s'est matérialisé en test réel**

Premier run complet sur `idf` en résolution 8 (18 307 cellules, `--max_workers 1`) : environ
24 requêtes passent en ~6s, puis un `429` avec un `Retry-After` de 133s. Extrapolé sur 18 307
cellules, ça donne de l'ordre de **24 à 28h** pour un run complet — bien au-delà de l'estimation
initiale de 1-2h. Ce n'est pas un bug côté client (le retry + backoff sur 429 fonctionne
correctement, voir `packages/clients/sentinel_hub/statistics.py`), c'est le vrai débit autorisé
par le compte CDSE gratuit.

Recherche faite sur les options disponibles :
- Les limites de débit sont **ajustables par compte sur demande au support Copernicus**, pas par
  changement de plan automatique (`documentation.dataspace.copernicus.eu/APIs/SentinelHub/Overview/RateLimiting.html`)
  — une demande a été envoyée au support en expliquant l'usage (projet étudiant, calcul mensuel
  sur l'Île-de-France).
- La Batch Statistical API résoudrait structurellement le problème (pas de rate-limit par
  minute, facturé en processing units, jusqu'à 700 000 géométries par envoi) mais reste
  verrouillée aux comptes "Copernicus Service account" — éligibilité à vérifier, pas acquise par
  défaut sur un compte gratuit/étudiant.
- Aucune autre API Sentinel Hub n'évite le problème (Process API a une famille de rate-limiting
  similaire, en pire puisqu'il faudrait en plus télécharger et traiter du raster).

En attendant une réponse du support, le job n'est pas bloqué — juste lent. C'est compatible avec
l'exécution en job de fond/nocturne déjà prévue par l'architecture (`CLAUDE.md`), et l'idempotence
par zone permet d'interrompre et reprendre un run sans perte ni doublon.

**Update 2026-10-09 — réponse du support Copernicus : refus, pistes restantes évaluées**

Le support Copernicus a répondu : **refus explicite** de la demande de quota. Citation : *"every
registered CDSE account is subject to the same quota limitations [...] we can't simply increase
it per nature of the project."* Les paliers de rate-limit sont fixes par compte, pas négociables
au cas par cas — ce n'est pas un refus ponctuel, c'est une politique. Cette piste est **fermée**.

Deux options alternatives ont été évaluées et écartées avant de se résoudre à accepter le run
long :

- **Changer d'API Sentinel Hub (Process API, Catalog API...) sur le même compte** : écarté.
  Confirmé par le support *et* par la doc technique (`docs.sentinel-hub.com/api/latest/api/overview/rate-limiting/`)
  que le rate-limit est attaché au **compte**, pas à l'API appelée — toutes les API d'un même
  compte partagent le même palier. Pire : la Statistical API déclenche en interne une
  sous-requête par date d'observation dans la période demandée, ce qui la rend **structurellement
  plus coûteuse en quota** que Process/Catalog pour un volume de travail équivalent. Changer d'API
  reviendrait à retomber sur le même mur, probablement plus vite, en plus de perdre le travail
  déjà stabilisé sur `packages/clients/sentinel_hub/`.
- **Une seule requête globale sur toute l'AOI au lieu d'une requête par cellule H3** : écarté, et
  pas qu'une question de performance — c'est une rupture fonctionnelle. Le coût en PU dépend de
  la surface totale et de la résolution, pas du nombre de requêtes : regrouper ne réduit pas le
  volume total de PU, ça le concentre en un seul appel (risque de dépassement de PU/minute d'un
  coup, ou de timeout). Plus fondamentalement, la Statistical API synchrone ne renvoie qu'**un
  seul résultat agrégé par requête, quelle que soit la taille de la géométrie envoyée** — une
  requête sur toute l'AOI donnerait un NDVI unique pour toute l'Île-de-France, pas une valeur par
  zone. Ventiler les stats par sous-géométrie en un seul envoi est exactement ce que fait la Batch
  Statistical API (verrouillée, voir plus haut) ; l'API synchrone ne le permet pas.

**Décision retenue** : accepter la durée de run actuelle (~24-28h pour `idf` en résolution 8) en
attendant l'issue des deux candidatures en cours (Credits for Value Creators, Copernicus Services
user — voir ci-dessous). Pas de changement d'architecture ni de dégradation de la résolution H3
pour contourner le problème dans l'immédiat.

**Update 2026-10-09 (suite) — "Copernicus Services user" confirmé fermé, CREODIAS retenu**

Vérification plus poussée de l'éligibilité "Copernicus Services user" (FAQ officielle
`documentation.dataspace.copernicus.eu/FAQ.html` + guide quotas) : réservé aux *"institutions and
organisations developing or operating Copernicus Services"* et aux *"European institutions and
Bodies set up under the EU Treaties"* — le dossier demande même une référence de contrat
Copernicus Services et un contact côté Commission Européenne. **Pas un statut ouvert à la
recherche académique en général** (correction par rapport à la première lecture du 2026-10-06,
qui s'appuyait sur une liste de secteurs marketing trop large) — un projet étudiant comme BioWatch
n'y est pas éligible. Cette piste est **fermée**, confirmé par deux sources officielles distinctes.

La doc officielle sur les quotas (`documentation.dataspace.copernicus.eu` — guide "Quotas and
Limitations") confirme par ailleurs que pour un dépassement de quota **Sentinel Hub** spécifiquement,
les seules options listées sont : attendre le renouvellement mensuel (ne s'applique pas à notre
cas : rate-limit par minute, pas quota mensuel épuisé), réduire l'usage via les apps Sentinel Hub
en navigation déconnectée (ne s'applique pas à un usage API direct), **Credits for Value
Creators**, ou accès commercial direct via CREODIAS. Aucun autre levier gratuit n'existe dans
l'écosystème Copernicus pour ce type de blocage — confirmé par la documentation officielle elle-même,
pas seulement par la suggestion du support.

**Décision** : candidature déposée sur `creodias.eu/pricing/credits-for-value-creators/`
(Sentinel Hub sélectionné), malgré la dépendance à une plateforme commerciale que cela introduit.
Accepté comme compromis pragmatique : c'est la seule option gratuite reconnue par Copernicus
lui-même pour ce problème précis — l'alternative stricte (aucun levier, `CLAUDE.md` à la lettre)
laisserait le job à 24-28h indéfiniment sans aucune perspective d'amélioration.
**Hypothèse à vérifier** dans la réponse du comité (sous 30 jours) : rien ne garantit que ces
crédits lèvent spécifiquement le rate-limit par minute plutôt que juste le quota mensuel de PU.

**Alternative identifiée mais non retenue pour l'instant : Google Earth Engine**

Seule alternative extérieure à l'écosystème Copernicus qui aurait un sens technique :
`reduceRegions()` de GEE calcule des stats pour des milliers de polygones en un seul appel
serveur — équivalent fonctionnel à ce que la Batch Statistical API offrirait. Accès gratuit
généreux pour la recherche non-commerciale. Non retenue dans l'immédiat : nouvelle auth (compte
de service Google), nouveau modèle de requête à apprendre, clause non-commerciale des conditions
d'utilisation à vérifier sérieusement avant tout usage au-delà d'un POC (BioWatch vise
potentiellement des collectivités publiques), et perte du travail déjà stabilisé sur le client
Sentinel Hub. À reconsidérer seulement si les deux pistes ci-dessus échouent également.

**Update 2026-10-09 (suite) — passage à une cadence annuelle**

Décision complémentaire, indépendante de l'issue CREODIAS : la cadence d'extraction passe de
**mensuelle à annuelle**, sur une fenêtre temporelle courte et fixe répétée chaque année (pas une
moyenne lissée sur 12 mois). Deux justifications :

- **Cohérence avec le reste du pipeline** : toutes les autres sources de données sont déjà
  annuelles (`update_gbif_occurrences` : 1x/an, `update_iucn_taxonomy` : annuel max,
  `compute_species_features_by_zone` : `period_year` annuel, import OSM : `bucket_id` annuel).
  Le satellite était la seule source pensée en mensuel — ce changement aligne enfin tout le
  pipeline sur une cadence commune, plutôt que de maintenir une incohérence.
- **Compatible avec le rate-limit actuel sans attendre de quota supplémentaire** : à ~635
  requêtes/heure, un run national (~840 000 cellules pour la France métropolitaine) prend ~55
  jours — ça ne rentre pas dans un mois, mais rentre très confortablement dans une année. Le
  choix d'une fenêtre **courte** plutôt qu'une agrégation `aggregationInterval=P1Y` sur l'année
  entière est délibéré : la Statistical API déclenche une sous-requête interne par date
  d'observation dans la période demandée (voir update 2026-10-09 ci-dessus) — une fenêtre
  courte reste donc moins coûteuse en quota qu'une agrégation annuelle complète, en plus de
  garder une vraie comparabilité saisonnière (même période chaque année) plutôt qu'une moyenne
  qui lisserait les écarts entre saisons.

Tickets Notion mis à jour en conséquence : `Job extract_sentinel_2` (bucket_id annuel, CLI
example, workflow) et `6. Industrialisation du calcul des scores` (DoD "tous les deux mois" →
"annuel", le texte supposant le satellite comme "source la plus fréquente" n'étant plus
applicable puisque toutes les sources sont désormais alignées).

**Fenêtre de référence tranchée (2026-10-09)** : **1er au 31 mai, chaque année** (mois complet,
pas une sous-période). Raisonnement : couvert nuageux moyen de 45% en Île-de-France en mai —
point médian correct, nettement mieux que novembre-janvier (58-60%) — et végétation déjà active
sans stress hydrique d'été. Le mois complet est préféré à une fenêtre plus courte pour maximiser
le nombre de scènes agrégées par cellule (signal plus robuste), au prix d'un coût en quota
légèrement supérieur à une sous-période.

Implémentation : `bucket_id` reste au format annuel (`"YYYY"`, cohérent avec les autres sources),
mais le `time_range` envoyé à la Statistical API est construit directement sur mai de cette
année-là (`{year}-05-01T00:00:00Z` → `{year}-06-01T00:00:00Z`, borne de fin exclusive — même
piège que le bug du 2026-10-04 sur `aggregationInterval=P1M`) plutôt que via `get_bucket_range`
sur le bucket annuel complet (qui donnerait tout le calendrier, pas seulement mai).

**Impact sur le backfill historique** : ce changement de cadence ne réduit pas le volume total
du backfill (N années × ~840 000 cellules reste le même calcul, qu'on interroge une fenêtre
courte ou longue par année) — seul le run **récurrent** devient praticable sans quota
supplémentaire. Le backfill reste l'usage principal justifiant la demande CREODIAS en cours.

**Remise en cause**

Ce seuil est désormais **atteint en pratique**, pas seulement hypothétique (voir update
2026-10-06). La demande de quota direct est définitivement fermée (update 2026-10-09) — à
réévaluer uniquement si l'une des deux candidatures en cours aboutit. Si l'éligibilité à la
Batch Statistical API est obtenue, réévaluer complètement cette décision — le stockage S3
deviendrait alors un besoin documenté et justifié, contrairement à aujourd'hui.
