# Objectifs (todo #496)

Un objectif vit hors session : un fichier `var/objectifs/<id>.json` (id entier croissant, écriture atomique
fichier temporaire puis `os.replace`, droits 0600, verrou `.verrou`). Une session coupée relit l'objet et reprend.
Code : `src/ultron_hub/objectif.py` ; tests : `tests/test_objectif.py`. Pas de suppression.

Champs : `id`, `but` (non vide, 2000 caractères max), `critere_fin` (liste de commandes texte, peut être vide ; elles
sont exécutées par `verifier`, todo #497), `budget` et `chantier` (optionnels), `etape` (défaut « cadrage »), `statut`
(défaut « ouvert »), `preuves` (liste de `{le, texte}`), `cree_le`, `maj_le` (ISO).

- `ultron-hub objectif add --but "…" [--critere "cmd"]... [--budget …] [--chantier …]`
- `ultron-hub objectif show <id>` ; `ultron-hub objectif list [--statut …]`
- `ultron-hub objectif etape <id> --etape "…"` ; `ultron-hub objectif preuve <id> --texte "…"` (horodatée)

Sortie JSON `{status, command, summary, data, error}` ; id inconnu ou but invalide = `status: error`, code 1.

## Vérifier (todo #497)

`ultron-hub objectif verifier <id> [--dossier <chemin absolu>] [--timeout <s>]` exécute chaque commande de
`critere_fin` (`shlex.split`, jamais de shell) dans le bac à sable `repos.sandbox_argv` (sans réseau, home vide),
dossier de travail = `--dossier` (défaut : racine du projet), timeout par commande (défaut 120 s), sortie tronquée à
4000 caractères. Toutes à rc 0 = `status: ok` ; sinon `status: ko` (code 1). `data = {ok, resultats: [{commande, rc,
sortie, duree}]}` ; rc `null` = timeout, lancement impossible ou commande illisible (KO). Liste vide = KO « contrat vide ».
Une preuve horodatée est ajoutée (« verifier : OK 3/3 » ou « verifier : KO, 2/3 en échec, dont <première commande en échec> »).
Dossier refusé (`status: error`) : non absolu, introuvable, `/`, le home, ou lié aux chemins interdits du registre.

## Frein Stop (todo #498)

`ultron-hub objectif lier <id> --session <nom>` lie l'objectif à une session tmux (`[A-Za-z0-9_.:-]{1,64}` ; `--session ""`
délie ; un nom déjà lié à un autre objectif ouvert = refus). `ultron-hub objectif stop-hook` est le hook Stop de Claude Code
(branché par `claude_hooks_sync`) : il lit le JSON du hook sur stdin (ignoré, aucun argv), trouve la session via
`tmux display-message -p '#S'` (hors tmux ou échec = arrêt permis) et ne lance jamais `verifier`. Arrêt permis si aucun objectif
ouvert n'est lié, ou si la dernière preuve `verifier : …` est « OK » et postérieure au dernier changement d'étape. Sinon
`{"decision": "block", "reason": …}` avec la preuve « frein : relance N/3 » (`relances_stop`), 3 relances au plus, puis preuve
« frein : arrêt permis après 3 relances » et arrêt permis. Une preuve OK remet le compteur à 0. Toute exception = arrêt permis
(log sur stderr). Exception documentée à la règle « stdout = enveloppe JSON » : le stdout est le JSON du hook, ou vide.

## Reprise (todo #501)

`ultron-hub objectif reprendre [--essai]` relance, dans une nouvelle session, chaque objectif `ouvert` lié à une session tmux
qui est « coupée » (`tmux has-session -t =<nom>` en échec) ou « quota » (l'écran, `tmux capture-pane -p`, contient
« usage limit », « limit reached » ou « resets »). Code : `src/ultron_hub/reprise.py` ; tests : `tests/test_objectif_reprise.py`.

- Condition : le quota est revenu, c'est-à-dire `ultron-hub usage` non périmé (`stale` faux) et `session.percent` < 95. Sinon rien
  n'est lancé ni écrit (pas de preuve répétée). Usage illisible = pas de reprise.
- Commande (liste d'argv, jamais de shell, binaires en chemins absolus) : `~/.local/bin/ultron [--chantier <c>] "Reprends l'objectif
  <id> (ultron-hub objectif show <id>) à l'étape « <etape> »."`. Le chantier est celui de l'objectif s'il existe dans `~/notes/prompt`
  (la forme `--chantier` est la seule qui accepte chantier + consigne). Lancée sans terminal ni `TMUX` : la session est créée
  détachée (`Session tmux : <nom>` lu sur la sortie), l'attache finale échoue sans gravité.
- Ensuite l'objectif est lié à la nouvelle session et reçoit la preuve « reprise : <coupée|quota> -> <session> ». Cause « quota » :
  l'ancienne session n'est jamais fermée, seulement déliée. Lancement sans nouvelle session = preuve « reprise : échec du lancement ».
- Garde-fous : 3 reprises au plus par objectif sur 24 h (échecs compris), 30 min mini entre deux, comptées sur les preuves « reprise : ».
- Session fermée par l'utilisateur (`session fermer`, après vérification) : l'objectif ouvert qui lui est lié est délié et reçoit la
  preuve « fermée : par l'utilisateur (<session>) » (pas « reprise : », hors plafond) ; la reprise l'ignore (todo #538).
- `--essai` ne lance rien : `data.plan` donne objectif, cause, commande exacte et action (`reprise` ou `rien` + raison).
- Appel automatique : `handle_heartbeat` (`backup/commands.py`, le heartbeat de 5 min) appelle `reprise.reprise_auto` une fois par
  passage ; toute exception est écrite sur stderr et n'arrête jamais le heartbeat. `deploy/` n'est pas touché.

## Routines (todo #503)

Une routine = une phrase de l'utilisateur rendue répétable. Fichier JSON `~/ultron/routines/proposees/<nom>.json`, déplacé dans
`actives/<nom>.json` après le ✅ signé, ou dans `refusees/` (suffixe `-2`, `-3`… si le nom y est pris) sur ❌ : rien n'est supprimé.
**Rien ne lance une routine ici** (le lanceur à l'heure est une autre todo). Code : `src/ultron_hub/routines.py` ; tests :
`tests/test_routines.py` (la racine est remplacée par une fixture, aucune variable d'environnement).
Todo #539 : `ultron-hub routine retirer --nom N` déplace une ACTIVE dans `retirees/` (même schéma que `refuser` : verrou, nom libre
`-2`, `-3`, journal), non signé. Le lanceur n'en relance pas une dont la dernière session est morte moins de 2 min après le
lancement (objectif encore ouvert, jamais vue vivante 2 min après) : preuve notée dans l'objectif, état `arret` dans
`lancements.json`, levé par le retrait ou un nouveau ✅ (nouveau `active_le`).

Champs : `nom` (`[a-z0-9][a-z0-9-]{0,39}`), `phrase`, `cadence`, `but` (espaces normalisés, 400 caractères au plus),
`critere_fin` (1 à 3 commandes d'une ligne, comme `objectif add --critere`), `plafond` (points de quota par lancement, 1 à 20),
`chantier` (`[a-z0-9][a-z0-9-]{0,39}`), `cree_le` ; une active ajoute `active_le` et `active_par`.

Cadence, format fermé (`fullmatch`, toute autre forme refusée) : « toutes les <N> h » (N ≥ 1, sans zéro de tête : récurrence
minimale 1 h), « chaque jour <HH:MM> », « chaque <lundi..dimanche> <HH:MM> ». 50 routines ACTIVES au plus (la 51e est refusée).

- `ultron-hub routine propose --nom … --phrase … --cadence … --but … --critere … [--critere …] --plafond N --chantier …` :
  écrit la proposée (refus si le nom existe en proposée ou active), stdout JSON.
- `ultron-hub routine liste` (lecture seule) : `{proposees: […], actives: […]}`, chacune avec `statut`, `empreinte` (16 hex du
  SHA-256 du JSON canonique du fichier : clés triées, sans espaces), `prochain` (prochaine échéance, heure locale ; « toutes
  les N h » compte depuis `active_le`, ou depuis maintenant pour une proposée ; `null` si la cadence du fichier est invalide)
  et `lignes` (ce que montre la feuille du ✅). `routine voir --nom N` : une routine (proposée d'abord, sinon active).
- `ultron-hub routine activer --nom N --empreinte E` : jeton `routine` signé sur `<N>:<E>` (ou `--i-am-human`), sous verrou
  `var/routines/lock` ; refus (rien activé) si empreinte différente, champs revalidés invalides (fichier retouché à la main),
  active du même nom ou 50 actives. Anti-rejeu : `var/routines/activees.jsonl` (preuve dépensée avant le déplacement).
- `ultron-hub routine refuser --nom N` (simple, non signé) : déplacée dans `refusees/`.
- Dashboard : catalogue `routines`, `routine-voir` (lecture), `routine` (signée, `detail = routine-voir` : la feuille liste
  les `lignes` telles quelles), `routine-refuser` (simple) ; carte « 🔁 routine proposée » dans À toi, juste sous les habitudes
  (`docs/dashboard.md`).

## Lanceur (todo #509)

`ultron-hub routine lancer [--essai]` lance les routines de `actives/` dont l'échéance est passée ; le heartbeat de 5 min
l'appelle (`routines_lanceur.lanceur_auto`, juste après `reprise_auto`, même `try/except` : une exception n'arrête jamais le
heartbeat). Code : `src/ultron_hub/routines_lanceur.py` ; tests : `tests/test_routines_lanceur.py`.

- Échéance : calculée depuis le dernier lancement (ou `active_le` si jamais lancée) avec `routines.prochain`.
- Lancement : crée un objectif (`but`, `critere_fin`, budget « <plafond> points », `chantier`, preuve « routine : <nom> »), puis
  `~/.local/bin/ultron --chantier <c> "Objectif <id> (ultron-hub objectif show <id>), routine <nom> : <but>"` (comme la reprise,
  sans terminal), et lie l'objectif à la nouvelle session.
- État : `var/routines/lancements.json` (`dernier`, `objectif`, `session` par routine), écrit sous le verrou `var/routines/lock`
  AVANT le lancement : si le heartbeat repasse, pas de double lancement (un lancement raté n'est pas rejoué, il laisse une preuve).
- Garde-fous : quota revenu (usage non périmé et `session.percent` < 95, sinon rien et rien d'écrit) ; 1 lancement au plus par
  passage ; pas de relance tant que la session précédente existe (`tmux has-session`) ; chantier inconnu dans `~/notes/prompt` =
  rien ; plusieurs échéances ratées = un seul lancement, la suivante repart du lancement.
- `--essai` : `data.plan` = routine, échéance, commande exacte (`<id>` à la place de l'objectif à créer), action (`lancement` ou
  `rien` + raison) ; ne lance ni n'écrit rien.

### Porte sans IA (todo #510)

Une routine peut porter une `porte` : `{"fichiers": ["/chemin/absolu", ...]}` (1 à 10 fichiers, sous `$HOME`, jamais `~/.ssh`,
`~/.hermes`, `~/.config/gh`, `~/.claude`, `~/.sudo_pass` ni `auth.json`). Aucun shell, aucune commande : seulement de la lecture
(`os.stat`). Elle est validée dans `routines.py`, proposée par `routine propose --porte-fichier <chemin>` (répétable), comprise
dans l'empreinte (une porte changée = nouveau ✅) et montrée dans `lignes` : « Porte : lance seulement si changé : … ».

- Empreinte de la porte : SHA-256 sur les chemins triés, chacun avec (taille, mtime_ns) ou « absent ». Elle est écrite dans
  `var/routines/lancements.json` (`porte`) au lancement.
- Routine échue dont l'empreinte est identique à celle du dernier lancement : action `saute`, raison « porte : rien n'a changé »,
  aucun objectif, aucune session, aucun appel à usage. Sous le verrou : `dernier` = maintenant (le prochain contrôle attend la
  cadence) et `evites` += 1. Premier passage (aucune empreinte connue) = lancement. Le résumé compte les sautées.
- `--essai` montre `saute` sans rien écrire. `routine liste` et `routine voir` donnent `evites` (0 par défaut) pour chaque active.

### Déclencheur sur évènement (todo #512)

Une routine peut porter un déclencheur `quand` au lieu d'une cadence fixe (sa cadence vaut alors « sur évènement ») :
`{"dossier": "/chemin/absolu"}` (un fichier nouveau ou modifié dans ce dossier, mêmes interdits que la porte : sous `$HOME`,
jamais `~/.ssh`, `~/.hermes`, `~/.config/gh`, `~/.claude`, `~/.sudo_pass` ni `auth.json`) ou `{"service": "<nom>.service"}`
(unité systemd utilisateur tombée ; nom validé par `fullmatch` strict, lu par `systemctl --user is-active <unité>` en liste
d'arguments, sans shell, injectable dans les tests). Un seul déclencheur, et pas de porte en plus. Proposé par
`routine propose --quand-dossier <chemin>` ou `--quand-service <unité>` (sans `--cadence`) ; compris dans l'empreinte (déclencheur
changé = nouveau ✅) et montré dans `lignes` (« Quand : … »).

- Le heartbeat (`lancer`) vérifie sans IA, avec les mêmes garde-fous qu'une échéance : quota, une session précédente encore
  vivante = pas de double lancement, un lancement par passage, chantier connu.
- Empreinte d'un dossier : SHA-256 sur les fichiers réguliers (nom, taille, mtime_ns), lecture seule, 500 entrées au plus ; dossier
  vide ou absent = rien. Pour un service, l'évènement est « tombé » (`inactive` ou `failed` ; état illisible = rien).
- Un évènement déjà vu (même empreinte, ou service resté tombé) ne relance pas : l'empreinte est écrite dans `lancements.json`
  (`evenement`) sous le verrou, avant le lancement. Si le service revient, elle est effacée : une nouvelle chute relance.
  Si un garde-fou bloque (quota, session vivante), rien n'est écrit : l'évènement est retenté au passage suivant.
- `--essai` montre `lancement` avec `evenement` (texte de l'évènement) sans rien écrire ni lancer.

## Journal avant d'agir (todo #514)

Une action sensible faite pour un objectif écrit d'abord son intention, puis son résultat, dans la liste `actions`
(absente = vide) : `ultron-hub objectif intention <id> --texte "…" [--commande "…"]` rend `n` ;
`ultron-hub objectif resultat <id> --n N --statut ok|echec [--texte "…"]` le complète (`fini_le`). Ces commandes
n'exécutent rien, elles notent (textes de 500 caractères au plus, `n` inconnu refusé). En Python :
`with objectif.action(settings, id, texte, commande): …` écrit l'intention avant d'entrer et ok/echec en sortie ; si le
processus meurt au milieu, l'intention reste `en_cours`. `objectif show` marque « action interrompue » (et liste
`interrompues`) toute action `en_cours` depuis plus de 30 min ; la session liée n'est pas consultée. Tests : `tests/test_objectif_journal.py`.

## Historique du banc (todo #513)
Chaque passe du banc de 10 objectifs (chemin A') s'écrit en une ligne dans `var/banc/historique.jsonl` (JSON lines, ajout atomique).
- `ultron-hub banc noter --reussis N --sur 10 [--cout P] [--note "..."] [--date AAAA-MM-JJ]` : refuse hors `0 <= reussis <= sur <= 50`, note de plus de 200 caractères ou date invalide ; date par défaut = aujourd'hui.
- `ultron-hub banc historique` (lecture seule) : `data = {passes, texte}` (« 02/11 : 7/10 ; ... ») et `summary` = la tendance (« 7/10 puis 8/10 »).
- Dashboard : bouton « Historique du banc » (entrée `banc-historique`, type `lecture`).


### Mode et modèle (todo #517)

Une routine porte un `mode` qui fixe le modèle de la session que le lanceur ouvre. Table unique `routines.MODES` :
`tri` = haiku, `travail` = sonnet, `decider` = opus (défaut). Proposé par `routine propose --mode tri|travail|decider`, validé dans
`valider` (autre valeur refusée), stocké dans la routine, compris dans l'empreinte, relu à `activer` et montré dans `lignes`
(« Mode : tri (modèle haiku) ») sur la carte d'approbation. Un fichier sans champ `mode` (routine d'avant) vaut `decider`.
`routines_lanceur.commande()` ajoute `--modele <modèle>` : `ultron --modele haiku --chantier <c> "..."`. `deploy/bin/ultron` accepte
`--modele haiku|sonnet|opus` (liste fermée, refus clair sinon, opus par défaut) en tête, seul ou avant `--chantier` ; la valeur remplace
`--model opus` de la commande `claude`. Tests : `tests/test_routines.py`, `tests/test_routines_lanceur.py`, `tests/test_ultron_chantier.py`.
