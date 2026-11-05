# Revue de cap : faits (todo #189), passage Opus (todo #190), envoi (todo #191)

Carte : `src/ultron_hub/cap/collect.py` (faits), `cap/report.py` (Opus), `cap/commands.py` (CLI), tâche `tasks/revue-cap/` ;
tests `tests/test_cap_collect.py`, `tests/test_cap_report.py` ;
spec `~/notes/spec-revue-cap.md` §3.1. Réutilise `chantier/core.py` (tableau), `esprit/focus.py`, `esprit/store.py`
(journal), `menage/audit.py` (exclusions de secrets).

## `ultron-hub cap collect`
Sans IA, lecture seule absolue (test), liens jamais suivis, secrets jamais lus (YAML `secrets` et `exclusions` du
ménage, plus `~/forum` et `~/.ssh`). Une source illisible = une ligne dans `warnings`, jamais une exception.
Fenêtre : 7 jours glissants (`now` injectable).

Sortie : `{status, command: "cap.collect", summary, data, error}` ; `data` =
- `depuis`, `jusqu_a`, `focus` (domaines) ;
- `chantiers[]` (focus d'abord) : `nom`, `titre`, `domaine`, `focus`, `groupe`, `ou_en_est`, `a_toi[]`, `objectif`,
  `decisions` (extraits de la fiche, 1500 car. max), `todos` {`ouvertes`, `livrees`, `closes`, `abandonnees`} ;
- `commits[]` (sujets de `main`, `sha`, `todo`), `journal` {`par_source`, `dernieres`}, `echecs` {`missions`,
  `menage`, `reflexes` (diagnostics du veilleur, `var/health/reflexes.json`, best effort)}, `revue_precedente` (`var/cap/last.json` si présent) ;
- `non_revus[]` et `warnings[]`.

Budget 40 Ko : au-delà, les chantiers hors focus sont retirés (le dernier d'abord) et listés dans `non_revus`, puis
journal et commits sont réduits, en dernier recours les extraits.

## `ultron-hub cap report`
Un passage **Opus** (modèle fixé dans `tasks/revue-cap/task.yaml`), `tools: []`, lecture seule : il ne modifie ni ne
lance rien. Les faits de `cap collect` vont dans `{metrics}` entre balises, marqués données non fiables (modèle
`menage/audit.py`). 

Réponse (schéma `output.schema.json`, revalidée en Python) : `summary`, `chantiers[]` {`nom`, `verdict`
vert|orange|rouge, `objectif`, `technique`, `derives`, `preuves[]` >= 1}, `corrections[]` {`titre` <= 40 car.,
`chantier`, `notes`, `preuve`}. Au-delà de 3 corrections, les suivantes sont coupées ; une correction au titre trop long
ou au chantier inconnu est écartée (avertissement au journal) ; une réponse mal formée = rapport de repli.

Garde quota : aucun seuil par défaut (todo #424) ; seulement si `report.QUOTA_MAX_PCT` est posé explicitement, semaine au-dessus = aucun appel, rapport « revue sautée (quota) ». Semaine illisible =
la revue a lieu. Échec de Claude, réponse invalide ou collecte impossible = rapport « revue impossible : <raison> »
avec les faits bruts résumés, jamais d'exception (statut `failed`, `skipped` pour le quota, sinon `ok`).

Fichiers (0600, dossier 0700, liens jamais suivis) : `var/cap/<date>.md` et `.json` (avec `cost_usd` et
`semaine_pct`) ; `var/cap/last.json` seulement après une revue réussie, pour que `collect` garde la dernière vraie
revue comme `revue_precedente`. Une écriture impossible n'est pas fatale (`path: null`).

## `ultron-hub cap report --send` et timer
Même chemin d'envoi que `menage report --send` (`todo.service.send_message`, cible `todo_notify_target`). Texte
(`report.short_text`) <= 25 lignes : titre, une ligne par chantier (🟢/🟠/🔴 + dérive ou objectif, 80 car.), corrections
numérotées, coût et % semaine, puis « 👉 /cap pour créer les todos ». Trop de chantiers : « +N autre(s) » renvoie à
`var/cap/<date>.md`. Revue sautée ou impossible : titre, raison, faits bruts, coût (pas de ligne /cap). Un envoi qui
échoue n'est pas fatal (`sent: false`, `send_error`). Une ligne au journal commun (source `ultron-hub`, genre `cap`).
Lancé sans `--send` chaque dimanche par `rapports preparer` (minuterie `ultron-hub-dimanche.timer`, todo #460, qui remplace
`ultron-hub-cap.timer` ; `docs/filets.md`). Tests : `tests/test_cap_send.py`.

## `ultron-hub cap last` (todo #192)
`cap last` lit `var/cap/last.json` (lecture seule) : `{exists, date, text (texte Telegram), corrections[]}`. Le `/cap` du bot Telegram et
ses cartes par correction sont retirés (todo #463) : le texte de la revue part sans bouton, les corrections se créent depuis le dashboard
ou à la main (`todo add --chantier`). Tests : `tests/test_cap_last.py`, `test_cap_send.py`, `test_cap_report.py`.

## `ultron-hub bilan semaine [--send]` (todo #256)
Code `cap/bilan.py`, CLI dans `cap/commands.py` (`handle_bilan`), test `tests/test_cap_bilan.py`. Sans IA, 7 jours glissants,
chaque source en best effort (`warnings`). Sortie : `data` = `todos`, `boutons`, `ratio`, `quota_semaine_pct`, `text`.
- Todos finies (closes `done` ou livrées ouvertes, `livree` en dernier événement) par `nature` `fond` / `chantier`. Aucun marqueur
  d'usage réel n'existe sur les todos : la nature est le seul découpage disponible.
- Boutons : `var/usage-reel/*.jsonl` groupés par action (`hub:<module>`, `cmd:<nom>`), part de `hub:merge`.
- Scripts contre IA : lignes `Started ultron-hub-*.service` de `journalctl --user` (fonction injectable) contre les
  `var/runs/*/request.json` des 7 jours (champ `action`).
- Quota : `report.week_percent` (module `usage`, pas d'IA) ; `cout` ne mesure que des sessions, donc « non mesuré » si illisible.
- Envoi : `todo.service.send_message`, sujet `revue-de-cap`. Lancé sans `--send` chaque dimanche à 10 h par
  `rapports preparer` (minuterie `ultron-hub-dimanche.timer`, todo #460, qui remplace `ultron-hub-bilan.timer`).
