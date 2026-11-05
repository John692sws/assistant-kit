# Todos : nature, chantier, ranger

Référence détaillée, sortie de CLAUDE.md le 18/10/2026 (todo #167).

## Nature des todos (todo #52)
- Base **v2** (`todo/store.py`, `PRAGMA user_version`) : colonnes `nature` (`chantier`|`fond`, CHECK) et `clair`
  (≤ 140 car., une ligne). Migration automatique à l'ouverture du store, après copie `var/ultron.db.bak-v1`
  (API `backup()`, jamais écrasée) ; idempotente. Lignes existantes = `chantier`, **nouvelle todo = `fond`**
  (`todo add`, `store.add`) ; les todos créées par une escouade sont des chantiers.
- Changement de nature (`todo edit --nature … --by …`) = événement `nature` (`from`, `to`, `by`) + ligne du
  journal de l'esprit ; non signé. Le plugin passe `--by telegram-button`.
- `todo board` : le filtre `open` sans catégorie ne liste que les chantiers et donne `fond` (compteur) ; le
  filtre `fond` liste le fond. Côté plugin, filtre `f` du contexte `<filtre>.<page>` (`todo_flow.py`).
  Callbacks `nt:<id>:<ctx>:<c|f>` ; `ef:…:C` = saisie du « En clair ».
- `tasks/todo-clair/` (haiku, `tools: []`) rédige `clair` : à la création si `--clair` est absent, et par
  `todo digest` (au plus 5 todos ouvertes sans `clair` par passage, 60 s de budget). Tout est best effort
  (`service.fill_clair`). Le résumé de 8 h ne détaille que les chantiers + « 🌱 N tâches de fond ».
- Rappel À toi (todo #164) : sous le titre du résumé de 8 h, « 📥 À toi : N choses attendent ton ✅ (🔀 2 · ✅ 1) → /start »
  (`service.atoi_line`, comptes par icône tirés de `chantier atoi` par `commands._atoi_counts`) ; rien si N = 0 ; À toi illisible =
  résumé envoyé sans la ligne (WARNING). `data.atoi` = total.
- Tests : la fixture automatique `no_real_clair` interdit tout vrai appel ; un test qui veut la génération
  utilise le faux `claude` avec `summarizer.llm_clair` importé avant la fixture (`tests/test_nature.py`).

## Chantier des todos (todo #103)
- Base **v3** (`todo/store.py`) : colonne `chantier` (NULL = hors chantier, sinon 1 à 40 car., CHECK). Migration v2 -> v3
  automatique à l'ouverture, copie `var/ultron.db.bak-v2` (jamais écrasée), idempotente, `user_version` relu sous verrou.
- Retour arrière : `ultron-hub todo schema-retour --vers 2` (`store.downgrade_to_v2`) copie `var/ultron.db.bak-v3`
  (`.bak-v3.N` si elle existe), retire la colonne (`DROP COLUMN`, sinon recopie de table) et remet `user_version = 2` ; les
  chantiers sont perdus dans la base, gardés dans la copie. Sans cela, le code v2 refuse une base v3. `backup()` de sqlite3
  ne doit jamais tourner dans une transaction d'écriture de la même connexion (blocage sans fin).
- Valeur permise = sujet existant (`service.known_chantiers` : `<prompts>/<s>.md` ou `termines/<s>.md`, dossier de
  `ULTRON_PROMPTS_DIR`), sinon refus « chantier inconnu ». Le filtre `--chantier` de `list`/`board` ne vérifie pas.
- CLI : `todo add --chantier`, `todo edit --chantier <s> | --sans-chantier --by …` (événement `chantier` {from,to,by} + ligne du
  journal, non signé comme `nature`), `todo list|board --chantier <s> | --hors-chantier`. `show` et `list` rendent `chantier`.
- Tests : `tests/test_chantier.py` (dont deux processus et le retour v3 -> v2 rouvert comme le ferait le code v2).
- Todos d'escouade (todo #105) : `moteur.build_env` passe `ULTRON_ESCOUADE_SUJET=<sujet>` à la session (lancement et reprise) ;
  `todo add` sans `--chantier` le prend (si c'est un sujet connu, sinon hors chantier sans erreur) et sans `--nature` met
  `chantier`. La fixture `escouades_dir` de conftest retire la variable (pytest lancé depuis une escouade).

### Ranger (todo #104)
- `todo rattacher --proposer` rend `lines` (`id`, `titre`, `de`, `vers`) + `empreinte` (8 hex du SHA-256 des lignes) ; table fixe
  `service.RATTACHEMENT` (spec v3 §3.1, 25 todos dont 7 hors chantier). Seules les todos **ouvertes** dont le chantier diffère, vers des
  sujets connus (`known_chantiers`), sont proposées.
- `--appliquer <h8> [--by …]` recalcule la proposition : empreinte différente = refus, rien modifié ; sinon un changement de chantier
  par ligne (événement `chantier`, `by`, journal). Non signé, réversible par `todo edit --sans-chantier`.
- Telegram : bouton temporaire `🗂 Ranger les todos` de /todo (`rg`) -> `ui.approval_card` ; ✅ = `hub:todo:rt:<h8>` (`--by telegram-button`).
  Retirer le bouton et la table une fois les todos rangées. Tests : `tests/test_rattacher.py`.

## Clôture auto vérifiée (todo #528)
`ultron-hub todo close-auto <id>` ferme une todo **sans jeton** (décision du 03/11 : Ultron clôt seul une todo fusionnée et
vérifiée ; sécurité, envoi à un tiers, argent gardent le ✅). Code : `closing.verify_auto_close` (vérifie, n'écrit rien), puis
la CLI passe le statut à `done` avec `by=auto:verifie`. Toutes les preuves sont exigées :
- appel hors job et hors escouade (`ULTRON_HUB_JOB`, `ULTRON_ESCOUADE_DIR` = refus, comme `branch auto-merge`) ; todo ouverte ;
- une fusion automatique enregistrée pour la todo (`var/fusions-auto.jsonl`, by `auto:orchestrateur`) : elle n'est écrite que
  si les tests sont passés sur le résultat fusionné (`merge_and_close`) ; non annulée par ↩️ (`revert-last`) ;
- la dernière livraison (`livree`) est celle de cette fusion (by `auto:orchestrateur`, sha12 dans la note) et aucun changement
  de statut ni `fusion-annulee` ne l'a suivie ;
- chaque commit de fusion est dans main, a deux parents et le sujet « Fusion [automatique ]de todo-<id>/… » ; une fusion
  faite à la main avec git (sans trace des tests) ne suffit pas ;
- aucune nouvelle branche `todo-<id>/…` n'attend ;
- le diff de la fusion (`<sha>^1..<sha>`) ne touche rien de `auto_merge.SECURITY` ni un nom de secret (`auto_merge.security`).
Refus : `CloseRefused`, WARNING `todo=<id> step=close-auto-<étape> reason=…` et journal « Clôture auto refusée : … », la todo
reste ouverte. Succès : `events.details` = `by`, `proof` (sha12 de la fusion), `cible` (`<id>-<sha12>`), `fusion` (dépôt, sha),
`via: close-auto` ; journal « Todo terminée seule (fusionnée et vérifiée, <sha12>) ». La logique vit dans `closing.py` (déjà dans
SENSITIVE et SECURITY) pour ne pas changer la liste SECURITY, ce qui annulerait l'autorisation d'avance en cours.
Tests : `tests/test_close_auto.py`.
