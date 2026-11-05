# Questionnaire

Une question à la fois. Valeur par défaut entre crochets : Entrée = défaut. Ce qui est **détecté** n'est jamais demandé :
on montre la valeur trouvée et on demande seulement « c'est bon ? ». Aucune question sur un secret.

Les réponses vont dans `~/.config/assistant-kit/` (sa partie, jamais touchée par une mise à jour) :
- `reponses.json` : les marqueurs des gabarits, lus par `remplir.py`. Seulement des chaînes non vides.
- `choix.json` : le reste (chantiers, Tailscale, appareils, style). Lu par les étapes, jamais par `remplir.py`.

Déjà rempli (installation relancée) : relire les deux fichiers, montrer les valeurs, demander « on garde ? ». Oui = on saute.

## Les questions

| # | Question | Défaut | Marqueur | Comment |
|---|---|---|---|---|
| 1 | Ton prénom ? | aucun (obligatoire) | `{{PRENOM}}` | demandé |
| 2 | Nom de ton assistant ? | Ultron | `{{NOM_ASSISTANT}}` | demandé |
| 3 | Ta machine | détecté | (aucun, `choix.json` : `systeme`) | `uname -s` : `Linux` ou `Darwin` (= Mac). Confirmé seulement. Windows : non pris en charge en v1, on s'arrête. |
| 4a | Ton nom d'utilisateur | détecté | `{{UTILISATEUR}}` | `whoami`. Confirmé seulement. |
| 4b | Ton dossier de base | détecté | `{{HOME}}` | `echo "$HOME"`. Confirmé seulement. |
| 4c | Nom de ta machine | détecté | `{{MACHINE}}` | Linux : `hostname -s`. Mac : `scutil --get LocalHostName`. Confirmé seulement. |
| 5 | Où ranger tes projets ? | `~/workspace/projects/active` | (aucun) | fixé en v1 (les unités et la commande `ultron` l'attendent là) : on le dit, on ne le demande pas. |
| 6 | Tes premiers chantiers : 1 à 3 noms courts, une phrase de but chacun ? | un chantier `exemple` | (aucun, `choix.json` : `chantiers`) | nom : a-z, 0-9 et tirets, 40 caractères au plus. |
| 7 | Tailscale est-il installé sur ce PC et sur ton téléphone ? | non | (aucun, `choix.json` : `tailscale`) | `command -v tailscale` d'abord (Mac : l'app fournit la commande). Non = étape manuelle M1, le dashboard attendra. |
| 8 | Quels appareils peuvent ouvrir le dashboard ? | aucun | (aucun, `choix.json` : `appareils`) | seulement si Q7 = oui : lister `tailscale status`, il coche son téléphone. On garde le nom et l'ID (`tailscale status --json`, champ `ID`). |
| 9a | Ta langue ? | français | `{{LANGUE}}` | demandé |
| 9b | Réponses courtes ou détaillées ? Tu ou vous ? | court, tu | (aucun, `choix.json` : `style`) | devient une proposition de profil après l'installation (étape 9). |
| 9c | Ton fuseau horaire | détecté | `{{FUSEAU}}` | Linux : `timedatectl show -p Timezone --value`. Mac : `readlink /etc/localtime` (garder la fin, ex. `Europe/Paris`). Défaut si rien : `Europe/Paris`. Confirmé seulement. |

Pas posées en v1 :
- Seuil de quota de la semaine : aucun réglage ne le lit encore.
- Drive pour les sauvegardes, mail de l'assistant : v2.

## Format de `reponses.json`

Un objet JSON, une clé par marqueur (le nom sans les accolades), valeur = chaîne non vide :

```json
{
  "PRENOM": "Exemple",
  "NOM_ASSISTANT": "Ultron",
  "UTILISATEUR": "exemple",
  "HOME": "/home/exemple",
  "MACHINE": "mon-pc",
  "LANGUE": "français",
  "FUSEAU": "Europe/Paris"
}
```

`remplir.py` refuse (code 1, rien n'est écrit) s'il trouve un marqueur sans réponse ou une réponse vide.
Sur Mac, `HOME` ressemble à `/Users/<nom>`.

## Format de `choix.json`

```json
{
  "systeme": "Linux",
  "chantiers": [{"nom": "exemple", "titre": "Chantier exemple", "but": "Découvrir l'assistant."}],
  "tailscale": false,
  "appareils": [],
  "style": "court, tu"
}
```

`appareils` : une liste de `{"nom": "...", "id": "..."}` (ID de nœud Tailscale), vide tant que Tailscale n'est pas prêt.
