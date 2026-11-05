# /video (menu : /analyser)

Référence détaillée, sortie de CLAUDE.md le 18/10/2026 (todo #167).

## Vidéo (/video, todos #56 à #94)
- **Todo #174** : la commande du menu Telegram s'appelle `/analyser` (« Analyser un lien ou un fichier ») et ouvre 📥 Analyser ; `/video` reste un alias silencieux (même écran, hors menu, `PLUGIN_COMMANDS` de `deploy/user/ultron-user`). Menu réenregistré après fusion par `apres-fusion/todo-174.json`.
- **Todo #173 revient sur « aucune question avant » de la #94** (décision D2, 18/10) : depuis 📥 Analyser, une confirmation précède la collecte (« Vidéo de 12 min, analyse Opus de quelques minutes. », ✅ Analyser / ✖ Quitter ; durée via `video probe`, « durée inconnue » si illisible). Un seul /video à la fois (`var/video/.lock`) reste vrai.
- **Un seul parcours** (todo #94, chantier /video neutre 4/4) : `/video` + lien → collecte complète (script) → 🔬 analyse
  (Opus ; par intention depuis la #441, voir plus bas) → compte rendu Telegram + `.md` → **ta réponse** (texte libre) → actions à valider (✅ une par une). Aucune question
  avant (plus d'écran « Pourquoi cette vidéo ? », plus de délai de 2 min) ; un mot avant le lien (« analyse ») est ignoré.
- Retirés par la #94 (les fiches déjà rangées restent) : boutons 💡 📚 💬 📝, tâches `video-{resume,inspiration,fiche,parler}`,
  `video/parler.py`, commandes `video run | summarize | reply | tags | link-todo`, collecteur `video` (`COLLECTORS`,
  `PARAM_COLLECTORS` : plus aucune tâche ne lit une vidéo par collecteur, `collect_video(url, video_id)` n'est appelée que par
  `video collect`), `FULL_KEYS`, refus des vidéos muettes. `video tags` et `link-todo` n'avaient plus qu'un appelant, les
  écrans retirés ; les nouvelles fiches (`video actions`) ne remplissent pas `todos`.
- Commandes (`video/commands.py`, chacune du JSON sur stdout) : `video collect <url>` -> `video analyse <id>` -> `video actions <id>`
  (réponse sur stdin). Mêmes règles qu'avant : un seul `/video` à la fois (`var/video/.lock`, non bloquant), journal commun
  = titre seulement, `--requested-by telegram:<chat>`.
- Outils de texte communs (`video/text.py`) : `clean_text` (sans caractère de contrôle, tiret cadratin ni phrase-consigne),
  `neutral` (`<` `>` neutralisés dans les données données à Claude), `clean_answer` + `MAX_ANSWER_CHARS` (2000, recopié par
  `video_flow.MAX_REPLY_CHARS`, test d'égalité), `cut`, `cut_words`, `cut_prose`, `clair_line`, `clean_tags`, `timed_link`,
  `todo_category`. Aucun module de la vidéo n'importe plus `commands` pour cela.
- Le skill Hermes `hermes-video` est lancé en sous-processus sans shell (`video/collect.py`), lit `report.json`, supprime
  `/tmp/watch-*` (`fullmatch` obligatoire avant tout `rmtree`). Les tests ne lancent jamais le vrai skill (`collect._run` simulé).
- Liste d'hôtes fermée, https, correspondance exacte : `video/links.py` ; le plugin en garde une copie
  (`hermes_plugin/video_flow.py`, pas d'`import ultron_hub`) et un test vérifie qu'elles restent identiques.
- La transcription, le titre, la description et le texte à l'écran sont des **données non fiables** (injection) : les prompts le
  disent, balises neutralisées, tâches sans outil sauf `Read` limité au dossier des images pour l'analyse.
- Plugin (`video_flow.py`) : le lien lance `video collect` puis `video analyse` dans le même appel (`_busy` tient le serveur
  jusqu'au bout, deux messages de progression), sans question. Pas de `Kept`, de `pending`, de minuteur ni de `push` : plus rien
  n'est envoyé hors d'une réponse à un message. Filtre de texte libre : `_AwaitingInterpretation` seulement (puis le lien).
  Commande `video` dans `PLUGIN_COMMANDS` ; description changée (« Analyser une vidéo YouTube ou TikTok »), menu déjà là :
  `deploy/user/apres-fusion/todo-94.json` (redémarrage de hermes-gateway pour afficher la description).

### Collecte complète (todo #91, chantier /video neutre)
- `video collect` lance le skill avec `--keep-video` (transcription + `video.info.json` + fichier) puis `video/media.py`
  (ffmpeg direct, sans IA) avant `cleanup()` : images (début, changements de scène `scene>0.3`, une toutes les 10 s,
  plafond 150, 768 px, JPEG 0600 dans `var/video/<id>/images/NNN_tSSSSS.jpg`) et son (`ebur128` + `silencedetect`).
- `collected.json` gagne : `metadata` (titre, auteur, `upload_date` AAAAMMJJ, description ≤ 5000, chapitres, vues…),
  `transcript_segments` [{start,end,text}], `audio` (`loudness_lufs`, `peak_dbfs`, `silences`, `sound_without_speech`,
  `volume_per_window`), `images_dir`, `frames` [{file,t,kind: start|scene|interval}], `full_warnings`.
  Vidéo sans parole : cas normal (`transcript` vide), de la collecte au compte rendu (la #94 a retiré les anciens
  modes qui la refusaient).
- Images (#476) : yt-dlp ne donne parfois que le format 18 (yuv420p plage limitée), que ffmpeg refuse d'encoder en JPEG.
  `media.extract_frames` ajoute `-pix_fmt yuvj420p` ; le skill externe (non modifiable) passe par le relais
  `video/shim/ffmpeg`, placé en tête du PATH du skill seul (`collect._env(shim=True)`), qui fait de même pour toute sortie
  .jpg/.jpeg sans `-pix_fmt`, puis exécute le vrai ffmpeg. Le réflexe de santé `video-skill` alerte sur « video skill failed » (24 h).

### Analyse neutre (todo #92, chantier /video neutre 2/4) : chronologie = annexe depuis la #441
- Depuis la todo #441 (section suivante), ce parcours neutre ne produit plus que la **chronologie** (lots d'images), rendue en
  **annexe** à la fin de `analyse.md` ; le compte rendu commence par la devinette et la grille. Le reste ci-dessous (lots,
  lecture limitée, nettoyage, trous) reste vrai.
- `ultron-hub video analyse <id> [--requested-by …]` (`video/analyse.py`, même verrou `var/video/.lock`) lit la collecte de la #91 :
  Claude Opus (`tasks/video-analyse/`, modèle fixé dans `task.yaml`, `tools: [Read]`) travaille **par lots** de 20 images
  (`BATCH_FRAMES`) avec la transcription et les mesures du son de la plage ; chaque lot rend `timeline` (`start`, `end`, `said`,
  `shown`, `sound`), `unreadable` et `overview` ; un dernier appel **sans image** rend `overview` + `digest` (8 à 10 lignes). Un
  seul schéma pour les deux étapes, l'étape est dite par `{etape}` (texte écrit par ultron-hub). Sans image (extraction ratée),
  lots de 180 s sans Read. Délai total 30 min (`DEADLINE_S`), plafond de budget par appel dans `task.yaml`.
- **Lecture limitée au dossier des images** : `task.yaml` garde `allowed_tools: []` et `add_dirs: []` (rien n'est lisible
  tant qu'un appelant ne limite pas), `run_job(..., read_dir=<var/video/<id>/images>)` appelle `registry.scope_reads` qui ajoute
  `--add-dir` + la règle `Read(//<dossier>/**)` (dossier validé comme un `add_dir`, `Read` obligatoire dans `tools`). Seuls les noms
  `NNN_tSSSSS.jpg` fichiers réguliers (pas de lien) sont donnés à Claude. Les appels sans image n'ont aucune permission de lecture.
- Données non fiables (transcription, titre, description, texte à l'écran) : balises avec `<` `>` neutralisés (`text.neutral`), le
  prompt le dit ; la sortie de Claude passe par `clean` (caractères de contrôle, tirets cadratins, phrases-consignes de
  `savoir.filter_instructions` retirées **et comptées** dans « ce que je n'ai pas pu lire »). Le son vient des mesures du script
  seulement (« son sans parole », jamais « musique » sans mention).
- Rien ne s'échappe en silence : images qu'aucune entrée ne couvre (`uncovered`), lot échoué (sa parole est gardée, ses images
  deviennent un trou), synthèse échouée (résumé tiré de la chronologie), avertissements de collecte, phrases écartées. Tous les lots
  échoués = erreur. Les horodatages du résumé sont ramenés à une vraie entrée de la chronologie.
- Sorties (0600) dans `var/video/<id>/` : `analyse.md` (pièce jointe : métadonnées, vue d'ensemble, moments clés et chronologie avec
  liens `&t=…s`, trous, mesures du son, description non vérifiée) et `analyse.json` (lu par `analyse.load_analysis`, point d'accroche
  de la #93). Message Telegram : titre, durée, vue d'ensemble, ≤ 10 moments, ligne des trous, ligne de la pièce jointe = 10 à 15 lignes.
  Journal commun : « Vidéo analysée : <titre> ». Durée maximale de la vidéo : 45 min (collecte, inchangée).
- Plugin : l'analyse est le parcours par défaut (plus de bouton `m:a` ni de mot nécessaire avant le lien, voir plus haut). Envoi par
  `Screen.documents` (nom du fichier conservé). **Point d'accroche (utilisé par la #93)** :
  `VideoFlow.interpreting` (chat -> id de la vidéo dont le compte rendu vient d'être envoyé), `interpretation_target(chat)` et
  `end_interpretation(chat)`. 🏠 et un nouveau lien effacent l'attente.
- Essai réel du 17/10 sur « Me at the zoo » (19 s, 2 images, 1 lot) : 37 s, 2 appels Opus ≈ 1,01 $ éq. API (non facturé), 5 trous
  déclarés (dont « aucune image entre 0:00 et 0:10 » : une image toutes les 10 s). Tests : `tests/test_video_analyse.py`, faux
  `claude` (arguments, lots, nettoyage, trous, échecs) et section « vidéo muette » de `tests/test_video.py`.

### Analyse par intention (todo #441, remplace le parcours neutre)
- Ultron **devine sans demander** ce que l'utilisateur cherche, dans l'ordre : chantier d'envoi (ou cité depuis Main), sa phrase,
  le sujet de la vidéo. Les lots d'images restent la chronologie neutre (inchangés) ; seule l'étape « synthèse » (sans image, sans
  outil, opus) reçoit en plus `{intention}` (`analyse.intention_text` : origine, session, chantier, phrase), `{chantiers}`
  (`known_chantiers` : `chantier.core.subjects` + `parse_prompt`, nom + une ligne, 40 au plus, 3000 car.) et `{ultron}`
  (`ultron_text` : frontmatter des `~/.claude/skills/*/SKILL.md` (`SKILLS_DIR`, remplacé dans les tests), « Carte des docs » du
  CLAUDE.md d'ultron-hub préfixée `ultron-hub/`, titres du carnet de savoir avec leur fichier ; chaque bloc borné, source illisible
  = « (illisible) »). Tous neutralisés (`text.neutral`) ; les lots reçoivent une chaîne vide.
- Schéma (un seul pour les deux étapes, tout requis) : `cas` (inspiration | info | methode | donnees, vide pour un lot), `devine`
  {phrase, pourquoi}, `inspiration` {sujet, analogie, comment[] (scindé par la #443, voir plus bas), non_montre[], face_ultron[{idee, etat: existe|en_partie|non,
  fichier}], pour_nous[{application, chantier, dossier_dabord}]}, `info` {faits[], opinions[]}, `methode[]`, `donnees[]`,
  `rangement[]` (fiche | todo, mêmes champs que `video-actions` + `chantier`). Le prompt remplit d'abord la grille du cas deviné,
  les autres si c'est utile ; « face à Ultron » cite un fichier de l'inventaire ou « non vérifié ».
- `clean_grid` : chaque texte par `clean` (phrases-consignes retirées et comptées), longueurs et nombres bornés, `cas` et `etat`
  vérifiés (idée à `etat` inconnu écartée), `chantier` hors de la liste connue vidé. Rangé dans `analyse.json` (`grille`).
  Synthèse échouée ou grille vide : repli d'avant (résumé tiré de la chronologie) + `empty_grid()` + un trou déclaré.
- **Rangement en un lot** (`file_batch`) : `rangement` (fiche et todo seulement, 5 au plus) passe par `proposals.build_actions`
  (nouveau paramètre `chantiers` : une todo dont le chantier est connu le porte, le ✅ fait `todo add --chantier`) ; sans phrase de
  l'utilisateur, pas de « Ce que tu en as dit ». `actions.json` est écrit comme par `propose` (`reponse` = la devinette), les ✅
  restent un par proposition (un ✅ unique pour le lot demanderait une action signée : non fait). Enveloppe de `video analyse` et
  `analyse-lien` : `data.cas`, `data.reponse`, `data.actions`.
- `analyse.md` : en-tête, « Ce que je pense que tu cherches », grille du cas deviné puis des autres cas remplis, « Ce que je
  propose de ranger », vue d'ensemble, moments clés, trous, puis **Annexe : chronologie**, annexe son, annexe description.
  Message (Telegram, dashboard) : titre, méta, 🎯 devinette, l'essentiel de la grille, ligne des propositions, trous, pièce
  jointe, « Ce n'est pas ça ? Réponds en une phrase » : 20 lignes au plus (`MESSAGE_MAX_LINES`) ; sans grille, l'ancien message.
- **Mauvaise devinette** : l'utilisateur corrige en une phrase par la réponse existante (`video actions`, stdin). `analysis_text`
  donne à Claude la devinette, le cas, la grille et ce qui a déjà été proposé ; le prompt de `video-actions` traite la
  correction (« non, c'était pour … ») comme une consigne suffisante et propose selon la bonne intention (actions.json remplacé).
- Dashboard : `video-analyse` et `video-tout` ont `sortie` `donnees` = `PROPOSITIONS` (`video-tout`, en fond, passe aussi par
  `_finish` : ça marche) ; `app.js` remplit `analyse.propositions` et `analyse.video` (pour 💡 Actions, la correction). Telegram
  (`video_flow`) n'affiche pas encore les propositions du lot : la réponse libre reste le chemin (non changé).
- Tests : `tests/test_video_analyse.py` (section « grid by intention »), `tests/test_video_actions.py` (correction),
  `tests/test_dashboard_catalogue.py`.

### Justesse sur tous les sujets (todo #443)
- Constat (essai réel du 27/10) : « comment il a fait » ne reprenait que ce qui est DIT ; le savoir-faire MONTRÉ restait noyé dans
  la chronologie (la synthèse n'a pas d'image). Règle : juste sur n'importe quel sujet, **aucune règle propre à un sujet** dans le
  prompt (test `test_the_prompt_has_no_rule_tied_to_one_subject`, seule la liste des valeurs de `projet` est tolérée).
- Lot : nouveau champ `technique` (≤ 12 par lot, `{t, text}`, instant ramené dans la plage, `clean_technique`) = ce que les images
  montrent du savoir-faire (interface et valeurs, réglages, gestes, outils visibles, mise en page, rythme des plans, transitions).
  Gardé dans `analyse.json` (`technique`, 120 au plus, au-delà = un trou déclaré), annexe « savoir-faire montré » de `analyse.md`,
  et donné à la synthèse dans `{technique}` (neutralisé, 15 000 car.).
- Grille : `inspiration.comment` devient `comment_dit` (liste) + `comment_montre` (`{t, text}`, instant ramené à une entrée de
  la chronologie ou du savoir-faire) ; nouveaux `ecarts` (écart dit/montré = point à vérifier), `a_verifier` (affirmations à
  vérifier en ligne), `cas_secondaires` (0 à 3 cas parmi les quatre, jamais le principal ; leurs grilles passent après lui). Message :
  « Il dit faire » / « On le voit faire » + ligne 🔎 du nombre de points à vérifier (toujours ≤ `MESSAGE_MAX_LINES`).
  **Compatibilité** : `grid_view` lit une ancienne grille (`comment` = `comment_dit`, champs manquants vides) pour `render_*` et
  `video actions` (`analysis_text`).
- Lentille : `lens_text(chantier)` lit la fiche du chantier d'envoi (`Fiche du chantier :` de son prompt, sinon
  `~/notes/chantier-<nom>.md`), section `## But` (sinon Objectif, Vision, Besoin, numérotation ignorée ; sinon le titre et
  l'introduction), nettoyée par `clean`, 1500 car., neutralisée, dans `{lentille}`. Fiche hors du dossier des notes (parent du
  dossier des prompts), lien symbolique, nom de chantier invalide, pas de chantier ou fiche illisible : « (aucune) ».
- Titre de todo : `text.cut_title` (utilisé par `proposals.build_actions`, 40 car.) coupe sur un mot entier et retire les petits
  mots pendants (« au », « de », « l' »…) ; `cut_words` reste tel quel pour les autres usages.

### Avis et allègement (todo #470)
- **Avis d'Ultron** : la synthèse rend `avis` {`credible` (preuves montrées ou non), `vente` (produit, formation, lien
  d'affiliation poussé), `applicable` (oui | en_partie | non), `pourquoi` (cite l'inventaire `{ultron}` si possible)} ; vide pour
  un lot. `clean_grid` le nettoie (400 car., `applicable` vérifié) ; `grid_view` donne des chaînes vides à une ancienne grille.
  `analyse.md` : section « Avis d'Ultron » juste après la grille du cas deviné ; message : une ligne ⚖️ (toujours ≤ 20 lignes).
- **Allègement** : les lots d'images passent en **sonnet** par la tâche `video-analyse-lot` (`TASK_LOT`), dont `prompt.md` et
  `output.schema.json` sont des liens vers `video-analyse` (un seul texte à tenir). La synthèse (sans image) reste en opus
  (`video-analyse`). Avant : 6 lots opus à 0,47-0,66 $ chacun (29/10). Mêmes permissions (`Read` limité par `read_dir`).
  Les tests qui copient `tasks/` copient les deux tâches.

### Réponse en actions (todo #93, chantier /video neutre 3/4)
- Après le compte rendu neutre, le **texte libre suivant du chat** (15 min, `REPLY_TTL_S`) = interprétation + consigne. Le plugin
  l'envoie sur **stdin** à `ultron-hub video actions <id> [--requested-by …]` (`video/actions.py`, verrou `var/video/.lock`,
  texte ≤ 2000 car., `text.clean_answer`). Tâche `tasks/video-actions/` : sonnet, `tools: []`, sans collecteur ; le prompt
  reçoit `{analyse}` (identité, vue d'ensemble, moments clés, trous, chronologie ≤ 40 000 car.), `{profil}` et `{reponse}`, les
  deux textes en **données** entre balises (`<` `>` neutralisés) : la vidéo ne donne jamais d'ordre, et rien n'est proposé que la
  consigne ne demande (consigne vague = `actions: []` + une phrase « ce que j'ai compris »).
- Quatre sortes d'actions (`fiche`, `todo`, `recherche` ajoutée par la #156, `profil` ; 5 au plus, un seul fait de profil ; logique dans `proposals.py`), **rien n'est fait par la commande** : `fiche` (la « note » à garder
  est une fiche : brouillon `var/video/<id>/actions/<n>.md` par `savoir.write_draft`, `but: retenir`, sections À retenir /
  Détail / Ce que tu en as dit, raccourci jusqu'à 3 Kio, moment ramené à une vraie entrée de la chronologie pour le lien `&t=`),
  `todo` (titre ≤ 40 car., notes autosuffisantes bâties par le code : texte de Claude + « Ce que tu en as dit » + source + lien,
  `clair`, catégorie par `todo_category`), `profil` (une phrase ≤ 300 car.). Tout texte de Claude passe par `text.clean_text`
  (sans tiret cadratin ni phrase-consigne). Résultat gardé dans `actions.json` (0600) ; journal : titre et nombre seulement.
- Plugin (`video_flow.py`) : `VideoFlow.interpreting` (+ `interpreting_until`) posé par `_reported`, filtre
  `_AwaitingInterpretation` (le premier texte libre après les réponses citées). Fin de l'attente : tout autre bouton ou commande (`stop_answer` via
  `moved_on`), 🏠, un message contenant un lien (nouvelle vidéo, il passe au traitement des liens), l'expiration, ou les
  propositions reçues. Échec du CLI, ou aucune proposition : l'attente reste ouverte, 15 min de nouveau (renvoyer une réponse plus précise). Un seul appel en vol par chat (`thinking`).
- Liste `hub:vid:ax:<id>` (une ligne et un état par proposition, un bouton par proposition ouverte) → carte `ui.approval_card`
  (`axs:<id>:<n>`) → ✅ `axok` / ✖ `axno` (écarte). ✅ fiche = `savoir verifier` à l'ouverture de la carte puis
  `signer("savoir", empreinte vue)` + `savoir ranger` (jamais sans carte ouverte) ; ✅ todo = `todo add --source video:<id>` (comme
  ➕ Todo, non signé) ; ✅ profil = `profil propose … --source video:<id>` (la validation reste dans /profil). L'élément est
  réservé (`busy`) avant l'appel, rouvert si l'appel échoue. Aucune clôture de todo ici. Pas de menu ni de commande nouveaux.
- Tests : `tests/test_video_actions.py` (tâche, données neutralisées, nettoyage, brouillons, erreurs) et section « la réponse
  devient des actions » de `tests/test_video.py` (flux, signature, attente).
- **✅ Tout ranger (todo #442, `video/lot.py`)** : toutes les propositions encore en attente d'une vidéo (celles de `actions.json`,
  lot de `video analyse` ou réponse `video actions`) rangées d'UN appui signé. `video lot <id>` (lecture seule, sans IA) : en attente =
  fiche dont le brouillon est valide et dont l'empreinte n'est pas dans `rangements.jsonl` du carnet ; todo ou recherche sans todo de
  source `video:<id>` du même titre (tout statut) ; fait sans proposition de profil de source `video:<id en minuscules>` (règle des
  sources du profil) au même texte. `actions` (+ `texte` d'une ligne), `faites`, `ignorees` (brouillon illisible + raison) ;
  `empreinte` = 16 hex du SHA-256 de l'id puis d'une ligne JSON par proposition en attente (la fiche par l'empreinte ACTUELLE de son
  brouillon), null si rien n'attend.
- `video ranger-lot <id> --empreinte X [--i-am-human]` : verrou `var/video/<id>/lot.lock` (pas celui des analyses), recalcule (empreinte
  différente, proposition rangée entre-temps, brouillon modifié = refus, rien fait), exige le jeton `video-lot` signé sur
  `<id>:<empreinte>`, jamais rejoué (preuves dans `var/video/lots.jsonl`, écrites AVANT de ranger). Puis chaque proposition par le
  code de son ✅ unitaire : `savoir.rank` sur l'empreinte vérifiée (trace = `by` du signataire + `via: video-lot`, `lot_proof`,
  `empreinte_lot` ; pas de `proof` : une seule preuve pour plusieurs fiches), `todo add --source video:<id>` (mêmes titre ≤ 40, notes,
  clair, catégorie, chantier que la carte), `profil propose` (validé ensuite dans Profil). Chaque proposition est tentée ; bilan d'une
  ligne chacune (`📥 Propositions rangées : n/N`), erreur si une a échoué.
- Catalogue : `video-lot-voir` (lecture, liste blanche `VIDEO_LOT`) et `video-lot` (signée, cible `{id}:{empreinte}`, `detail` =
  `video-lot-voir` : la feuille liste exactement ce qui sera rangé). Bouton de la page (todo #477, `rangerLot` de `dashboard/static/app.js`, section
  Propositions d'Analyser) : lit `video-lot-voir`, affiche le nombre en attente puis `form("video-lot", {id, empreinte})` ; rien en attente =
  « Rien d'autre à ranger ». Remplace la ligne « à valider sur Telegram » (bot de secours, #463). Tests : `tests/test_video_lot.py`.

### Chantier d'envoi (todo #440)
- `video collect` et `video analyse-lien` gagnent `--session <nom>` (facultatif, regex `sessions.journal.NAME_RE` comme le dashboard ;
  refus = enveloppe `error`) et `--phrase-stdin` (phrase facultative de l'utilisateur sur stdin, nettoyée comme `text.clean_answer`,
  vide = pas de phrase, 500 caractères au plus, plus = refus avant tout téléchargement).
- `collected.json` garde toujours `intention` = `{session, chantier, phrase}` (`None` si rien) ; le chantier vient de
  `journal.chantier_of(session)` (`None` pour Main). `analyse.json` la recopie (une vieille collecte sans la clé donne les `None`) et
  `analyse.md` ajoute « - Envoyée depuis : chantier X (session S) | Main | inconnu » et « - Ta phrase : … » (neutralisée).
- La phrase est une donnée : jamais dans le journal commun ni les logs (sa longueur seulement). La synthèse de `video-analyse` la lit
  depuis la todo #441 (neutralisée).
- Dashboard : `video-tout` et `video-collecte` ont un champ contexte `session` (rempli par la page quand elle est ouverte depuis une
  session) et un champ « Ta phrase (facultatif) » envoyé sur stdin.

### Tests et historique (todo #94)
- Tests : `tests/test_video.py` (collecte, CLI, parcours Telegram, filtres de texte libre avec de faux modules telegram, vidéo
  muette de la collecte au compte rendu), `test_video_analyse.py`, `test_video_actions.py`, `test_video_media.py`.
- Ancien code et décisions des modes retirés : historique git (branches `todo-64` à `todo-67`) et `ETAT.md`.
