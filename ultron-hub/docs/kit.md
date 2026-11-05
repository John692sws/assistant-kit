# Kit public : détecteur de données personnelles

Avant tout export du kit (« assistant-kit »), `ultron-hub kit scan <dossier> [--interdits <fichier>]` cherche toute
donnée personnelle. Lecture seule, sans IA, sans réseau, bibliothèque standard (`src/ultron_hub/kit/`).

- **Règles intégrées** : `/home/<nom>` en dur (`home-en-dur`), IP 100.64.0.0/10 (`ip-tailscale`, la chaîne exacte
  `100.64.0.0/10` est tolérée), `*.ts.net` (`tailnet-ts-net`), adresse mail (`adresse-mail`), jeton Telegram
  (`jeton-telegram`).
- **Noms interdits** (`nom-interdit`, ligne 0) : dossier `var/`, `*.db`, `*.key`, `*.pem`, `*.pass`, `.env`,
  `auth.json`, `credentials*`, `config/dashboard.yaml`, `config/groupe.yaml`.
- **Liste personnelle** (`interdit-perso`) : `~/.config/ultron-hub/kit-interdits.txt`, hors dépôt, un motif littéral par
  ligne, insensible à la casse, `#` = commentaire. Chemin dérivé de `pwd` (pas de `$HOME`). Fichier absent ou vide :
  le scan est **refusé** (code 1, `status: error`), on ne scanne pas à l'aveugle. Les noms de fichiers sont aussi testés.
- **Sortie** : `{status, command, summary, data: {findings: [{path, line, rule}], count}, error}`. Jamais la valeur
  trouvée. `status` vaut `ok` (rien), `found` (code 1) ou `error` (code 1).
- Ignorés : binaires (NUL dans les 8 premiers Ko), `.git`, `.venv`, `__pycache__`, `.pytest_cache`, liens symboliques.

## Export : `ultron-hub kit export <dossier-sortie> [--interdits <fichier>] [--essai]`

Construit `<dossier-sortie>/ultron-hub/` à partir des fichiers **suivis par git au commit de `main`** (`git ls-tree` +
`git archive`), jamais de l'arbre de travail ni de `var/`. Liste blanche dans `config/kit.yaml` (`include` / `exclude`,
motifs `fnmatch` sur le chemin relatif, `*` traverse les `/`). L'exclusion l'emporte, puis l'inclusion ; un fichier dans
aucune des deux listes est signalé dans `data.non_classes` et **pas copié**. Liens symboliques ignorés.
- Refus (`status: error`) si le dossier de sortie existe et n'est pas vide, ou si la liste d'interdits est absente ou
  vide (vérifié avant toute écriture).
- Après la copie, le détecteur tourne sur le résultat. Trouvailles : `status: found`, code 1, `data.publiable: false`,
  le dossier reste pour inspection. Sinon `data.publiable: true`.
- `--essai` : n'écrit rien, rend `fichiers_retenus`, `fichiers_exclus`, `non_classes` et leurs nombres.
- Options cachées pour les tests : `--depot`, `--ref` (défaut `main`), `--config`.

## Gabarits et remplissage (todo #388)

- `kit/gabarits/` (versionné) : charte, socle de prompt, CLAUDE.md global, profil vide, `settings.yaml`, `dashboard.yaml`
  vide, unités systemd, agents launchd (`launchd/`, Mac). Marqueurs `{{NOM_ASSISTANT}}`, `{{PRENOM}}`, `{{UTILISATEUR}}`, `{{MACHINE}}`, `{{HOME}}`,
  `{{LANGUE}}`, `{{FUSEAU}}`. Génériques : aucun nom, compte ni appareil.
- `kit/setup/remplir.py <reponses.json> <dossier>` (bibliothèque standard) : remplace les marqueurs dans tous les
  fichiers texte. Refus (code 1, liste JSON) s'il reste un marqueur sans réponse ou si une réponse est vide ; rien n'est
  écrit dans ce cas.
- `kit export` copie aussi `kit/gabarits/` vers `<sortie>/gabarits/` et `kit/setup/` vers `<sortie>/setup/` (le détecteur
  scanne tout `<sortie>`, chemins préfixés `ultron-hub/`, `gabarits/`, `setup/`).
- `remplacer:` dans `config/kit.yaml` : chaînes littérales exactes (jamais le mot « ultron » seul) remplacées dans les
  fichiers texte de `<sortie>/ultron-hub/` avant le détecteur (`/home/exemple` -> `{{HOME}}`, `serveur-test` -> `{{MACHINE}}`).
- Exclus du kit : tout ce qui est lié à Hermes, Telegram, la bascule du bot ou remplacé par un gabarit (`deploy/user`,
  `deploy/prompt`, `deploy/escouade`, `deploy/esprit`, `deploy/navigateur`, `deploy/veilles`, `scripts/*` sauf
  `install-systemd.sh`, `config/{contexte,menage,prompt-essais,sante,settings}.yaml`).

## Setup guidé (todo #389)

- `kit/README.md` et `kit/CLAUDE.md` sont copiés à la racine de `<sortie>` (`EXTRA_FILES` de `kit/export.py`) : le README pour
  l'utilisateur (« colle ce lien et dis : installe »), le CLAUDE.md pour son Claude Code (questionnaire, étapes dans l'ordre,
  contrôle après chacune, jamais sudo, aucun secret, `settings.json` jamais écrasé).
- `kit/setup/questions.md` : questions, marqueur rempli par chaque réponse, formats de `reponses.json` (marqueurs, lu par
  `remplir.py`) et `choix.json` (le reste). `kit/setup/etapes.md` : étapes 0 à 9, Linux et Mac, chacune avec son contrôle ;
  sur Mac, les étapes 7 et 8 posent des agents launchd (voir « Mac »).
- `kit/setup/hooks.py <hub> [--essai]` : fusionne les 3 hooks de l'esprit dans `~/.claude/settings.json` (même liste que
  `CLAUDE_HOOKS` de `deploy/user/ultron-user`, absent du kit), sauvegarde unique `settings.json.avant-ultron`, idempotent.
- `kit/setup/verifier.sh` : contrôle final en lecture seule, une ligne OK / KO / SAUTÉ par point, code 0 sans KO ; sur Mac
  (`uname` = Darwin, ou `KIT_OS` pour les tests) les points dashboard et santé lisent launchd (`launchctl print`).
- `kit/gabarits/prompt/` : orchestrateur (section `## 1.`) et Reprise minimaux, pour que `ultron` assemble son prompt ;
  `_socle.md` (sections 2, 3, 5, 6, 7) exigé par `escouade nouveau`, seule commande qui crée une fiche de chantier.
- Noyau et sa partie : une mise à jour remplace `~/assistant-kit/` et le code installé, jamais `~/.config/assistant-kit/`,
  `var/`, `config/settings.yaml`, `config/dashboard.yaml`, `~/notes/`, `~/ultron/` ni ses fichiers de `~/.claude/` (table dans `kit/CLAUDE.md`).
- Tests : `tests/test_kit_setup.py` (exclu du kit : il lit `kit/`).

## Mac (todo #390)

- `src/ultron_hub/common/plateforme.py` (bibliothèque standard) : `platform.system()` choisit systemd utilisateur (Linux) ou
  launchd (`Darwin`). Services de la v1 seulement : `dashboard` (`ultron-hub-dashboard.service` / label
  `ultron-hub.dashboard`) et `sante` (`ultron-hub-health.timer` / `ultron-hub.health`). Opérations `etat`, `demarrer`,
  `arreter`, `redemarrer`, `installer` ; Mac : agents dans `~/Library/LaunchAgents/<label>.plist`, domaine `gui/<uid>`
  (`bootstrap`, `bootout`, `kickstart [-k]`, `print`) ; état `active` (running), `waiting` (chargé, au repos), `failed`
  (dernier code de sortie ≠ 0), `inactive` (non chargé). `installer` copie le gabarit rempli s'il est absent (jamais
  d'écrasement) puis charge (Mac) ou `daemon-reload` + `enable --now` (Linux). En ligne :
  `python -m ultron_hub.common.plateforme <op> [service] [--gabarits $R]`, une ligne JSON, code 0 si réussi.
- Branché là où la v1 l'utilise, Linux inchangé (même argv) : `dashboard cert --redemarrer` (`kickstart -k`),
  `health collect` (sonde `services` : états launchd des deux agents, sous leur nom d'unité systemd),
  `repos.run_tests` (commit séparé, fichier SENSITIVE : sur Mac les tests tournent en clair, sans bwrap).
- Gabarits `kit/gabarits/launchd/` : dashboard (`RunAtLoad`, `KeepAlive` `SuccessfulExit: false` + `ThrottleInterval`
  10 s ≈ `Restart=on-failure` ; pas d'équivalent de `RestartPreventExitStatus=78`, launchd relance au plus toutes les 10 s),
  santé (`StartCalendarInterval` à h00 et h30, rattrapé une fois au réveil). Journaux dans `~/Library/Logs/`.
  Marqueurs existants seulement (`{{HOME}}`, `{{NOM_ASSISTANT}}`).
- Mac qui dort = dashboard injoignable ; pas de `enable-linger`.
- Hors v1, non portés (appellent encore `systemctl`/`systemd-run`) : `branches.py`, `branch_lot.py`, `auto_merge.py`
  (redémarrages après fusion : échec signalé, la fusion passe), `after_merge.py`, `bot/run.py`, `backup/heartbeat.py`,
  `escouade/*`, `missions/*`, `executors/claude_cli.py`, `health/reflexes.py` (lancé par `health alertes`, rend « inspection
  impossible »), `dashboard/apercu.py` (textes d'exemple).
- Tests : `tests/test_plateforme.py` (faux `launchctl` en tête de PATH, `platform.system` forcé), `tests/test_kit_setup.py`.

## Essai à blanc (todo #391)
- `kit/setup/essai_blanc.sh <linux|mac> <kit> <travail>` rejoue `etapes.md` dans un HOME jetable puis `verifier.sh` : faux
  `claude`, `tmux`, `systemctl`, `launchctl` en tête de PATH, jamais le vrai HOME ni les vrais services. `mac` force
  `platform.system()` à Darwin (`sitecustomize`) et `KIT_OS=Darwin`. Dépendances : `.pth` vers les paquets du Python d'essai
  (`ESSAI_PYTHON`) au lieu de `pip install` (hors ligne). `ESSAI_PYTEST=1` lance aussi les tests du code installé.
- `tests/test_kit_blanc.py` (exclu du kit) : export du `HEAD` + les deux variantes, aucun KO attendu. Il lit git : il voit les
  commits, pas l'arbre de travail.
- Tests du kit exporté : `tests/conftest.py` ignore (`collect_ignore`) ceux qui importent `hermes_plugin` ou `user_actions`
  (et ceux qui les importent), saute (`KIT_SKIPS`) quelques tests qui lisent un fichier non exporté ; `config/kit.yaml` exclut
  les tests de fichiers non exportés. `deploy/git-hooks/*` est exclu (flux de branches `todo-<id>/` propre à l'original).
- Santé sans `/proc` (Mac) : `probe_system` / `probe_memory` rendent des valeurs `null` (inconnu), jamais d'exception.
- Dashboard launchd sans config : `dashboard serve --sans-config-code-0` sort en code 0 (launchd relance sur code ≠ 0 et ne
  sait pas exclure 78) ; le gabarit l'utilise, systemd garde le code 78.
