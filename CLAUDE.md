# BioWatch backend — agent context

## Vision

BioWatch détecte automatiquement les zones où les activités humaines mettent en danger la biodiversité locale. La plateforme fusionne données satellites, données géospatiales, open data environnementales, données de biodiversité et indicateurs de pression humaine pour produire un **score synthétique de stress écologique par zone**, affiché dans une carte interactive.

Cibles : collectivités locales, communes, acteurs publics de l'aménagement, associations environnementales.

BioWatch ne cherche pas à produire plus de données — il **transforme des données complexes en information simple, exploitable, compréhensible** sous la forme d'un score.

## Tâches dans Notion

Toutes les tâches sont trackées dans Notion, pas dans le repo. Chaque page de tâche contient les détails d'implémentation, la **Definition of Ready (DoR)** et la **Definition of Done (DoD)**.

**Avant d'implémenter une tâche**, utiliser les tools MCP du serveur `biowatch-notion` (auto-chargés via `.mcp.json`, namespace `mcp__biowatch-notion__*`) :

1. `notion_get_subpages` — index récursif des tâches, retrouver l'ID par titre.
2. `notion_get_page_content` — corps de la page (détails, DoR, DoD).

Lire la DoR/DoD depuis la page. **Ne pas inventer de critères d'acceptation.**

### Setup par membre (une fois)

Le serveur lit `NOTION_TOKEN` depuis `.env` (gitignored). Chaque membre doit :

1. Demander un token au propriétaire du projet **ou** créer une intégration sur https://www.notion.so/my-integrations et faire partager la racine BioWatch avec.
2. Ajouter `NOTION_TOKEN=secret_...` dans son `.env` local.
3. Relancer Claude Code.

## Principes de conception

- Solutions simples, robustes, réalistes. Pas d'architecture inutilement complexe.
- Séparation claire : logique métier / infrastructure / API / frontend.
- Modularité et évolutivité — ajout de sources sans refonte majeure.
- Contraintes de coût en tête, pas de dépendance non justifiée.
- Compatible avec un projet étudiant ambitieux mais limité dans le temps.

Si une proposition est fragile, surdimensionnée, ou contredit l'architecture : **le dire clairement** et proposer une alternative. Ne pas être complaisant.

## Architecture

- **Backend** : Python + FastAPI (API de lecture) + jobs Python sur VPS. L'API expose uniquement des résultats déjà calculés — pas de logique métier lourde à la volée.
- **Frontend** : React + Mapbox. Calcul de bbox, appels API, affichage carto, interactions. Pas de logique métier côté front.
- **DB** : PostgreSQL + PostGIS. Stockage géospatial central, agrégations par zones **H3**.
- **Stockage rasters** : local VPS par défaut. Migration S3 uniquement sur besoin documenté.
- **Auth** : déléguée (Cognito ou Firebase Auth). L'API consomme une identité déjà validée, elle ne gère pas l'authentification elle-même.
- **Données client** : base séparée envisagée (DynamoDB ou Firebase).
- **Monitoring** : logs VPS via **Logdy**, KPI front via **Umami**.
- **Jobs** : exécution VPS, scheduling **systemd timers**, retry via job nocturne dédié.

**Règle forte** : toute infra plus complexe que le socle VPS actuel doit être justifiée par un besoin réel, immédiat, documenté.

## Infra VPS

- **VPS 1** (principal) : jobs (extraction, pipeline, scoring) • API • frontend servi via Nginx • reverse proxy • PostgreSQL + PostGIS • Umami • Logdy • scripts système de monitoring, sauvegarde, alertes.
- **VPS 2** (temporaire) : entraînement IA uniquement. Ne doit pas perturber VPS 1.

**Exposition réseau**
- Le frontend peut être exposé publiquement.
- L'API et la DB **ne sont jamais exposées directement** à Internet — reverse proxy, réseau privé ou tunnel SSH.
- Dev local : tunnel SSH pour accéder à l'API ou à la DB.

**Sécurité**
- SSH sans mot de passe, port non standard.
- Pas d'ouverture inutile vers l'extérieur.
- Secrets stockés hors dépôt.
- Surface d'attaque minimale.

## Stack & structure logicielle

- **Python 3.12**, **uv** comme gestionnaire de dépendances. Un seul `pyproject.toml` à la racine.
- Commandes : `uv sync`, `uv add <pkg>`, `uv run <cmd>` (ex. `uv run pytest`, `uv run ruff check`).

Layout (chaque dossier est un package Python installé en éditable) :

- `apps/api/` — FastAPI de lecture
- `apps/jobs/` — runners, CLI, pipelines
- `packages/core/` — types, config, modèles communs, génération des clés d'idempotence, gestion temporelle (buckets)
- `packages/clients/` — DB, storage, logs, secrets, services externes et sur-couches
- `packages/scoring/` — calcul des sous-indices et du score écologique
- `packages/ml/` — entraînement, datasets, inférence
- `packages/geo/` — H3, intersections, conversions GeoJSON / WKT / PostGIS

Quand tu proposes du code, **respecter cette séparation**. Pas de logique scoring dans `apps/api`, pas d'accès DB direct dans `packages/scoring`, etc.

### Frontend (rappel)

Séparer composants UI, pages, routes, services API, store Zustand, modèles TypeScript, hooks, composants cartographiques, utilitaires. Pas de mélange affichage / appels API / logique métier.

## Fondations techniques obligatoires

Avant tout job métier, le projet doit disposer d'une base commune stable qui standardise :

- indexation spatiale (grille H3 + AOI)
- gestion temporelle (système de bucket)
- exécution commune des jobs
- génération des clés d'idempotence
- séparation environnements dev / prod
- règles de qualité de code
- logger backend Python centralisé
- package géospatial partagé
- script d'init DB + versioning

**Règle forte** : aucun job métier ne contourne ces outils communs.

## Philosophie de la donnée

BioWatch ne collecte pas des données « parce qu'elles existent ». Chaque source doit avoir un rôle clair dans le score écologique. Toute source proposée doit satisfaire les trois critères suivants.

**1. Accessibilité.** Open data ou gratuite • simple à récupérer • maintenue par un organisme reconnu.

**2. Historique exploitable.** Permet une lecture temporelle pertinente : historique suffisamment long, ou continuité fiable sur plusieurs années.

**3. Pertinence écologique.** Réellement utile au score. Éviter les datasets trop spécifiques ou peu exploitables, éviter les couches redondantes. Privilégier les données actionnables pour comprendre la végétation, l'eau, l'urbanisation, les pressions humaines, la biodiversité sensible.

**Règle forte** : ne jamais proposer l'ajout d'une source de données « au cas où ». Si une source ne sert pas un signal identifié du score, elle n'entre pas.

## Modèle de données & vocabulaire métier

Base géospatiale structurée autour d'un index spatial central : **H3**. Chaque zone est identifiée par `zone_id` (une cellule H3).

### Tables

| Table | Rôle | Champs clés |
| --- | --- | --- |
| `zones_hex` | Référence géo (source de vérité spatiale) | id H3, résolution, géométrie, centroïde, bbox |
| `satellite_features_by_zone` | Features satellites par zone × période | NDVI, NDWI, NDBI, SWIR, métriques qualité |
| `osm_features_by_zone` | Pression humaine | ratio surface bâtie, densité routière, occupation urbaine |
| `protected_areas_by_zone` | Aires protégées applicables à une zone | type de protection, validité temporelle, taux de couverture |
| `species_features_by_zone` | Agrégats biodiversité par zone × période | nb total d'espèces, version source |
| `stress_score_by_zone` | Résultat métier principal | score global, sous-indices, méthode de calcul, run associé, date |
| `job_runs` | Traçabilité d'exécution | état, idempotence, erreurs de run |
| `job_run_zone_errors` | Erreurs partielles par zone | zones échouées, reprise ciblée possible |

### Principes

- H3 est la clé spatiale centrale.
- Chaque source possède **sa propre** table de features.
- Le temps est normalisé via `bucket_id` ou `period`.
- Les scores vivent dans une table dédiée (`stress_score_by_zone`).
- Chaque exécution de job est traçable (`job_runs` + `job_run_zone_errors`).

**Règle forte** : utiliser les noms **réels** de ces tables dans le code (backend, SQL, repositories, services) et les explications d'architecture. Ne pas inventer d'autres entités métier si les tables existantes couvrent déjà le besoin.

## Pipelines de données

Les pipelines récupèrent, nettoient, transforment et agrègent les données externes avant toute exposition API. Objectif : transformer des sources hétérogènes en features homogènes exploitables par le scoring, l'API, et les futurs modèles IA.

**Chaque source suit le même cycle** :
1. Récupération depuis une source externe fiable.
2. Prétraitement.
3. Agrégation par `zone_id`.
4. Rattachement à une période normalisée (`bucket_id` ou `period`).
5. Stockage dans une table **dédiée** à la source.

**Principe de stockage** : séparer les données par source métier (satellite, OSM, aires protégées, espèces). **Ne pas** proposer de table monolithique qui mélange toutes les features.

**Versionnement** : les pipelines intègrent la version de source et la version de calcul, pour garantir reproductibilité, idempotence et comparaison dans le temps.

## Score écologique

Cœur du projet : calcul d'un **score de stress écologique par zone**, persisté dans `stress_score_by_zone` (score global, sous-indices, méthode, run associé, date).

**Sous-indices possibles** (selon la phase) : pression humaine, état de la végétation / dégradation, biodiversité sensible, dynamique temporelle, protection environnementale.

**Problème identifié** : un score basé sur des valeurs absolues produit des résultats trompeurs (ex. une métropole dense apparaît artificiellement « mauvaise »).

**Principe de contextualisation** : une zone doit être évaluée par rapport à son **contexte écologique attendu** (métropole dense / zone agricole / zone forestière / …). La logique de scoring doit tendre vers :
- une classification du type de territoire,
- des références adaptées par catégorie,
- une normalisation relative,
- un score final contextualisé.

**Exigences sur le calcul** : traçable • testable • explicable • compatible avec données manquantes • modulaire (évoluer sans casser le pipeline).

## Idempotence des jobs

Tout job doit être relançable sans produire de doublons.

- Chaque job génère une `idempotency_key` **déterministe** = fonction de (job, scope, période, version des données, éventuellement environnement). Même job + même scope + même période ⇒ même clé.
- `job_runs.status = success` → **skip**.
- `job_runs.status ∈ {failed, partial}` → **retry** autorisé.
- Écritures via **UPSERT** ou transaction batch. Les tables métier doivent avoir les contraintes d'unicité adaptées (ex. `UNIQUE(zone_id, bucket_id, source_version)`).
- Erreurs partielles par zone → `job_run_zone_errors`, reprise ciblée possible.

Toute proposition liée aux jobs doit respecter cette logique.

## Exigences de qualité

**Code** : lisible, structuré, typé sur la logique métier. Pas de bricolage. Pas de mélange logique métier / infrastructure.

**Tests** : obligatoires sur la logique métier critique. Coverage maintenu ou amélioré. Nouvelles features testables.

**Logs** : JSON structurés. Pas de `print()` ni `console.log` en production. Champs attendus selon contexte :
- `timestamp`, `service`, `level`, `event`
- `request_id` ou `run_id`
- `zone_id`, `idempotency_key`, `duration_ms` si pertinent

**Secrets** : aucun secret dans le dépôt. `.env.example` à jour. Configuration via variables d'environnement.

**Documentation** : toute feature met à jour la doc existante ou en ajoute une (description courte, architecture, exemple/workflow, requirements si utile).

## Rôle de l'API

Couche d'**exposition** des données déjà calculées. Ne pas exécuter de traitements lourds ni recalculer à la demande.

**À faire** : lire les données géospatiales, filtrer (bbox, date, résolution, paramètres), transformer en GeoJSON / formats adaptés, exposer infos utilisateur et état des jobs.

**Interdit** : recalculer features satellites, lancer le scoring à la volée, dupliquer la logique métier des jobs, contourner `packages/*`.

**Organisation** : par domaine, séparation `router` / `service` / `schémas` / accès externes via `packages/clients`. Routes protégées via le provider d'auth, JWT validé côté backend.

## Rôle du frontend

Rendre l'analyse écologique lisible et exploitable : auth, carte, exploration des couches, sélection de zones, navigation temporelle, visualisations, vues détaillées. Animations / transitions / adaptation mobile en bonus.

**Peut** : calculer une bbox, gérer l'état local, demander des données, transformer légèrement pour l'affichage.

**Ne doit pas** : recalculer le score, rejouer un pipeline, réimplémenter la logique métier.

Animations, 3D, visualisations avancées ne doivent **jamais** dégrader lisibilité, performances, maintenabilité ou accessibilité.

## Place de l'IA

L'IA est une couche d'**amélioration progressive**, pas le cœur initial. Ordre de priorité :

1. Pipelines fiables
2. Features propres
3. Scoring métier robuste
4. API et visualisation
5. Modèles IA supplémentaires

**Progression prévue** : classification supervisée → détection d'anomalies → active learning → segmentation pixel à pixel (plus tard).

**Hypothèses interdites** dans toute proposition ML : infra cloud lourde déjà en place, temps illimité, datasets parfaitement annotés dès le départ, mise en prod temps réel obligatoire.

L'entraînement tourne temporairement sur VPS 2 ou Colab. Les propositions doivent rester cohérentes avec l'archi VPS — pas d'extrapolation AWS / services managés si ça contredit le socle retenu.

## Organisation & workflow git

Organisation horizontale : responsabilités partagées, montée en compétence collective, décisions collégiales, relecture croisée, documentation obligatoire pour la reprise. Éviter les solutions qui dépendent d'un expert unique ou les architectures incompréhensibles pour une équipe étudiante.

**Workflow**
- Sprint de 2 semaines. Réunion sprint chaque lundi (QA avant la réunion). Travail async le reste du temps.
- Une branche par tâche. Merge uniquement via PR, **≥ 2 reviews**. PR vers `dev`, puis `main` après QA.
- Commits au format `<type>: description`. Types autorisés : `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `style`, `ci`, `build`.

## Comment raisonner avant de proposer

Avant tout code ou architecture, identifier explicitement :
- phase du projet,
- module concerné (`apps/*` ou `packages/*`),
- entrées / sorties attendues,
- dépendances techniques existantes,
- impacts sur l'architecture.

Toute solution doit être cohérente **simultanément** avec : archi VPS retenue • tables de données réelles • packages communs • séparation jobs / scoring / API / frontend • phase actuelle • capacité d'implémentation de l'équipe.

Si une demande est floue : raisonner dans l'archi BioWatch existante, ne pas inventer une nouvelle organisation. Si une solution est théoriquement bonne mais trop complexe, coûteuse, fragile ou éloignée du cadre : la refuser et proposer une alternative réaliste.

**Style de réponse attendu** : direct, clair, exigeant techniquement. Dire explicitement quand une idée est mauvaise ou incohérente. Pas de complexité gratuite, pas de réponse SaaS générique, pas de logique métier déplacée vers le front. Quand pertinent, structurer en : objectif • proposition • avantages • limites • recommandation.
