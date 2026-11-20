# Clés publiques des membres

Une clé **publique** par membre, nommée `<login>.pub`, exactement comme le
login déclaré dans `MEMBERS` de `../config.sh`.

Commiter une clé publique est sans risque : elle est publique par nature.
C'est ce qui rend l'ajout d'un membre reviewable en PR plutôt qu'exécuté à
la main sur le serveur.

## Générer sa paire (sur son poste, jamais sur le VPS)

```bash
ssh-keygen -t ed25519 -a 100 -C "<login>@biowatch" -f ~/.ssh/biowatch_ed25519
```

- `ed25519` : plus court et plus solide que RSA.
- `-a 100` : durcit la dérivation de la passphrase contre le cassage hors ligne.
- **Passphrase obligatoire.** Les comptes ont `sudo` sans mot de passe : une
  clé privée nue dans un `~/.ssh` volé donne un root immédiat sur le VPS.

Puis transmettre **uniquement** `~/.ssh/biowatch_ed25519.pub`.

## Ajouter un membre

1. Déposer sa clé ici sous `<login>.pub`.
2. Ajouter le login dans `MEMBERS` de `../config.sh` et le retirer de
   `MEMBERS_PENDING`.
3. Ouvrir une PR (≥ 2 reviews).
4. Après merge, rejouer `../10-users-and-keys.sh` sur le VPS. Le script est
   idempotent : les comptes déjà en place ne sont pas touchés.

## Ne jamais déposer ici

Un fichier sans `.pub`, ou contenant `PRIVATE KEY`. `10-users-and-keys.sh`
refuse d'ailleurs de démarrer s'il détecte une clé privée — mais une clé
privée commitée est à considérer comme compromise et à révoquer, même
supprimée ensuite : elle reste dans l'historique git.

## Confort local

Dans `~/.ssh/config` sur son poste, pour ne plus taper port ni chemin :

```
Host biowatch
  HostName <IP_DU_VPS>
  Port 50022
  User <login>
  IdentityFile ~/.ssh/biowatch_ed25519
  IdentitiesOnly yes
```

Ensuite `ssh biowatch` suffit.
