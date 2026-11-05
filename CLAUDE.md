# Installation de l'assistant (consignes pour Claude Code)

Tu installes l'assistant de la personne qui t'a donné ce dépôt. Elle est peu technique : phrases courtes, une seule
question à la fois, la valeur par défaut toujours donnée entre crochets. Réponds dans sa langue (français par défaut).

## Ordre de travail
1. Cloner ce dépôt dans `~/assistant-kit` (s'il n'y est pas déjà ; sinon `git -C ~/assistant-kit pull --ff-only`).
2. Faire le questionnaire : `setup/questions.md`. Il produit `~/.config/assistant-kit/reponses.json` et `choix.json`.
3. Suivre les étapes de `setup/etapes.md` **dans l'ordre**. Après chaque étape, lancer sa commande de contrôle.
   Contrôle KO = s'arrêter, expliquer en une phrase, corriger ou donner la commande à taper. Jamais d'étape suivante sur un KO.
4. Finir par `bash ~/assistant-kit/setup/verifier.sh` : tout doit être OK (ou SAUTÉ avec sa raison).

## Règles
- **Jamais `sudo`**, jamais d'installation système par toi. Il manque un outil : donne la commande exacte à taper,
  précédée de `!` (`! sudo apt install tmux` sur Linux, `! brew install tmux` sur Mac), puis attends qu'elle soit faite.
- **Aucune question sur un secret** (mot de passe, clé, jeton). Tu n'en lis, n'en affiches et n'en copies aucun.
- **Ce qui se détecte se détecte** (`uname`, `whoami`, `$HOME`, nom de la machine, fuseau) : tu demandes seulement de confirmer.
- **Toutes les étapes sont rejouables** : une installation coupée se relance depuis le début sans dégât. Un fichier
  déjà présent chez lui n'est jamais écrasé.
- **`~/.claude/settings.json` n'est jamais écrasé** : la fusion passe par `setup/hooks.py`, qui garde tout le reste et
  sauvegarde l'original une fois dans `settings.json.avant-ultron`. Jamais d'édition à la main de ce fichier.
- Les gabarits sont remplis par `setup/remplir.py`, jamais à la main.
- Tu ne lances aucune commande qui n'est pas écrite dans `setup/etapes.md`. Un cas imprévu : tu le décris et tu demandes.
- Mac : services par launchd (`~/Library/LaunchAgents/`), posés par les étapes 7 et 8. Premier essai sur un vrai Mac :
  un contrôle KO, tu le décris (commande et sortie) et tu demandes, sans contourner.

## Le noyau et sa partie à lui
Le kit sépare deux choses. Une mise à jour remplace le **noyau** et ne touche **jamais** à **sa partie**.

| Noyau (remplacé par les mises à jour) | Sa partie (jamais touchée par une mise à jour) |
|---|---|
| `~/assistant-kit/` entier : `ultron-hub/`, `gabarits/`, `setup/`, ce fichier | `~/.config/assistant-kit/` : ses réponses (`reponses.json`, `choix.json`) |
| le code installé dans `~/workspace/projects/active/ultron-hub/` (sauf la colonne de droite) | `ultron-hub/var/` (todos, journaux), `ultron-hub/config/settings.yaml`, `ultron-hub/config/dashboard.yaml` |
| | `~/notes/` : ses chantiers, ses consignes, ses Reprises |
| | `~/ultron/esprit/` (profil, charte, journal) et `~/ultron/savoir/` |
| | `~/.claude/CLAUDE.md`, `~/.claude/charte.md`, `~/.claude/socle.md`, ses hooks dans `~/.claude/settings.json` |

Le nom de son assistant, son prénom et sa langue vivent dans sa partie (ses réponses et les fichiers remplis), jamais dans le noyau.
