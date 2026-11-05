# Filets : sauvegardes, veilles, ménage, usage réel

Référence détaillée, sortie de CLAUDE.md le 18/10/2026 (todo #167).

## Filets : sauvegardes et alerte externe (todo #46)
- `ultron-hub heartbeat` (timer 5 min) : ping healthchecks.io `HC_VIVANT_URL`, `/fail` si le pouls du veilleur interne est
  mauvais (todo #159). Todo #460 : hermes-gateway (désactivé depuis le 18/10) n'est plus regardé, il rendait chaque battement `/fail`. URL dans `~/.config/ultron-hub/healthchecks.env` (0600, hors dépôt, chemin depuis `pwd`) ; absentes =
  « non configuré », jamais une erreur. Un ping raté ne fait jamais échouer la commande.
- `ultron-hub backup run|status|check` (`backup/run.py`) : restic + rclone (`~/.local/bin`), dépôt
  `rclone:gdrive:ultron-backup`, mot de passe `~/.config/ultron-hub/restic.pass` (copie gardée par l'utilisateur
  hors du serveur). Sources : `~/.hermes`, `~/.claude`, `~/notes`, `~/workspace`, `~/.omniroute`, `~/ultron` (esprit et
  carnet de savoir, ajouté par la todo #63), `~/.config/systemd/user`, `~/.config/ultron-hub`. Bases SQLite copiées par l'API `backup()` dans `~/.cache/ultron-hub/backup-staging` (fichiers
  vivants exclus). Secrets interdits aux agents **exclus** (`Layout.excludes`) : ne pas les ajouter sans décision
  de l'utilisateur. Rétention 7 j / 4 sem. / 6 mois ; `check` hebdomadaire sur 5 %. État : `var/backup/state.json`,
  lu par `/sante` (sonde `backup`). Ping `HC_SAUVEGARDE_URL` : `/start`, succès ou `/fail` (le check ne pingue
  qu'en cas d'échec).
- Allègement (todo #456, après 2 échecs Drive le 28/10 : rateLimitExceeded, 500) : rclone ralenti par `rclone.args`
  (`RCLONE_ARGS` : défauts de restic + `--tpslimit 4 --drive-pacer-min-sleep 200ms`) ; **une** nouvelle tentative de
  `restic backup` après 10 min si l'erreur est réseau/500/rateLimit (`is_transient`, attente injectable) ; exclus en plus :
  `venv`, `var/missions/*/work`, `var/worktrees`, `var/captures` d'ultron-hub, `notes/montage-video/references`,
  `.claude/projects/-tmp-*`. Contrôle : `data_added` > 300 Mo (`DATA_ADDED_WARN_BYTES`) pose `warning` dans `last_run`,
  affiché par `backup status`.
- Les tests ne lisent jamais la vraie config (fixture `no_real_backup_home`) et simulent restic et le réseau.

## Réflexes du veilleur (todo #161)
- Table `reflexes` de `config/sante.yaml` : `motif` (cherché sans casse dans l'erreur de la dernière sauvegarde ou du
  dernier `backup check`, sonde `backup`) → `diagnostic` → `action`, plus une `verification` de la liste fermée
  `health/reflexes.py:VERIFICATIONS`. Réflexe invalide ignoré ; fichier illisible = réflexe par défaut ; liste vide = aucun.
- `health alertes` (#160) appelle `reflexes.diagnostiquer` : motif trouvé ET vérification réussie ⇒ anomalie
  `reflexe:<nom>` (« 🩺 diagnostic : … → action proposée : … ») dans le même message, au changement d'état seulement.
  Diagnostics gardés dans `var/health/reflexes.json` (réécrit à chaque passage). Un réflexe en panne n'arrête jamais les alertes.
- Contrôles sans motif ni bouton (`CONTROLES`) : `dashboard-a-jour` (#294, source `dashboard`) alerte si le dashboard est plus ancien que le code de main (voir `docs/dashboard.md`).
- `tmux-hors-service` (#354, source `dashboard`) alerte si le serveur tmux des sessions `ultron` vit dans le cgroup d'un service `ultron-hub-*`/`ultron-bot*` (une fusion le tuerait) ; `deploy/bin/ultron` lance désormais tmux dans une unité `systemd-run --scope` propre, avec repli direct.
- `merge-check-orphelin` (#395, source `dashboard`) alerte si un worktree jetable `var/merge-check/wt-*|rv-*` (essai de fusion ou de retour arrière) date de plus d'1 h, sur disque ou dans `git worktree list` (inscrit sans dossier = signalé aussi). Lecture seule : le nettoyage reste celui de la fusion (`branches.sweep_merge_check`, sous verrou), donc pas de bouton ; la prochaine fusion 🔀 le balaie.
- Premier réflexe `verrou-restic` : « repository is already locked » + chaque verrou du dépôt > `verrou_min_h` (6, plancher)
  + pris sur cette machine + PID absent. L'inspection est celle d'ultron-user (`inspect_locks`, `restic --no-lock list locks`
  puis `cat lock`) : un seul code pour le diagnostic et l'action. Empreinte = 12 hex du SHA-256 des id de verrous triés.
- Bouton : `/sante` liste les diagnostics (`health reflexes`, sans réseau) → 🔓 Retirer le verrou (`rx:<nom>:<empreinte>`)
  → carte « À valider » → ✅ (`rxok:`) signe `reflexe` sur `<nom>:<empreinte>` → `health reflexe-agir <cible>` : preuve
  (jeton ou `--i-am-human`, refusée si déjà dépensée : `var/health/preuves-reflexes.txt` sous verrou), diagnostic encore
  présent, puis `ultron-user restic_unlock_stale <heures> <empreinte>`, qui revérifie TOUT au moment de l'appui (motif dans
  `var/backup/state.json`, aucune sauvegarde en cours via `var/backup/run.lock`, âge, machine, PID, mêmes verrous) puis
  `restic unlock` (jamais `--remove-all`). Rien d'automatique en v1. Tests : `tests/test_health_reflexes.py` (restic simulé ;
  fixture `no_real_restic` de `conftest.py`).

## Veilles (todo #85)
- `veille/core.py` (logique) + `veille/commands.py` (CLI). Dossier `~/ultron/veilles` (`ULTRON_VEILLES_DIR` pour les
  tests ; fixture `veilles_dir` de `tests/test_veille.py`). Lancé par `ultron-hub-veille.timer` (5 min).
- Script lancé sans shell, env minimal (HOME, PATH, LANG, `ULTRON_VEILLE`), cwd = `<dossier>/travail/`, groupe de processus
  tué au délai, sortie lue depuis un fichier temporaire (bornée), **empreinte sha256 revérifiée avant chaque lancement**.
- Canal (todo #393) : `veille.json` peut porter `"canal": "fil"` (CLI `veille canal <nom> fil|telegram`) ; les nouveautés vont alors
  dans le Fil seulement (push + historique, `envoi.envoyer(fil_seul=True)`), jamais sur Telegram. Défaut : Telegram.
- Bac à sable (todo #87, `veille/sandbox.py`, spec `~/notes/spec-veilles-sandbox.md`) : bwrap via `repos.sandbox_argv`, sans
  réseau, dossier de la veille en lecture seule, seul `travail/` inscriptible, `setrlimit` (pas de `systemd-run` : pas de
  D-Bus dans les bacs à sable). Jamais de repli hors bac à sable. `veille.json` : `lecture` (≤ 3 dossiers sous
  `~/workspace/projects/active/`) et `urls` (≤ 3, https, DNS publique, sans redirection) téléchargées HORS bac à sable par
  `fetch_pages` et données sur stdin (`pages`) ; revérifiées à chaque lancement. CLI `veille acces`. Action 🧰
  `veille_lecture <veille> <dépôt>` d'`ultron-user`. Tests : fixture `no_sandbox` (sauf `test_real_sandbox_probe*`).
- Création (todo #86, `veille/propose.py` ; le flux Telegram `veille_flow.py` est retiré, todo #463, la CLI et le dashboard restent) :
  `veille propose` (tâche `veille-script`, deux essais à blanc via `core.execute`, une seule relance) puis `veille creer`
  signé (action `veille`, cible = empreinte de `check` + `veille.json`, preuves dans `var/veilles-propositions/preuves.json`).
  Scripts écrits par Claude : `urls` seulement, jamais `lecture`. Plafond 10 actives, fréquence ≥ 15 min. Tests :
  `tests/test_veille.py` (tâche simulée, page simulée, bac à sable remplacé, sonde bwrap).
- Relancer (todo #223) : l'alerte de pause est un texte qui donne la commande (plus de bouton « 🔁 Relancer », todo #463) ;
  `veille relancer <nom>` = un seul essai `core.check` dans le bac à sable, sans avancer l'état ni envoyer ; réussi = `resume`,
  raté = reste en pause et rend la cause (sans signature : l'empreinte du script est revérifiée). L'erreur gardée est la
  DERNIÈRE ligne non vide de stderr (fin de 2 Kio), donc l'exception d'une trace Python.
- `run(send, journal)` : `send` lève en cas d'échec (l'état n'avance pas) ; le journal commun ne reçoit que le nom et
  le message court de la veille. Les tests n'envoient jamais de vrai message (`service.send_message` remplacé).

## Ménage du serveur (todos #124, #125)
- `config/menage.yaml` = carte de rangement officielle (dossiers attendus/permis/obsolètes, worktrees `<projet>.worktrees/todo-<id>`,
  jetables et âges, seuils, caches, code hors dépôt). Seuils jamais en dur dans le code.
- `ultron-hub menage scan` (`menage/rules.py`, `checks.py`, `commands.py`) : 18 vérifications, sans IA, JSON (`anomalies` ≤ 50 par
  vérification, `counts` = vrais totaux, `truncated`, `sizes`, `disk`, `warnings`). **Lecture seule absolue** : aucun `unlink`, `rename`,
  `rmtree`, écriture (test). Liens jamais suivis, `~/forum` et `~/.ssh` jamais parcourus, secrets : nom et taille seulement (jamais
  hachés). Disque = `health.collect.probe_disks`. Une vérification qui plante = ligne dans `warnings`, jamais une erreur.
- Premier scan réel (16/10) : 31 écarts justes après correction de 3 faux positifs (`__pycache__` de Hermes : seuls les orphelins
  comptent ; sources sans `.py` comme `ultron-priv` ; dossiers imbriqués de la vérif. 16). `var/merge-check` < 24 h = normal.
- Tests : `tests/test_menage_scan.py` (HOME jetable), `tests/test_menage_config.py`. `tests/test_menage.py` = 🧹 Ménage des todos (#54).
- `menage report [--send]` (todo #126, `menage/report.py`) : scan, retrait des chemins acceptés (`var/menage/ignores.yaml`, clé `paths`),
  texte Telegram ≤ 20 lignes (✅ / ⚠️ / 👉), version complète `var/menage/last.md`, `last.json`, `history.jsonl` (tailles, ≤ 200 lignes,
  relu par la vérif. 14). `tasks/menage-resume/` (haiku, `tools: []`, sans collecteur : les anomalies arrivent dans `{metrics}`, ≤ 3 Ko)
  **seulement s'il y a des anomalies** (test : zéro anomalie = zéro appel) ; échec de Claude = actions du script. Envoi `hermes send`
  comme `todo digest`, une ligne au journal. `menage last` rend le dernier texte. Seuls `report.py` et `audit.py` écrivent (dans
  `var/menage/`), rien dans `menage/` ne supprime ni ne déplace (test). Lancé chaque dimanche par
  `rapports preparer` (minuterie `ultron-hub-dimanche.timer`, todo #460, section « Minuterie du dimanche » ci-dessous), sans `--send`.
- `menage audit` (todo #127, `menage/audit.py`, tâche `tasks/menage-audit/` sonnet, `tools: []`, sans collecteur) : relit la carte, l'arborescence
  de `~` sur 2 niveaux (sans secrets, `~/forum`, `~/.ssh`) et les extraits de `serveur.md` et de la charte, puis **propose** des changements
  de `config/menage.yaml` (≤ 8, section = clé existante, YAML valide) dans `var/menage/audit-<date>.md` ; n'applique rien. `audit.json` garde
  la date : le rapport hebdo ajoute « 🔍 Lancer l'audit… : /menage » au-delà de `report.audit_every_days` (60), ligne jamais coupée.
- Plugin `hermes_plugin/menage_flow.py` (`hub:mng:`, `/menage`) : dernier rapport (`menage last`) + `🔍 Lancer l'audit` → `ui.approval_card`
  → ✅ `menage audit` (non signé : rien n'est modifié ; écran ⏳, verrou par chat). Après 🔀 : `apres-fusion/todo-127.json`.

## Usage réel (todo #166)
- Compteur du plugin (`hermes_plugin/usage_log.py`) : un `TypeHandler(Update)` en groupe -1 (vu avant tous les autres, ne les arrête
  jamais ; câblage raté = WARNING) écrit, pour chaque appui ou /commande d'un utilisateur autorisé, UNE clé : `hub:<flux>[:<verbe>]`
  (ids et noms coupés) ou `cmd:<nom>`. Jamais le texte libre, une légende ou un nom de fichier. `var/usage-reel/AAAA-MM.jsonl`
  (0600, dossier 0700, `O_NOFOLLOW`) = `{"t", "k"}` ; best effort, jamais d'exception.
- Lecture (`menage/usage.py`, sans IA) : `menage usage [--jours 30]` (top 10, portes jamais servies, `compteur_actif`) ; une ligne
  « 📊 Usage 30 j : … ; jamais servi : … » ajoutée au rapport hebdo du ménage (best effort). `usage.DOORS` = portes de ⋯ Plus, rangée
  ▶️ ➕ ⋯ d'À toi et menu `start video plus` : `tests/test_usage_reel.py` vérifie qu'elle suit le plugin. Rien n'est retiré tout seul.

## Ménage proposé, validé en un clic (todo #362, `menage_lot.py`, hors du paquet `menage/` qui reste en lecture seule)
- Todo #535, appliqué seul : `menage appliquer-auto` (et `rapports appliquer-auto`, aussi lancé par `rapports preparer` de la minuterie du dimanche) applique la proposition SANS ✅ quand elle ne supprime rien pour de vrai (todos annulées, chantiers archivés, skills et dossiers déplacés vers une `.trash`), `by` = `auto:bilan`. Refusé, rien ne change et le ✅ reste : depuis un job (`ULTRON_HUB_JOB`) ou une escouade, clé inconnue dans la proposition (suppression réelle), proposition bloquée, empreinte changée entre la lecture et l'application. Chaque application figure au bilan du soir (`bilan soir`, sorte `ménage` ou `rapports`) ; ↩️ = rouvrir la todo ou remettre depuis la `.trash`.
- Un agent prépare : `ultron-hub menage proposer --cancel ID:raison … --archiver chantier … [--par nom]` écrit
  `var/menage/proposition.json` (remplace la précédente ; refus si une todo n'est pas ouverte, un chantier pas ouvert, un doublon, une
  raison vide ou de plus de 200 caractères ou sur plusieurs lignes). Rien ne change avant le ✅.
- `menage proposition` (lecture) : lignes avec titre et `refus`, `bloquee` = premier refus, `empreinte` = 16 hex du SHA-256 des lignes
  `cancel:<id>:<titre json>:<raison json>` puis `archiver:<nom json>`, dans l'ordre du fichier.
- `menage appliquer --empreinte …` : jeton `menage-lot` signé sur l'empreinte (dashboard, ou `--i-am-human`), jamais rejoué (preuves dans
  `var/menage/propositions-appliquees.jsonl`). Tout est recalculé d'abord : todo plus ouverte, chantier parti ou empreinte différente =
  refus, RIEN appliqué, la proposition reste. Puis abandons (`cancel`, `by` du signataire + `via: menage-lot`, `empreinte` ; notes
  gardées, ↩️ Rouvrir les rend), puis archivages (`chantier archiver`, mêmes refus). Arrêt au premier refus, le reste signalé
  (`restants`). Dès que l'application commence, la proposition est consommée (copiée avec son résultat dans le jsonl).
- Skills à la corbeille (todo #451) : `menage skills` (`menage/skills.py`, sans IA, lecture seule) compte l'usage de chaque skill de
  `~/.claude/skills/` dans les transcripts (`Skill`, lecture ou `cat` de `skills/<nom>/SKILL.md`) : « corbeille » = aucun usage depuis 30 j et
  âge >= 30 j, « a_revoir » = jamais utilisée depuis 14 j. `menage proposer --corbeille NOM:raison` ajoute une ligne (empreinte
  `corbeille:<nom>:<raison>` APRÈS les autres) appliquée en dernier : déplacement vers `~/.claude/skills/.trash/<nom>-<date>`, jamais une suppression.
  Le rapport hebdo ajoute « 🧩 Skills » et, sans proposition en attente, propose les skills « corbeille » (par `menage-hebdo`).
- Dossiers à `var/.trash` (todo #474) : `menage proposer --dossier CHEMIN:raison` ajoute un dossier signalé par le ménage n° 10.
  Accepté seulement s'il est une entrée directe d'un dossier `disposable.aged_dirs` de `config/menage.yaml` (ou `<entrée>/<sous_dossier>`
  quand la clé est posée, ex. `work/` des vieilles missions, todo #464) ; refusés : chemin relatif, non normalisé ou avec `..`, lien
  symbolique (le dossier ou un élément sous la racine configurée), dossier caché, fichier, racine du projet ou au-dessus, dossier
  contenu dans un autre dossier proposé, config illisible. Son identité (`dev:inode:mtime_ns`) est gardée dans la proposition et
  entre dans l'empreinte (`dossier:<chemin>:<raison>:<identité>`, APRÈS les skills) : dossier disparu, remplacé ou modifié = proposition
  bloquée, rien appliqué ; revérifié juste avant le déplacement. Appliqué en dernier : déplacement vers `var/.trash/<3 derniers
  éléments du chemin>-<date>`, jamais une suppression (le remettre en place suffit). La carte « 🧹 Ménage proposé » montre la ligne
  « 📁 Corbeille (var/.trash) ».
- Tests : `tests/test_menage_proposition.py`.
- Todo #381 : ménage n° 10 couvre aussi `var/docs` (lots avec `presentation.md`), `var/video` (lots avec `fiche.md`/`resume.md`/`analyse.md`) via la clé `traite_si`, et `var/recus`, `var/notifs/pj` à l'âge seul (30 j, aucun marqueur fiable). Proposé seulement, jamais effacé seul.
- Todo #461 : les pièces jointes du Fil (`notifs.py`, `var/notifs/pj/`) sont des liens durs vers les fichiers 0600 de `var/` (même système de fichiers), sinon des copies ; la purge des PJ ne retire que le lien. Ménage n° 10 : `var/captures` et `var/worktrees` (restes `captures-*`) signalés après 7 j (lecture seule ; depuis la todo #474, `menage proposer --dossier` les fait passer par le 🧹 signé).

## Fichiers vivants trop gros (todo #168, vérification n° 19 du ménage)
- `config/menage.yaml` section `budgets.files` : motif glob → Ko max (CLAUDE.md d'ultron-hub 30, prompt orchestrateur 12,
  `~/notes/chantier-*.md` 15, `~/notes/prompt/*.md` 12, `MEMORY.md` de la mémoire 4). Section absente = vérification muette.
- `checks.check_19_living_files` : taille seulement (`lstat`), liens ignorés ; action proposée « ranger l'historique dans archives/
  ou docs/ ». Le rapport compte désormais `len(CHECK_LABELS)` vérifications (19). Une anomalie acceptée va dans `ignores.yaml`.
- Geste automatique (todo #231, `menage/rangement.py`, tests `tests/test_menage_rangement.py`) : `menage report` (sans scan fourni) appelle,
  AVANT le scan, `archiver_reprises` : chaque fichier de `budgets.archiver_seul` (ici `reprise-orchestrateur.md`) qui dépasse son
  budget est archivé par `esprit.reprise.archive` (le code de `esprit reprise-archiver`, 3 dernières entrées + FOCUS gardées, aucune
  ligne perdue, sinon rien d'écrit) et une ligne « menage » va au journal. Jamais d'exception (échec = le scan signale le fichier
  comme avant). `menage scan` reste en lecture seule.
- Premier passage réel (18/10, avant fusion de #167) : 6 fichiers (CLAUDE.md 108 Ko, prompt orchestrateur 16, fiches agent-ultron 24,
  escouade-autonome 44, fba-reflexion 34, MEMORY.md 5). Spec : `~/notes/spec-memoire-consolidee.md`.

## Sujets d'escouade incomplets (todo #216, vérification n° 20 du ménage)
- `checks.check_20_squad_subjects` : `escouade.core.incomplets(~/notes/prompt)` (assemblage à blanc, rien d'écrit) ; une anomalie par
  sujet qui ne s'assemble pas (ex. `novapro.md` sans « ## 4. »). Le rapport compte 20 vérifications.

## Minuterie du dimanche (todo #460)
- UNE minuterie `deploy/systemd/ultron-hub-dimanche.{timer,service}` (dimanche 10 h, `Persistent=true`, `TimeoutStartSec=2400`)
  lance `ultron-hub rapports preparer` (`rapports.py`). Elle remplace les 4 minuteries du dimanche (focus 9 h, ménage 10 h, cap
  10 h 30, bilan 19 h) qui envoyaient chacune leur message, et la minuterie quotidienne `todo-digest` (8 h, doublon de « Fait
  aujourd'hui » du Briefing et de l'onglet Travail ; la commande `todo digest` reste, sans minuterie : plus de rattrapage
  quotidien des `clair` manquants ni de régénération quotidienne de la page d'état, qui reste faite à chaque fusion).
- Étapes, chacune lancée même si une autre a échoué (exception = ligne d'échec) : `menage report` (écrit `var/menage/`),
  `cap report` (écrit `var/cap/`, `last.json` lu par la carte « Rapports du dimanche » de Moi ; revue sautée pour quota = pas un
  échec), `bilan semaine` (comptage, texte seulement), puis la question du focus (focus actuel, « le changer : Moi > 🧭 Focus »).
  Aucune n'envoie son propre message : UN seul message au Fil, « 📋 Rapports du dimanche prêts, onglet Moi » + une ligne par
  rapport (« ❌ échec : … » compris), genre `question` (il finit sur la question du focus), sujet `revue-de-cap`.
- Trace : `var/rapports/preparation.json` (résultat de chaque étape, texte, envoi) et une ligne au journal commun. Une étape en
  échec ou un envoi raté = code de sortie 1 (unité en échec, vue par `health alertes`). Santé : `ultron-hub-dimanche.timer`
  remplace `todo-digest` dans `USER_TIMERS` de `health/collect.py`.
- Après 🔀 : `apres-fusion/todo-460.json` = `user_unit_enable ultron-hub-dimanche.timer` puis `user_unit_disable` de focus,
  ménage, cap et todo-digest (5 actions au plus par déclaration : `ultron-hub-bilan.timer` est à désactiver à part).
  `user_unit_disable` (ultron-user) : un `ultron-hub-*.timer` dont le lien de `~/.config/systemd/user` pointe vers
  `deploy/systemd` du dépôt (le fichier peut avoir été retiré par la fusion) ; `disable --now`, retire les liens du timer et de son
  service s'ils pointent vers le dépôt, `daemon-reload`, vérifie qu'il n'est plus dans `timers.target.wants` ; pas de lien = rien à faire.
- Tests : `tests/test_rapports_preparer.py`, `tests/test_user_actions.py` (`user_unit_disable`).

## Bac à sable et frontières de sécurité (repris de l'ancien `docs/missions.md`, missions retirées par la todo #455)
- Le moteur de missions (`missions/`, `mission plan|start|…`, 🤖 Confier, 🚀 Demande, /actifs, 🧩 Mission en équipe) est retiré
  (todo #455, archive : tag `archive/missions-2026-10-28`). Restent : le runner `ultron-hub run <tâche>` (`core/`, `executors/`),
  la sandbox bwrap, `ultron-priv`, `ultron-user` et les actions après fusion.
- Code non relu (branche à fusionner) : tests **toujours** via `repos.run_tests` (bubblewrap, sans réseau, home et `/run`
  masqués). Les tests unitaires remplacent `repos.sandbox_argv` (fixture `no_bwrap`) ; la vraie sandbox est sondée par
  `tests/test_repos.py::test_real_sandbox_probe` (ignoré sous bwrap imbriqué). Délai `TESTS_TIMEOUT_S` = 900 s ; au-delà de
  80 % (720 s) `run_tests` ajoute `warning` + une ligne au `detail` et journalise `step=tests-duree` : suite à alléger (todo #414).
- Sandbox vérifiée le 2026-10-10 (bubblewrap 0.9 + socat + profil AppArmor `/etc/apparmor.d/bwrap` avec `userns`, requis par
  Ubuntu 24.04). Refaire la sonde après toute mise à jour.
- Échecs de `claude -p` classés (`auth`, `quota`, `transient`, `other`) dans `claude_cli.classify_failure`, sur les **seuls**
  champs d'erreur (stderr, `error(s)`, `subtype`, `api_error_status`, début de `result`) : le texte rédigé par l'agent n'est
  jamais fouillé. Seul `transient` est relancé, une fois. Les coûts `cost_usd` sont des estimations en équivalent API
  (abonnement Pro, non facturé) : toujours « ≈ … $ éq. API (non facturé) ».
- `deploy/priv/ultron-priv` est la frontière de sécurité root : bibliothèque standard seulement, aucune dépendance à
  ultron_hub, `fullmatch` pour toute validation, chemins absolus, jamais de shell. Toute nouvelle action = cas de refus testés
  dans `tests/test_privileged.py`, puis `sudo sh deploy/priv/install.sh`. Aperçu : `ultron-hub priv preview`.
- `deploy/user/ultron-user` est la frontière de sécurité sans sudo : mêmes règles (bibliothèque standard, pas d'`ultron_hub`,
  `fullmatch`, chemins absolus, jamais de shell) + environnement construit par l'outil (jamais `os.environ`), chemins depuis
  `pwd`, refus en root. Lancé depuis le dépôt (`user_actions.run`), sans installation. Liste blanche fermée : `hermes_menu_add`,
  `hermes_menu_set` (1 à 6 commandes du plugin), `user_unit_enable`, `user_unit_disable` (todo #460), `user_unit_restart`, `hermes_pip_install` (`PIP_ALLOWED`,
  **vide**), `hermes_config_set` (3 clés, valeurs fermées), `montage_install`. Toute nouvelle action, clé, valeur ou paquet = cas
  de refus dans `tests/test_user_actions.py`, relu par l'utilisateur avant 🔀. Jamais : secrets, `~/.ssh`, sudo, commande libre,
  clé MAJUSCULE (`hermes config set` l'écrit dans `~/.hermes/.env`). Les tests n'exécutent rien (`spec._run` et les
  `__defaults__` de `after_merge.decide` remplacés).
- **Secrets interdits aux sessions Claude Code** (todo #485). `~/.local/bin/ultron` lance les sessions en
  `--dangerously-skip-permissions` : les règles `allow` y sont ignorées, mais les règles `deny` bloquent dans tous les modes,
  bypass compris (https://code.claude.com/docs/en/permission-modes). `claude_hooks_sync` (ultron-user) écrit donc, à côté
  des hooks, la liste fixe `permissions.deny` de `CLAUDE_DENY_PATHS` × `CLAUDE_DENY_BASH` dans `~/.claude/settings.json`
  (ajoute seulement les règles qui manquent, garde le reste, copie `.bak-<date>`) : `Read(…)` et `Edit(…)` sur
  `~/.config/ultron-hub/**`, `~/.claude/.credentials.json`, `~/.ssh/**`, `~/.sudo_pass`, `~/.hermes/.env`, `~/.hermes/google_*`,
  `~/.config/gh/**`, `//**/auth.json`, plus `Bash(<cmd> *<fragment du chemin>*)` pour cat, head, tail, less, cp, base64,
  grep, sed, python3, sh, bash… Syntaxe (https://code.claude.com/docs/en/permissions) : motifs gitignore, `~/` = home,
  `//` = racine ; un deny `Read` bloque aussi Write et les commandes de fichier que Claude Code reconnaît en Bash ;
  une règle `Write(chemin)` ne correspond jamais à rien (non écrite). Installé après le 🔀 par
  `deploy/user/apres-fusion/todo-485.json`.
  - Essai réel du 31/10 (`claude -p --model haiku --permission-mode bypassPermissions --settings <deny de test>`, fichier
    leurre) : **refusés** : Read, `cat`, `base64`, `/bin/cat`, `cd <dossier> && cat <fichier>`, `sh -c 'cat …'`,
    `bash -c 'head …'`. **Passé** : `python3 -c` qui ouvre un chemin construit à l'exécution (`'leurre'+'-485'`).
  - Ce que deny **ne couvre pas** (doc officielle + essai) : tout programme qui ouvre le fichier lui-même sans que le
    chemin apparaisse en clair dans la commande (python, node, script écrit puis lancé, variable, `find -exec`, `xargs -n1`),
    un `grep -r` lancé depuis le dossier parent, les sous-processus (dont les hooks et le CLI `ultron-hub`, qui doivent
    continuer à lire `close.key`). deny est un garde-fou contre l'accident, **pas une frontière** : seul le bac à sable
    (bubblewrap, home masqué) l'est ; il devient obligatoire pour les sessions (todo à venir). Effet de bord voulu :
    `grep …/.config/ultron-hub…` ou `cat …auth.json` en clair sont refusés même pour du code (utiliser l'outil Grep).
- Actions **après fusion** (`after_merge.py`, seul usage d'ultron-user) : la branche ajoute
  `deploy/user/apres-fusion/todo-<id>.json` (`{"actions": [{action, args, reason}]}`, ≤ 5). Lu **dans main** (`git show`) à la
  fin d'un 🔀 réussi, validé, gardé dans `var/after-merge/todo-<id>.json` ; boutons « 🧰 Actions après fusion »
  (`hub:merge:a|aq|ax|af:…`), signés `user` sur `todo-<id>:<n>` (`branch after-run`). Une déclaration illisible n'annule jamais
  la fusion. `branch after-run` lance `branch after-exec` dans une unité `ultron-after-merge-<id>-<n>` (`systemd-run --user
  --wait`, hors du cgroup de la gateway) ; `after_merge.reconcile` passe en `interrupted` une action `running` depuis plus de
  30 s dont l'unité n'existe plus (rejouable avec 🧰).
- Validation par regex : toujours `fullmatch` (avec `match`, `$` accepte un saut de ligne final).

## La garde des interdits (todo #493, chemin A' étape 0)
- `deploy/hooks/garde.py` : hook PreToolUse de **toutes** les sessions Claude Code, installé par `claude_hooks_sync`
  (ultron-user, après 🔀). Bibliothèque standard, lancé comme fichier (`.venv/bin/python -I …`, ~35 ms), jamais par la CLI.
- Refuse, quel que soit le mode de permission : sudo/su/doas/pkexec et arrêt du serveur ; tout chemin de secret nommé par
  un outil ou un mot Bash (même en lecture) ; écrire `~/.claude/settings*.json` ; `git push` ; commit/merge/rebase/reset/pull
  sur main ; supprimer ou déplacer main ; `todo done|cancel`, `branch merge|merge-lot`, `--i-am-human` ; systemctl système hors
  lecture (`--user` permis) ; `rm -r` de /, du dossier personnel ou d'un de ses dossiers de premier niveau.
- Un texte avec des espaces (note, message de commit) n'est pas un chemin : `--notes "… auth.json …"` passe.
- Erreur interne de la garde = appel laissé passer (stderr) : un bug ne doit pas bloquer toutes les sessions ; les règles
  `permissions.deny` (todo #485) et le mode auto restent en place. Garde-fou contre l'erreur, pas un bac à sable.
- Tests : `tests/test_garde.py`.
- **Clés des applis métier** (todo #519) : `APP_KEY_DIRS` (aujourd'hui `supertool`, clés SP-API et Keepa) déclare les
  dossiers `~/.config/<appli>/` interdits ; la garde (`SECRET_FRAGMENTS`) et `CLAUDE_DENY_PATHS` en dérivent tous deux.
  Liste déclarée plutôt que `~/.config/**`, qui bloquerait les unités systemd et les réglages git ou tmux ; le dépôt de
  l'appli (`~/workspace/projects/active/supertool`) reste lisible. Nouvelle appli = un nom dans les deux tuples (un test
  les compare). La garde s'applique dès le 🔀 ; les règles deny après « 🧰 Actions après fusion »
  (`deploy/user/apres-fusion/todo-519.json`, `claude_hooks_sync`).
