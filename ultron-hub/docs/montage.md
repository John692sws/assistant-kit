# Montage vidéo

Référence détaillée, sortie de CLAUDE.md le 18/10/2026 (todo #167).

## Montage vidéo (todos #73 à #75)
- But (todo #69) : une voix devient une vidéo verticale animée par du code, 100 % pilotable depuis Telegram.
  #73 = la chaîne en CLI (`montage/`), #74 = le flux Telegram, #75 = les retouches.
- CLI (`montage/commands.py`) : `montage prepare <audio>` (→ `data.id` + aperçu), `montage render <id>`,
  `montage make <audio>` (les deux). Verrou unique `var/montage/.lock` (non bloquant) ; id validé
  (`store.MONTAGE_ID_RE`) avant tout chemin ; dossier `var/montage/<id>/` 0700, fichiers 0600 : `voice.wav`
  (voix nettoyée), `words.json`, `plan.json`, `props.json`, `montage.mp4`. Journal : « Montage vidéo rendu : <titre> »,
  jamais les mots.
- Sous-processus : toujours `montage/tools.run` (argv, binaires absolus `~/.local/bin/ffmpeg|ffprobe`, `/usr/bin/node`,
  `/usr/bin/python3`, env minimal, groupe tué au délai). Les tests remplacent `tools.run` : jamais de vrai ffmpeg,
  Whisper, claude ni Remotion dans la suite.
- Audio (`audio.py`) : fichier régulier (pas de lien), ≤ 25 Mo, ≤ 120 s, piste son ; WAV mono 48 kHz ; silences
  > 0,7 s à −35 dB ramenés à 0,3 s ; ≤ 60 s après nettoyage (8 Go de RAM). La transcription se fait APRÈS la coupe :
  aucun horodatage à recalculer. Les ratés (mots répétés) ne sont pas retirés (limite connue).
- Mots horodatés (`transcribe.py` → `whisper_words.py`, script autonome lancé par `/usr/bin/python3`, sans
  `ultron_hub`) : il importe `watch.config.load_api_key` du skill `hermes-video` (`<skill>/src`), qui choisit le moteur
  (local faster-whisper `word_timestamps=True`, ou API Groq/OpenAI `timestamp_granularities[]=word`) et lit la clé
  lui-même ; seule l'erreur générique sort, jamais la clé. `merge_words` recolle ponctuation et morceaux (« 'hui, »).
- Plan (`plan.py`, tâche `tasks/montage-plan/`, sonnet, `tools: []`, collecteur `montage` dans `PARAM_COLLECTORS`) :
  Claude reçoit les mots numérotés (`metrics`, données non fiables) et rend `titre` + 3 à 14 scènes (10 avant la #436) `{mot, type,
  texte, points, emotion, style}` dans le **catalogue fermé** `SCENE_TYPES` (titre, mot_cle, phrase, chiffre, liste,
  emotion, formes). Il ne donne JAMAIS d'heure : `mot` = index du mot déclencheur, l'heure est celle du vrai mot
  (−0,1 s). `build_plan` écarte tout ce qui sort du catalogue, corrige (chiffre sans nombre → mot_cle, liste sans
  points → phrase, émotion « aucune » → mot_cle), espace les scènes d'au moins 1 s, (plus de borne de 8 s depuis la #436), passe
  chaque texte par `clean` (`savoir.filter_instructions`, sans tiret cadratin) ; < 3 scènes = erreur claire.
- Catalogue = trois copies tenues d'accord par `tests/test_montage.py` : `plan.SCENE_TYPES/EMOTIONS/STYLES/THEMES/THEME_LABELS`,
  `output.schema.json`, et le React (`CATALOGUE` de `scenes.jsx`, `THEMES` de `themes.js`, visages de `Faces.jsx`).
  **Ajouter un type de scène** = un composant dans `scenes.jsx` + `CATALOGUE`, l'`enum` du schéma, `SCENE_TYPES` +
  `TYPE_LABELS`, une ligne dans le prompt. Claude n'écrit jamais de code : le React est dans le dépôt.
- Rendu (`render.py`) : `node node_modules/@remotion/cli/remotion-cli.js render src/index.jsx Montage <out>
  --props=props.json --public-dir=var/montage/<id> --concurrency=2`, cwd `montage/remotion`, 15 min au plus, puis
  `check_mp4` (ffprobe : 1080x1920 H.264, AAC, durée à 0,5 s près ; > 49 Mo signalé `too_big_for_telegram`).
  Remotion 4.0.531 + React 18.3.1 + polices `@fontsource` (pas d'emoji : le serveur n'a que DejaVu, les émotions
  sont dessinées en SVG). Aucun appel réseau dans `src/` (test).
- Installation : `node_modules/` est ignoré ; après 🔀 de la #73, action `montage_install` d'`ultron-user` (sans
  argument : `npm ci --ignore-scripts` + `remotion browser ensure`, dans `montage/remotion` du dépôt). Sans elle,
  `render` répond « Remotion n'est pas installé… ».
- Vérifié le 14/10 : Node 18.19.1 suffit à Remotion 4.0.531 ; le Chrome Headless Shell de Remotion (149) démarre sans
  bibliothèque système manquante ; essai réel (voix de synthèse 40,7 s → 25,9 s après coupe, 67 mots, 7 scènes) :
  préparation 20 s (Whisper local 11 s), rendu 78 s, MP4 7,7 Mo, pic de mémoire ≈ 2 Go (somme des processus).

### /montage sur Telegram (todo #74)
- CLI ajoutée : `montage fix <id>` (texte corrigé sur **stdin**, jamais argv ; `plan.realign` le recale sur les
  horodatages de la voix : mots inchangés = même heure, mots remplacés ou ajoutés = temps du passage partagé ; texte
  trop différent (`FIX_MIN_RATIO` 0,3), vide ou > 2000 car. = refus sans rien changer ; puis nouveau plan, l'ancienne
  vidéo est effacée) et `montage keep <id>` (📚 : `meta.json` `kept`, jamais purgé ; journal « Montage vidéo gardé »).
- Plugin (`montage_flow.py`, callbacks `hub:mtg:<verbe>[:<id>]`) : `/montage` (ou porte 🎙 Montage de l'accueil,
  `hub:mtg:open`) → attente d'une note vocale 10 min. Filtre `_AwaitingVoice` (`filters.VOICE | filters.AUDIO`) : hors
  attente, une note vocale va à Hermes comme avant. La note (≤ 120 s, ≤ 20 Mo, extension tirée d'une liste fermée de
  types MIME) est téléchargée par le plugin (`get_file` + `download_to_drive`) dans `tempfile.mkdtemp` (0700) supprimé
  juste après `montage prepare`. Aperçu = `ui.approval_card` (texte nettoyé + une ligne par scène, risque 🟢, rendu
  ≈ durée × 3,5) avec `[✏️ Corriger le texte]` (`fix`, attente de texte 10 min, filtre `_AwaitingMontageText` placé
  juste après `_AwaitingTalk`, arrêtée par tout autre bouton ou commande via `moved_on`, et par 🏠). ✅ (`ok:<id>`,
  pas de jeton signé : rien n'est publié ni effacé) → `montage render` → `Screen.videos` : le plugin envoie le MP4 par
  `reply_video` (1080x1920, `supports_streaming`, délais 300 s), sinon en document, sinon le chemin ; > 49 Mo = chemin
  seulement. Puis `[📚 Garder]` (`keep`) `[🔁 Autre montage]`. Seul l'id de la carte affichée est accepté (carte
  périmée = « plus en attente »). Un seul montage à la fois (`_busy` + verrou CLI), un appel en vol par chat.
- Commande : `montage` dans `PLUGIN_COMMANDS` ; après 🔀 de la #74 : `todo-74.json` (`hermes_menu_add montage`,
  `user_unit_restart hermes-gateway`). Hermes charge le plugin depuis main : aucun essai réel avant 🔀.
- Tests : `tests/test_montage_flow.py` (écrans, attentes, faux modules telegram, envoi vidéo et repli document) ; le
  robot de `tests/test_ui.py` envoie aussi une « note vocale » (étape `voice`). Sonde du 14/10 avec le vrai
  python-telegram-bot 22.8 de Hermes (bot sans réseau, méthodes d'API remplacées) : note vocale et audio pris
  seulement après /montage, texte libre non pris, carte, rendu, `send_video` avec les bons arguments.

### Styles (todo #434)
- Le style se choisit **avant** le montage : `montage prepare|make <audio> --style STYLE` ou `prepare|make --script --style
  STYLE` (`choices = plan.THEMES`, défaut `noir`, hors liste = refus). Il est écrit dans `plan.json` dès la préparation ;
  l'aperçu le donne (`data.theme` + `data.theme_label`). `fix` et `photos` gardent le style déjà choisi.
- Un style est un **univers**, pas une palette : fond, couleurs, police, façon d'afficher les sous-titres.

| Style | Fond | Police | Sous-titres (`captions`) | Accent |
|---|---|---|---|---|
| `noir` | noir uni | Montserrat 800 blanc | `mot` : centrés, chaque mot entre flou et gris puis net et blanc, blocs de 10 mots au plus remplacés d'un coup | vert citron `#C8FF6B` |
| `canevas` | crème `#F4EFE6`, carte blanche arrondie sous les scènes | Montserrat 800 noir | `pastille` : pastille noire arrondie sous la carte, 3 mots, mot courant en jaune `#FFC93C` | orange `#F28C28` |
| `papier` | gris-beige `#E6E0D4` avec grain (bruit SVG) | Bebas Neue, MAJUSCULES | `surligne` : en bas, blanc à contour sombre, phrase de 9 mots au plus, mot courant dans un rectangle rose `#E8467C` | bleu-vert `#1F6B6B` + rose |

- Copies tenues d'accord par les tests : `plan.THEMES` / `THEME_LABELS`, `montage/remotion/src/themes.js` (clés et champ
  `captions`), `THEMES` de `hermes_plugin/montage_flow.py`, choix 🎨 du dashboard (`catalogue.RETOUCHES`).
- Un ancien plan (`nuit`, `neon`) se lit comme `noir` (`plan.LEGACY_THEMES`, `plan.normalized`) : il se rend et se retouche
  toujours. Les scènes (`scenes.jsx`) lisent la palette du style ; leurs visuels propres à chaque univers : voir « Visuels animés (todo #436) ».
- Aucune nouvelle dépendance : Montserrat et Bebas Neue étaient déjà installées. La pastille du canevas est posée juste
  sous la carte (≈ 73 % de la hauteur), la carte occupant la zone des scènes.


### Fin de vidéo (todo #438)
- La fin se choisit **au lancement** : `montage prepare|make ... --fin abonne|suite|rien` (`choices = plan.FINS`, défaut `abonne`,
  hors liste = refus). Ce n'est pas Claude qui la choisit (ni schéma, ni prompt de `montage-plan`). Écrite dans `plan.json`
  (champ `fin`), donnée par l'aperçu (`data.fin` + `data.fin_label`, `duration_s` reste la durée de la voix). `fix`, `photos`
  et `retouch` gardent la fin déjà choisie (`commands.current_fin`) ; un ancien plan sans `fin` se lit `rien` (`plan.normalized`).
- Durée : la vidéo dure la voix + `plan.FIN_S` (2,5 s) pour `abonne` et `suite`, la voix + `plan.FADE_S` (0,6 s) pour `rien`
  (`plan.total_duration`, utilisée par `check_mp4`). Les props donnent `fin`, `voiceS`, `finS` ; `durationInFrames` est le total.
- `abonne` : « ABONNE-TOI » (ABONNE à la couleur d'accent, trait qui se dessine puis reflet) + « pour la suite ». `suite` : « LA
  SUITE » + flèche SVG qui va et vient + « dans la prochaine vidéo ». Composant `Fin.jsx` : même police, palette et cadre
  que le style (carte numérotée « FIN » du papier, carte du canevas où la caméra glisse, noir nu), entrée en 330 ms
  (easeOutCubic) puis flottement et poussée lentes, sous-titres masqués, rien dans les 220 px du haut ni les 420 px du bas.
  `rien` : pas de carte, la vidéo se fond au fond du style. La voix finit puis silence (aucun nouveau son).
- Défauts des cartes (todo #446) : le reflet du trait souligné est masqué par le tracé du trait (plus de carré gris au bout) ; le chevron de la flèche entre cartes du canevas est assez court (`CHEVRON` de `Montage.jsx`) pour rester dans l'image, avec test de verrouillage.
- Lancement depuis le dashboard (todo #437) : 📥 Analyser, section Montage, « 🎬 Monter un script… » (`--script`) et « 🎬 Monter ce fichier audio… » (`--recu <fichier de 📎 joindre>`, seule poignée acceptée, jamais un chemin), avec `--style` et `--fin` en choix fermés, rendu complet livré au fil de l'eau.

### Visuels animés (todo #436)
- **Une scène dure jusqu'à la suivante** (`to` = début de la suivante, ou fin de la voix pour la dernière) : `MAX_SCENE_S`
  n'existe plus, `MAX_SCENES` passe à 14 (le prompt demande une scène toutes les 3 à 6 s).
- **Battements** : `plan.props` donne à chaque scène `beats` (images, relatives au début de la scène, des mots dits pendant
  elle, calculées depuis `words.json`, jamais par Claude) et `reveals` (image d'apparition de chaque point). Le hook
  `useBeat(scene.beats)` de `scenes.jsx` rend le nombre de mots dits et une impulsion 0 -> 1 -> 0 sur 8 images ; chaque
  composant s'en sert (pulsation de l'accent, mot surligné qui avance dans `phrase`, points allumés sous `chiffre`, élément
  de plus dans `liste`, `schema`, `comparaison`). Chaque composant garde aussi un mouvement continu (flottement, orbites,
  reflet de la barre) : rien ne reste immobile entre deux mots.
- **Deux types de plus** : `schema` (2 à 4 `points` reliés par des flèches qui se dessinent) et `comparaison` (exactement 2
  `points`, gauche puis droite, `VS` ou flèche entre les deux selon le style). `liste` passe à 4 points. Si le nombre de points
  ne convient pas, la scène devient `phrase`. Champ optionnel **`mots`** (index de mots, un par point) dans les deux schémas
  et dans `plan.SCENE_KEYS` : `time_scenes` ne le garde que si chaque index est dans la fenêtre de la scène (de son mot
  jusqu'au mot de la suivante), croissant, et en nombre égal aux points ; sinon `reveals` répartit les points régulièrement.
  `retouch.retime` recale `mots` avec les mots conservés. Le prompt demande de préférer schéma / comparaison / liste / chiffre
  quand la voix énumère, oppose ou enchaîne, et de ne jamais répéter deux fois le même type (le code ne l'interdit pas).
- **Cadre et caméra par style** (`themes.js` champ `cadre`, composant `Stage` de `Montage.jsx` ; les scènes ne connaissent pas
  la caméra) : `noir` = nu, fond vivant (9 points et traits vert citron qui dérivent, `Background.jsx`), la nouvelle scène
  entre par un balayage vertical, poussée lente 1 -> 1,04 ; `canevas` = chaque scène est une carte blanche numérotée
  (rond orange) posée sur un grand canevas à points (2 colonnes en zigzag), la caméra glisse de la carte précédente à la
  suivante en 0,6 s (`spring`) avec un dézoom à 0,85 au milieu, flèche orange pointillée entre deux cartes, poussée 1 -> 1,04 ;
  `papier` = carte « collée » (rotation ±1,5°, ombre dure, bandeau rose incliné « N° 01 »), grain qui « bout » (graine changée
  toutes les 6 images : toutes les 4, le MP4 pesait 88 Mo), la nouvelle carte tombe avec un rebond pendant que l'ancienne glisse hors champ (12 images de
  recouvrement).
- Rendu : `--crf=26` (`render.CRF`, un peu plus léger que 18 par défaut) ; le grain qui bout alourdit le papier (37 Mo contre 13 Mo avant).
- **Mesure** : `ultron-hub montage mesure <id> [--version N]` (lecture seule, JSON, sous le verrou unique). ffmpeg extrait la
  bande des scènes (y 260 à 1380) à 10 i/s en gris 108x112 ; `fige_max_s` = plus longue suite d'images dont l'écart moyen de
  gris est sous `render.FROZEN_DIFF` (0,25), `fige_pct` = part des images figées, `remplissage` = part moyenne des pixels à
  plus de `FILL_TOLERANCE` (32) du niveau de fond du style (`BACKGROUND_GRAY` : noir 0, carte blanche 255, carte papier 247).
  Cible : `fige_max_s` <= 1,5 s et `remplissage` >= 0,25 (`cible_ok`). Limite : le grain du papier d'avant la #436 (sans carte)
  compte comme « rempli », donc le « avant » du papier est trop flatteur.

### Retouches (todo #75)
- Versions : chaque rendu écrit la version suivante `var/montage/<id>/v<n>.mp4` (`store.versions`, `VERSION_RE`) ; les
  anciennes restent. `montage purge [--days 30]` (aussi lancé, best effort, par chaque `prepare`) supprime les montages
  plus vieux que 30 jours sauf ceux gardés par 📚 (`meta.json`), seulement des dossiers au nom d'id, jamais un lien.
- Une retouche n'est qu'une **proposition** : `montage retouch <id> --scene N | --style THEME | --duree CHOIX` écrit
  `pending.json` (+ `voice.next.wav` pour ⏱) et rend aperçu + `empreinte` (12 hex du SHA-256 de `pending.json`) ; le plan
  courant ne change pas. ✅ = `montage render <id> --apply=<empreinte>` : `take_pending` revérifie l'empreinte (carte
  périmée ou proposition remplacée = refus), `apply_pending` promeut le plan (et pour ⏱ la voix et les mots), puis
  rendu de la version suivante. Une proposition ne s'applique qu'une fois.
- 🔁 Refaire une scène : tâche `tasks/montage-scene/` (sonnet, `tools: []`, schéma = `{scene}` avec EXACTEMENT l'objet
  scène du schéma de `montage-plan`, test) ; le collecteur `montage` avec `params.scene` donne la fenêtre de mots permis
  (entre les mots des scènes voisines), la scène actuelle et ses voisines (`retouch.scene_metrics`). `replace_scene` :
  catalogue vérifié par `build_scene`, mot hors fenêtre ramené à l'ancien, proposition identique refusée, toutes les
  autres scènes inchangées (seule la fin de la précédente suit le nouveau début).
- 🎨 Style : changer d'avis après coup (le choix se fait AVANT, voir « Styles (todo #434) ») parmi `plan.THEMES` (noir, canevas, papier ; `plan.THEME_LABELS`), sans Claude. ⏱ Durée : liste fermée
  `retouch.DUREE_CHOICES` (−2/−5 s au début ou à la fin, voix ×1,1 ou ×1,25) ; `retime` décale/ralentit les mots, retire
  les mots coupés et leurs scènes ; < 3 scènes ou < 3 s = refus ; voix coupée par ffmpeg `atrim` + `atempo`.
- Plugin : sous la vidéo `[🔁 Refaire une scène] [🎨 Style] [⏱ Durée]` (`scenes`/`sty`/`dur`, puis `sc:<id>:<n>`,
  `st:<id>:<style>`, `du:<id>:<choix>`, valeurs revérifiées contre `THEMES`/`DUREES` du plugin, copies testées),
  carte `ui.approval_card` (« retouche de … », back = `done:<id>`) → `rok` (render `--apply`) / `rno`. Callbacks ≤ 64 octets
  (test).
- Essai réel du 14/10 sur la vidéo de l'essai #73 : scène 5 refaite par Claude (6 s), style papier, 2 s de moins à la
  fin ; chaque ✅ = un rendu de 88 à 97 s ; v2 7,6 Mo, v3 16,2 Mo (fond clair à motifs, plus lourd), v4 14,8 Mo, 23,9 s.
  Limite vue : la scène refaite peut reprendre le type d'une voisine (le prompt le déconseille, le code ne l'interdit pas).

### Photos de l'utilisateur (todo #76, chantier « images dans le montage » #76 → #78)
- Type de scène `image` (catalogue fermé : `scenes.jsx` `Photo`, schémas, `SCENE_TYPES`) + champ **`visuel`** dans
  l'objet scène des deux schémas (vide sauf pour les types visuels ; #77 et #78 le réutilisent). `plan.SCENE_KEYS`
  et `plan.bare(scene)` = les choix d'une scène sans son temps (défauts pour les plans d'avant la #76).
- CLI `montage photos <id> <fichier…>` (1 à 5, légendes en liste JSON sur **stdin**) : `photos.py` vérifie chaque fichier
  (régulier, pas de lien, ≤ 20 Mo, ffprobe = UN flux image mjpeg/png/webp ≤ 12000 px), ffmpeg en refait un JPEG
  (`-map_metadata -1`, 1080x1440 au plus) dans `var/montage/<id>/images/p<n>.jpg` (0600) via un dossier `images.new`
  (un refus ne change rien), `images.json` liste `{id, file, legende}` ; un nouvel envoi remplace l'ancien. Puis
  nouveau plan (`plan_montage`). `photos.read` ne garde que des ids `p1`…`p5` dont le fichier existe.
- Claude reçoit `photos` (id + légende, données non fiables, légendes passées par `plan.clean`) dans `{metrics}` des
  deux tâches ; `visuel` doit être un id connu, sinon la scène devient `mot_cle`. `place_photos` : chaque photo une
  fois (un doublon prend une photo oubliée ; une photo oubliée remplace la plus longue scène non photo hors la
  première, même instant). `src` (« images/p1.jpg ») est écrit par ultron-hub dans le plan, jamais par Claude.
- Plugin : sur la carte d'aperçu `[🖼 Ajouter des photos]` (`pho:<id>`, puis « Changer les photos ») → attente 10 min
  (`PhotoWait`, dossier `mkdtemp` 0700) ; filtre `_AwaitingPhotos` (`filters.PHOTO | filters.Document.IMAGE`), hors
  attente une photo va à Hermes. Chaque photo : « 📷 Photo n reçue » + `[✅ C'est tout (n)]` (`pok:<id>`) ; la place est
  réservée avant le téléchargement (album = plusieurs messages). Tout autre bouton, commande, 🏠, Retour ou ✖ arrête
  l'attente et efface le dossier. Pas de photos après le rendu (« fais un autre montage »).
- Essai réel du 14/10 (montage « lavage » 14,3 s, 2 images de synthèse) : Claude a placé les 2 photos avec leur légende,
  rendu 57 s, 5,4 Mo. Limite vue : une photo peut ne rester qu'une seconde si la scène suivante arrive vite.

### Icônes (todo #77)
- Type de scène `icone` : `visuel` = un nom français de la liste fermée `plan.ICONS` (143 noms → icône Lucide), sinon
  la scène devient `mot_cle`. La liste est recopiée dans les deux prompts (test : `plan.icon_names()` présent tel quel) ;
  `props` donne `icon` (nom Lucide) écrit par ultron-hub, jamais par Claude. React : `icons[scene.icon]` de
  `lucide-react`, rien si le nom est inconnu.
- `lucide-react` 1.48.0 (ISC, version de plus de deux semaines) figé dans `package.json` + `package-lock.json` ; après
  🔀 : `todo-77.json` = `montage_install` (sans lui, le rendu échoue à l'import). `test_every_icon_exists_in_lucide`
  vérifie chaque nom dans le paquet installé (ignoré sans `node_modules`, par exemple dans la sandbox de 🔀).
- Essai réel du 14/10 (montage « lavage ») : Claude a choisi voiture et horloge, rendu 63 s, 5,1 Mo.

### Photos libres Pexels (todo #78)
- Type de scène `photo` : `visuel` = 1 à 5 mots anglais en minuscules (`plan.STOCK_QUERY_RE`, ≤ 40 car.), sinon
  `mot_cle`. Proposé à Claude seulement si `photos_libres` (dans `{metrics}` des deux tâches) vaut true, c.-à-d. si la
  clé existe. `stock.resolve` (après `build_plan` dans `plan_montage`, et après `replace_scene`) cherche et télécharge
  au plus `MAX_STOCK` = 3 photos ; toute erreur (pas de clé, rien trouvé, réseau, image refusée) change la scène en
  `mot_cle` : le montage n'échoue jamais à cause d'une photo. `credit` (photographe, `plan.clean`) dans la ligne d'aperçu.
- Clé : `~/.config/ultron-hub/pexels.key` (chemin depuis `pwd`, pas de lien, `[A-Za-z0-9]{20,100}`), jamais affichée,
  journalisée ni donnée à Claude ; envoyée seulement à `api.pexels.com` (jamais à l'hôte des images). Sauvegardée
  (chiffrée) avec `~/.config/ultron-hub`. Mise en place par le script jetable `~/.local/bin/ultron-cle-pexels`
  (demande la clé sans l'afficher, la teste avec `curl -H @-`, l'écrit en 0600, se supprime en cas de réussite).
- Réseau (`urllib`, sans redirection : `_NoRedirect`) : recherche ≤ 1 Mo en 15 s ; image seulement en
  `https://images.pexels.com` (port et identifiant refusés), ≤ 10 Mo en 20 s, écrite en `O_NOFOLLOW` puis
  `photos.probe_image` + `photos.to_jpeg` → `images/s-<sha256(recherche)[:12]>.jpg`. Le fichier brut est toujours effacé.
- Tests : fixture automatique `no_real_pexels` (clé jetable absente, tout vrai appel réseau fait échouer le test).
  Sonde réelle du 14/10 : l'API répond 401 à une fausse clé, transformé en « Photo libre introuvable ». Pas d'essai avec
  une vraie clé (aucune clé sur le serveur ce jour-là).

## Script texte et voix de synthèse (todo #435)
- `montage prepare --script` et `montage make --script` : le texte vient de **stdin** (jamais argv), sans audio
  positionnel (exactement l'un des deux, sinon enveloppe d'erreur). Texte nettoyé des caractères de contrôle, non vide,
  1200 caractères au plus (`tts.check_text`).
- Nettoyage avant la voix (todo #445) : `tts.clean_for_voice`, appelée par `check_text` (chemin commun CLI et dashboard), retire
  emojis, pictogrammes, flèches, puces ; ﷺ devient « paix et salut sur lui » ; « (saw) »/« SAW » restent. Même texte pour les sous-titres.
- Chaîne : `store.create` → `tts.synthesize` (Piper, voix `fr_FR-siwis-medium`, via `tools.run`, texte dans
  `tts.txt` 0600 effacé ensuite, jamais en argv) → `folder/tts.wav` → `audio.clean_voice` → Whisper → `plan.realign`
  met le vrai texte sur les horodatages entendus (si le recollage échoue, les mots de Whisper restent) → plan.
  `data.source` = `script` ou `voix` ; `data.voice` = `piper:fr_FR-siwis-medium` pour le script.
- Réemploi (todo #439) : `montage lire` (texte sur stdin) lit un texte avec la même voix pour le 🔊 Lire du dashboard
  (`montage/lecture.py`, détail dans `docs/dashboard.md`).
- Installation : action `montage_tts_install` d'`ultron-user` (sans argument ; déclarée dans
  `deploy/user/apres-fusion/todo-435.json`) : venv `~/.local/share/ultron-hub/piper/venv`, `piper-tts==1.8.0`, voix
  `.onnx` et `.onnx.json` téléchargées depuis le dépôt `rhasspy/piper-voices` et refusées si leur SHA-256 diffère
  (écriture `.part` puis `os.replace`). Sans elle : « La voix de synthèse n'est pas installée… ».
- Licence de la voix : CC BY 4.0, jeu de données SIWIS (à citer si une vidéo est publiée avec cette voix).
