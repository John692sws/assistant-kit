# Chantiers : résumé, /chantiers, archiver

Référence détaillée, sortie de CLAUDE.md le 18/10/2026 (todo #167).

## Résumé des chantiers (todo #108)
- `ultron-hub chantier tableau | fiche <s> | atoi [--chantier <s>]` (`chantier/core.py`, `commands.py`) : lecture seule, JSON,
  best effort (toute source illisible = zéro + ligne dans `avertissements`, jamais une erreur). Chantier = sujet de prompt
  (`<prompts>/<s>.md`, archives `termines/`) ; `-` = « Hors chantier » (todos sans `chantier`).
- Sources : en-tête du prompt (titre, `Projet :`, « Session précédente », « Fiche du chantier »), dernière puce de la section
  `## REPRISE` de la fiche (5 lignes), todos ouvertes lues par une connexion SQLite `mode=ro` (aucune migration : sans la
  colonne de #103, tout est hors chantier), missions et branches (`todo/links.py`), boîte `~/.local/state/ultron-hub/boites/<s>/`
  (questions ouvertes sans réponse dans `reponses/`, remarques ; absente = 0).
- Les escouades autonomes ne sont plus lues (retrait #458) : pas de champ `escouade`, plus de refus d'archivage pour escouade en cours.
- 🧰 vient du fichier `var/after-merge/todo-<id>.json` (action `pending` ou `interrupted`), même si la todo est close par la fusion (#187).
- Groupes : 🟢 escouade en cours, 📥 quelque chose attend l'utilisateur (🔀, 🌿, 💬, ✅, 🧰, ❓), 💤 sinon.
- Essai réel du 16/10 (copie de la base de main, code du worktree) : 11 chantiers, escouade en cours vue 🟢, 0,05 s.
- Tests : `tests/test_chantier_resume.py` (sujets factices, prompt sans en-tête, fiche absente, git illisible).
- **Archiver** (todo #122) : `chantier archiver <s>` déplace `<prompts>/<s>.md` vers `termines/` ; `chantier rouvrir <s>` le remet.
  Seules commandes de `chantier` qui écrivent. Refus (rien déplacé) : nom hors `[a-z0-9][a-z0-9-]{0,39}`, escouade en cours sur le
  sujet (`squads_running`), todo ouverte du chantier (ou base illisible), cible déjà présente (jamais d'écrasement : lien dur puis
  suppression), lien symbolique (fichier ou `termines/`). Ligne au journal commun. `tableau` ne liste plus les archivés dans
  `chantiers` (clé `archives`) ; une todo rattachée à un archivé reste valide (`known_chantiers` lit `termines/`).
- Telegram (`chantier_flow.py`) : fiche -> `🗄 Archiver` (`ar:<s>`, absent si escouade en cours) -> écran de confirmation non signé
  (réversible) -> `🗄 Archiver` (`ay:<s>`) ; bas de /chantiers : « 🗄 Archivés (N) » avec `↩️ Rouvrir <s>` (`ro:<s>`, immédiat).
  Tests : `tests/test_chantier_archive.py`.

## /chantiers sur Telegram (todo #109, version minimale)
- `hermes_plugin/chantier_flow.py` (préfixe `hub:ch:`, sans état en mémoire : le nom du chantier passe en clair, `-` = Hors
  chantier) : `list` (`chantier tableau`, groupes 🟢 / 📥 / 💤, `z` = tous les 💤), `s:<nom>` (fiche, `chantier fiche`),
  `tl:<nom>` (todos ouvertes, `todo board --chantier <nom> | --hors-chantier`). Pas encore : ▶️, ⏹, ➕ Remarque, ⋯ Plus, 📥 À valider, 🗄.
- ➕ Remarque (todo #119) : bouton de la fiche (pas Hors chantier), `hub:ch:rq:<nom>` -> formulaire (pied ✖ Quitter / 🏠) et attente
  de texte 15 min (`ChantierFlow.wants_text`, arrêtée par tout autre bouton/commande via `moved_on`, par 🏠 et par l'échéance). Le texte
  est signé (`close_key.signed_env('reponse', 'R:<chantier>:<sha256(texte)[:16]>')`) puis `escouade remarque <chantier>` (texte sur
  stdin) ; retour sur la fiche avec « 📨 Remarque déposée ». `chantier fiche` donne `remarques.dernieres` (3 dernières, texte ≤ 80
  car., `at`, `lue_at`, ni sceau ni preuve) : « 👀 Lue à HH:MM » ou « ⏳ Pas encore lue ». Essai réel avec une vraie escouade : à faire.
- ➕ Nouveau chantier (todo #120) : dernier bouton de la liste, `hub:aut:newc` (`autonome_flow`, brouillon `to_card`) : nom puis
  titre + objectif sur UN message (`escouade nouveau`, objectif sur stdin ; nom refusé / sujet existant refusé), puis la fiche du
  chantier créé (`chantier fiche`, rendue par `chantier_flow.render_card`) au lieu de l'écran limite. Un seul chemin : depuis
  ▶️ Lancer d'« À toi » (mode court) la liste n'offre plus ➕ Nouveau ; `/escouade` tapée le garde. Essai réel : à faire (créer
  puis archiver un chantier d'essai). Un nom déjà archivé n'est pas vu par `escouade nouveau` (non traité).
- Contexte `k<h6>.0` (`h6` = 6 hex du sha1 du nom) dans `todo_flow._CTX` : la fiche todo revient à la liste du chantier ;
  `chantier_flow.todos_screen` retrouve le nom parmi `escouade sujets` + `-`, sinon « Chantier inconnu » et retour /chantiers.
- `todo board --chantier|--hors-chantier` liste aussi les todos de fond (le filtre « en tête » ne vaut que pour la liste générale).
- Commande `/chantiers` (`PLUGIN_COMMANDS`, `apres-fusion/todo-109.json`), porte `🏗 Chantiers` en tête de `home_flow.DOORS`.
- Lancer et arrêter (todo #112) : la fiche (pas « Hors chantier ») porte en tête `▶️ Lancer une escouade` (`hub:aut:pk:<s>`, entrée par
  nom d'`autonome_flow` : nom revérifié, sujet connu et libre, puis l'écran limite habituel ; son « Retour » = `hub:ch:s:<s>` via
  `Draft.back` ; le ▶️ signé est inchangé) ou, si `escouade` est renseignée, `⏹ Arrêter` (`x` = confirmation, `xs` = `escouade stop <id>`
  avec l'id relu dans `escouade sujets`, non signé) et la ligne « 🪖 Escouade <id> · <texte> ». Escouade finie entre-temps : message clair.
- ➕ Todo (todo #121) : l'écran « 📋 Todos : <chantier> » (pas « Hors chantier ») porte `➕ Todo` = `hub:todo:addc:<nom>` (nom validé `[a-z0-9][a-z0-9-]{0,39}`) ;
  c'est le formulaire ➕ Ajouter de `todo_flow` avec `Draft.chantier` : l'appel final ajoute `--chantier <nom>`, la fiche créée et ✖ Quitter
  reviennent à la liste du chantier (contexte `k<h6>`), la confirmation dit « dans le chantier <nom> ». Tout autre démarrage de formulaire efface le chantier.
- Libellé de nature : « 🎯 chantier » devient « 🔥 en tête » (affichage seulement, la colonne `nature` ne change pas).
- Tests : `tests/test_chantier_flow.py` ; `tests/test_ui.py` parcourt les nouveaux écrans.
- Domaines (todo #185) : chaque prompt `~/notes/prompt/<sujet>.md` porte une ligne d'en-tête `Domaine : <clé>` (`systeme` 🛠, `business` 💼,
  `perso` 🕌 ; liste `DOMAINES` dans `chantier/core.py`). Sans ligne ou clé inconnue : « ❔ À ranger ». `chantier tableau` rend `domaine`,
  `domaine_icone`, `domaine_titre` par chantier ; `/chantiers` groupe par domaine (🟢/📥/💤 marqués devant chaque volet, plus de bouton « 💤 Tous »).
  L'écran `hub:ch:z` (tous les 💤) reste servi pour les anciens boutons.

## Domaines (todo #185)

Chaque chantier appartient à un domaine, lu dans la ligne d'en-tête `Domaine : <clé>` de `~/notes/prompt/<sujet>.md` :
`systeme` (🛠 Système), `business` (💼 Business), `perso` (🕌 Perso). Absent ou inconnu : « ❔ À ranger ».
La liste vit dans `chantier/core.py` (`DOMAINES`). `chantier tableau` rend `domaine`, `domaine_icone`, `domaine_titre` par chantier.
