# update_gbif_occurrences — design

## Contexte

Ticket : récupérer les occurrences GBIF pour une AOI sur une fenêtre glissante
de 10 ans, filtrer la qualité, convertir les points en `zone_id` H3, stocker
dans `staging.species_occurrences_raw`. Fréquence : 1×/an (année N → recharge
N-9 → N).

Source de vérité : page Notion « update_gbif_occurrences » (fournie par
l'utilisateur après une première itération basée uniquement sur l'issue
GitHub — v2 a corrigé le schéma/logique pour coller au Notion). Le MCP
`biowatch-notion` reste non connecté (`NOTION_TOKEN` placeholder) ; si le
Notion évolue après cette date, relire la page directement plutôt que ce
document.

Prérequis DoR déjà en place dans le repo : AOI registry + `load_aoi(aoi_id)`
(`packages/core/aoi.py`), grille H3 (`packages/geo/h3_grid.py`), idempotence +
`job_run` (`packages/clients/db/job_runs.py`), env local. **Écart connu avec
le DoR** : le Notion mentionne "CLI Click" — le repo utilise un runner CLI
maison (`argparse`), fonctionnellement équivalent. **Écart non résolu** : env
dev/prod VPS pas encore montés, seul l'env local existe — hors scope de ce
ticket.

## v3 — Pivot Search API → Download API

**v1/v2** de ce document choisissaient l'Occurrence Search API (paginée par
année). **Implémentée, testée contre la vraie API GBIF (Tasks 9-11 du plan),
et invalidée par les vrais volumes de données** : GBIF plafonne
`offset + limit` à 100 000 résultats par requête (confirmé dans la doc
officielle : *"This service has a maximum offset of 100,000 [...] To
retrieve all records beyond 100,000 you should use our asynchronous download
service instead."*). L'AOI `idf` a entre 200k et 650k occurrences par an sur
9 des 10 années de la fenêtre (≈4.86M au total) — la Search API ne peut
structurellement jamais finir un run pour la seule AOI existante.

**v3 bascule sur l'Occurrence Download API** (asynchrone, prédicat, pas de
plafond d'offset — c'est explicitement le chemin que GBIF recommande pour ce
volume).

## Décisions validées (v3)

- **API GBIF** : Occurrence Download API. `POST /occurrence/download/request`
  avec un **prédicat unique** combinant géométrie (hull) + plage de dates
  complète (10 ans en une fois, plus de boucle par année) → poll
  `GET /occurrence/download/{key}` jusqu'à `SUCCEEDED`/échec → téléchargement
  du fichier résultat.
- **Format** : `SIMPLE_CSV` (zip contenant un fichier `.csv` (tab-séparé),
  une ligne par occurrence — **confirmé contre l'API réelle (Task 17) : GBIF
  sert toujours un zip, y compris pour SIMPLE_CSV, jamais un flux gzip
  brut**). Pas `DWCA`
  (zip + XML Darwin Core Archive, parsing plus lourd pour aucun champ
  supplémentaire utile ici). Colonnes confirmées dans la doc GBIF :
  `gbifID`, `scientificName`, `taxonKey`, `decimalLatitude`,
  `decimalLongitude`, `eventDate`.
- **Authentification** : obligatoire pour créer un download — *"It is
  necessary to register as a user at GBIF.org [...] use HTTP authentication
  using the username (not the email) and password."* Nouvelles variables
  d'env : `GBIF_USERNAME`, `GBIF_PASSWORD` (jamais commitées, comme
  `NOTION_TOKEN`).
- **Filtre spatial GBIF** : prédicat `within` sur le **convex hull** de
  l'AOI (`aoi.geom.convex_hull.wkt`), pas le polygone exact. Repris de v2 :
  le polygone exact d'`idf` (466 sommets) dépassait déjà la limite de
  complexité WKT de la Search API ; le hull (22 sommets pour `idf`, +15.5%
  de surface) reste valide pour la même raison qu'avant — les occurrences
  hors de la forme réelle sont filtrées après coup par le test
  d'appartenance à `zones_hex` (étape 6 ci-dessous), donc la surface en trop
  du hull n'introduit aucun risque de correction, seulement un fetch un peu
  plus large. La doc de l'API Download annonce une limite de 10 000 points
  pour un prédicat `within` — 22 est très en dessous.
- **Résolution H3** : `aoi.default_res`, inchangé.
- **Filtre taxonomique** : aucun, inchangé.
- **Conflit PK** : `DO NOTHING`, inchangé — `upsert(..., update_columns=[])`.
- **Purge staging** : `DELETE` avant fetch, dans la transaction du job (voir
  v4 ci-dessous pour la condition exacte — elle a changé depuis v3). **Ajout
  suite à la revue finale** : un test dédié doit exister pour cette
  opération destructive (v2 ne l'avait pas — trouvé en revue finale, corrigé
  en v3).
- **Idempotence** : **ajout suite à la revue finale** — la clé doit inclure
  `aoi_version` et `resolution` (comme `generate_h3_grid` le fait déjà),
  pas seulement `aoi_label` + année. Sans ça, un changement de géométrie ou
  de résolution de l'AOI à l'intérieur d'une même année ne redéclenche pas
  le job.
- **Mémoire** : le flux SIMPLE_CSV est **streamé** (zip + csv, ligne par
  ligne) et upserté par lots de taille bornée — pas de liste Python de
  millions de lignes en mémoire (le problème ne se posait pas en v2 tant que
  le plafond à 100k limitait accidentellement le volume par requête ; il se
  serait aggravé une fois ce plafond contourné, donc traité maintenant).
- **Nouveau package** : inchangé — `packages/species/`, client HTTP dans
  `packages/clients/gbif/`.

## v4 — Historique conservé, remplacement par fenêtre refetchée

**Changement de décision produit, après v3** : `staging.species_occurrences_raw`
n'est plus une fenêtre glissante qui s'auto-purge — c'est une **archive
historique complète**, jamais tronquée par âge. Ce que chaque run purge
change : au lieu de supprimer ce qui est **sorti** de la fenêtre
(`event_date < date_min`), on supprime ce qui est **dans** la fenêtre qu'on
vient de refetcher (`date_min ≤ event_date ≤ date_max`), avant d'insérer le
fetch frais. Motivation : une occurrence GBIF peut être corrigée après coup
(coordonnées, taxonomie) ; sans ce remplacement, un refetch sur une année
déjà connue ne verrait jamais la correction (`ON CONFLICT DO NOTHING` garde
silencieusement l'ancienne donnée pour toujours).

Avec ce changement : `ON CONFLICT DO NOTHING` devient surtout un filet de
sécurité (plus de conflit normal possible dans la fenêtre qu'on vient de
vider) plutôt que le mécanisme principal anti-doublon — le vrai mécanisme
anti-doublon redevient le `DELETE` scopé par fenêtre + `aoi_id`.

**⚠️ Écart avec le Notion** : la page source demandait explicitement une
purge « hors fenêtre » (`staging` bornée à 10 ans glissants). Ce changement
l'inverse délibérément, sur demande explicite de l'utilisateur en session —
**à reporter dans le ticket Notion** pour que la DoD écrite reste alignée
avec le comportement réel.

## Architecture

```
apps/jobs/definitions.py
  └─ update_gbif_occurrences (CLI: --aoi, --debug)
        └─ packages/species/gbif_occurrences.py
              ├─ packages/clients/gbif/client.py   (Download API : request/poll/stream)
              ├─ packages/species/quality.py        (filtre qualité, pur — inchangé)
              └─ packages/geo/h3_grid.py            (point→cell — inchangé)
```

## Logique (v3)

1. **`job_runs`** : `job_run(...)` standard — inchangé.
2. **Fenêtre glissante** : `date_min = date(N-9, 1, 1)`, `date_max = date(N, 12, 31)` — inchangé.
3. **Remplacement staging pour la fenêtre refetchée** (v4 — inversé depuis
   v3) : `DELETE WHERE aoi_id = :aoi AND event_date BETWEEN :date_min AND
   :date_max`. L'historique hors fenêtre n'est jamais touché, conservé pour
   toujours.
4. **Un seul appel GBIF pour toute la fenêtre** :
   - `predicate = {"type": "and", "predicates": [
        {"type": "within", "geometry": "<hull WKT>"},
        {"type": "greaterThanOrEquals", "key": "EVENT_DATE", "value": "<date_min>"},
        {"type": "lessThanOrEquals", "key": "EVENT_DATE", "value": "<date_max>"},
        {"type": "equals", "key": "HAS_COORDINATE", "value": "true"},
        {"type": "equals", "key": "HAS_GEOSPATIAL_ISSUE", "value": "false"},
     ]}`
   - `POST /occurrence/download/request` (auth Basic, body `{"creator": <username>, "format": "SIMPLE_CSV", "predicate": predicate}`) → `download_key`.
   - Poll `GET /occurrence/download/{download_key}` jusqu'à `status == "SUCCEEDED"` (ou erreur sur `FAILED`/`CANCELLED`/`KILLED`), borné par un timeout (le job ne doit pas bloquer indéfiniment).
   - Téléchargement du fichier (redirection GBIF vers le fichier réel), streamé sur disque (pas en mémoire — potentiellement des centaines de Mo).
5. **Filtrage qualité** — inchangé, mais adapté aux noms de colonnes CSV
   (`gbifID` au lieu de `key`, sinon mêmes noms). Une fonction de
   normalisation convertit une ligne CSV en le même dict que `quality_filter`
   attendait déjà (pas de réécriture de `quality_filter`).
6. **Conversion point → H3 + filtre `zones_hex`** — inchangé.
7. **Insertion idempotente par lots bornés** (ex. 5000 lignes), pas un seul
   lot pour tout le fichier — `upsert(..., update_columns=[])`.
8. **Fin de run** — inchangé.

**Exactitude des noms de clés de prédicat** : la doc GBIF confirme les types
`equals`/`and`/`greaterThanOrEquals`/`lessThanOrEquals`/`within` et les clés
`HAS_COORDINATE`/`HAS_GEOSPATIAL_ISSUE`/`YEAR`/`COUNTRY`, mais la
documentation Swagger (rendue en JS, non scrapable automatiquement) n'a pas
confirmé à 100% l'orthographe exacte de la clé de date (`EVENT_DATE`
supposé par convention avec les autres clés en `UPPER_SNAKE_CASE`). **À
vérifier contre la vraie API** au moment de l'implémentation (même
discipline que pour le hull et le rate-limit en v2 : si GBIF rejette le
prédicat avec une erreur nommant la clé, corriger et re-vérifier avant de
considérer la tâche terminée).

## Schéma `staging.species_occurrences_raw`

Inchangé depuis v2 :

```sql
CREATE TABLE IF NOT EXISTS staging.species_occurrences_raw (
  gbif_occurrence_id   TEXT PRIMARY KEY,
  event_date           DATE NOT NULL,
  gbif_taxon_key       INT NULL,
  scientific_name      TEXT NOT NULL,
  decimal_latitude     DOUBLE PRECISION NOT NULL,
  decimal_longitude    DOUBLE PRECISION NOT NULL,
  zone_id              TEXT NOT NULL,   -- H3
  aoi_id               TEXT NOT NULL,
  ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`gbif_occurrence_id` = `gbifID` de la colonne SIMPLE_CSV (équivalent du `key`
JSON de la Search API — même identifiant GBIF global, juste exposé sous un
nom de colonne différent selon l'endpoint).

## Idempotence (v3)

```
idempotency_key = compute_idempotency_key(
    job_name="update_gbif_occurrences",
    scope=aoi_label,
    period=str(N),
    source_version=f"gbif-download-v1+aoi{aoi.version}",  # bump + AOI version folded in
    resolution=aoi.default_res,   # compute_idempotency_key already has this param (core/idempotency.py)
)
```

`compute_idempotency_key` already accepts a `resolution` param (used as-is by
`generate_h3_grid`) — no signature change needed. `aoi.version` doesn't have
its own param, so it's folded into `source_version` (same effect: any value
change forces a new key).

Un run par (AOI, année N, version AOI, résolution). Rejouer à l'identique →
skip. Un changement de géométrie AOI ou de résolution, même pour la même
année N, déclenche un nouveau run.

## Gestion d'erreur

Un seul appel réseau bloquant (au lieu d'une boucle par année) : un échec
(auth, predicate rejeté, download `FAILED`, timeout de poll) fait échouer
tout le run (`status=failed`), retry standard via `job_run`. Pas de
granularité par zone.

## Tests (objectif ≥ 90% coverage sur le nouveau code)

- **Client GBIF Download** (`packages/clients/gbif`) : mock `httpx` — requête
  de download (corps du prédicat correct), poll (transitions de statut,
  timeout), téléchargement + streaming du fichier (zip+csv), erreurs HTTP
  typées à chaque étape.
- **Filtre qualité** : inchangé (déjà fait, testé, 100% coverage) — juste
  vérifier que la normalisation CSV→dict alimente `quality_filter` sans le
  modifier.
- **Remplacement de fenêtre** (v4) : lignes **dans** la fenêtre refetchée
  supprimées, lignes **hors** fenêtre conservées pour toujours, scoping
  correct par `aoi_id`.
- **Job complet** : DB de test — idempotence (rerun identique = skip),
  rafraîchissement de données sur fenêtres qui se recoupent (2e run gagne,
  pas de doublon), exclusion des points hors `zones_hex`, et vérifie que
  changer `aoi.version`/`resolution` déclenche un nouveau run
  (nouveau test, finding de revue).

## Hors scope

- Agrégation par espèce / statut IUCN par zone → job séparé
  (`compute_species_features_by_zone`, #39).
- Job de retry dédié (#40) — ce job suit le contrat `job_run` standard. La
  gestion des pannes réseau transitoires (timeout de connexion, coupure TCP
  en cours de téléchargement) reste hors scope de ce ticket : un rerun est
  sûr (idempotent, purge+refetch, `DO NOTHING`), donc suffisant en attendant
  #40.
- Env dev/prod VPS — hors scope, suivi séparément (issues #18/#19).
- Mise à jour de `packages/clients/db/init_db.py`, du schéma DB, du client H3
  et du filtre qualité — déjà livrés et corrects en v2, non affectés par ce
  pivot (seul le mécanisme de récupération réseau change).
