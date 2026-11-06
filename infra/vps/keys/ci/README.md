# Clé de déploiement de la CI (#27)

`biowatch-ci.pub` est la clé **publique** du compte `biowatch-ci`, utilisé par
le workflow `Deploy jobs` pour déployer sur le VPS. Sa clé privée vit
**uniquement** dans les secrets GitHub (`VPS_DEPLOY_KEY`, environnement
`production`) : jamais dans le dépôt, jamais sur un poste après sa création.

Sur le VPS, la clé est installée par `../../50-jobs-deploy.sh` avec le préfixe
`restrict,command="/usr/local/sbin/biowatch-ci-deploy"` : elle ne peut ouvrir
ni shell, ni tunnel, ni transfert de fichiers. Elle ne sait faire que
`deploy <sha>`, `rollback [<sha>]` et `status`.

## Générer la paire (une fois, sur un poste de membre)

```bash
# Sans passphrase : la CI ne peut pas la saisir. C'est pour ça que la clé
# est restreinte à une commande forcée côté VPS.
ssh-keygen -t ed25519 -N "" -C "biowatch-ci@github-actions" -f ./biowatch-ci
```

1. `biowatch-ci.pub` → déposer ici, ouvrir une PR.
2. Contenu de `biowatch-ci` (privée) → secret GitHub `VPS_DEPLOY_KEY`.
3. **Supprimer la clé privée du poste** : `shred -u ./biowatch-ci`.

## Rotation (clé compromise ou membre sortant qui l'a manipulée)

Régénérer la paire, remplacer le `.pub` ici, mettre à jour le secret, puis
rejouer `50-jobs-deploy.sh` : l'ancienne clé est remplacée, donc révoquée.
