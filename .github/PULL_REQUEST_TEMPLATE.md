## 🔗 Linked issue

Closes #<issue-number>

## 📋 What does this PR do?

## 🧪 How to test it?

## ✅ Definition of Done

### 🤖 Review automatisée
- [ ] Label `run-review` appliqué sur le dernier commit (relancer si de nouveaux commits ont été poussés depuis)

### 🧠 Code & qualité
- [ ] Code lisible et structuré (nommage, structure)
- [ ] Aucun `print`, aucun warning critique
- [ ] Typage

### 🧪 Tests
- [ ] Tous les tests passent
- [ ] Coverage maintenu ou amélioré

### 🔁 Idempotence (jobs uniquement)
- [ ] Chaque job génère une `idempotency_key` déterministe
- [ ] `idempotency_key` UNIQUE dans `job_runs`
- [ ] Si un run existe déjà en `success` → le job skip
- [ ] Si `failed` ou `partial` → retry autorisé
- [ ] Toutes les écritures en base utilisent UPSERT
- [ ] Contraintes UNIQUE définies sur les tables métier
- [ ] Le job est relançable sans duplication ni effet de bord

### 🪵 Logs
- [ ] Logs produits au format JSON structuré
- [ ] Structure minimale respectée : `timestamp`, `service`, `level`, `event`, `context`
- [ ] `run_id` (job) ou `request_id` (API) présent dans le context
- [ ] Erreurs attendues en WARNING, exceptions en ERROR
- [ ] Aucune donnée sensible présente dans les logs
- [ ] En cas d'échec, le job passe au statut error avec des logs exploitables pour le diagnostic

### 🔐 Secrets & config
- [ ] Aucun secret dans le repo
- [ ] `.env.example` à jour

### 📚 Documentation
- [ ] Documentation créée ou mise à jour (Notion)
- [ ] Respect du template : description (2 phrases) · architecture · exemple/workflow · requirements (si utile)

