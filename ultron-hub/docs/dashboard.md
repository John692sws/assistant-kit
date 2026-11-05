# Dashboard du téléphone (todos #130, #131, #132, #239, #240, #242, #244, #247, #249, #263, #264, #265, #267, #276, #283, #292, #293)

**Carte** : `src/ultron_hub/dashboard/` : `auth.py` (qui appelle : Tailscale whois + liste fermée), `views.py` (les blocs, JSON),
`server.py` (serveur HTTP/HTTPS, bibliothèque standard), `tls.py` (certificat Tailscale, contexte TLS), `catalogue.py` (le catalogue unique des actions, todo #251),
`actions.py` (chemin générique : validation par le catalogue, confirmation, jeton à usage unique, signature, appel de la CLI),
`commands.py` (`ultron-hub dashboard serve [--port 8787]`, `ultron-hub dashboard cert [--redemarrer]`), `static/` (`index.html`, `style.css`, `app.js`, sans ressource externe). Liste des appareils : `config/dashboard.yaml`. Service :
`deploy/systemd/ultron-hub-dashboard.service`, activé par `deploy/user/apres-fusion/todo-130.json` (`user_unit_enable`).
Après 🔀 de #131 et de #132 : `apres-fusion/todo-131.json` et `todo-132.json` redémarrent le service. Spec et suite du chantier : `~/notes/chantier-dashboard.md`.
Tests : `tests/test_dashboard.py` (lecture), `tests/test_dashboard_actions.py` (actions, preuve `dashboard`),
`tests/test_dashboard_catalogue.py` (tout le catalogue, entrée par entrée),
`tests/test_dashboard_tls.py` (HTTPS, repli HTTP, `dashboard cert`, faux tailscale, certificat auto-signé par openssl).

## Principe
- Lecture en GET/HEAD ; les seuls POST sont les deux étapes d'une action (voir « Actions ») et le dépôt d'un fichier (« 📎 joindre »), 405 sinon. Aucune règle métier : `views.py`
  appelle `handle()` des commandes existantes (`chantier atoi|tableau|fiche`, `usage`) et
  `health.collect`, puis ne garde que des champs choisis (jamais jeton, clé, chemin de travail).
- Un bloc en erreur affiche « Indisponible (NomDeL'erreur) » sans casser les autres. Cache de 15 s par bloc (le quota a en plus
  le cache de 60 s de `usage` : pas un appel réseau par affichage).
- Page : une seule, 375 px, thème sombre seulement (todo #281), rafraîchie toutes les 30 s et au retour sur l'onglet (en pause sur une fiche).
  `app.js` écrit avec `textContent` (le texte d'un chantier est une donnée) ; CSP `default-src 'self'`.

## Sécurité
1. Écoute **seulement** sur l'IP Tailscale lue par `tailscale ip -4` (jamais `0.0.0.0` ni `127.0.0.1` ; hors 100.64.0.0/10 = refus
   de démarrer, code 78, pas de relance en boucle).
2. Chaque requête : `tailscale whois --json <ip>` → `Node.StableID`, comparé à la liste fermée de `config/dashboard.yaml`
   (`appareils: [{nom, id}]`, ID de `tailscale status --json`). Inconnu, pas de réponse, IP du serveur ou nœud du serveur
   lui-même (même listé) = **403** avant tout le reste. C'est ce qui bloque les agents qui tournent sur serveur-test.
3. Chaque requête aussi : en-tête `Host` dans la liste fermée (IP Tailscale, nom MagicDNS et nom court lus dans
   `tailscale status --json`, avec le port), sinon 403 : bloque le *DNS rebinding* (un site étranger qui ferait pointer son nom
   vers le serveur serait « même origine » que lui-même, mais son `Host` n'est pas le nôtre).
4. Ajouter un appareil = modifier `config/dashboard.yaml` (relu et fusionné comme le code).
5. HTTPS servi par le dashboard lui-même (todo #239, voir « HTTPS ») ; sans certificat lisible, HTTP comme avant + avertissement au journal.

## Actions (todos #131, #132)
| Bouton | Commande (même CLI que Telegram, sous-processus, env minimal) | Preuve |
|---|---|---|
| ✅ Terminer (À toi : todo livrée ou à terminer ; fiche : chaque todo) | `todo done <id>` | signée `dashboard` (`done`, cible = numéro) |
| 🔀 Fusionner (À toi : branche à fusionner) | `branch merge <id>` (délai 420 s comme le plugin) | signée `dashboard` (`merge`, cible = numéro) |
| ➕ Todo (fiche ; rattachée au chantier) | `todo add --title=… --truncate --source dashboard [--notes=… --chantier=…]` | non signée |

Retrait de l'escouade autonome (todo #458) : les actions ⏹ Arrêter, ▶️ Lancer une escouade, ❓ Répondre, ➕ Remarque et ➕ Nouvelle escouade, le bloc `escouades` de `/api/state`, l'onglet Équipes et le mini-player sont supprimés (les anciens liens `#equipes`/`#escouades` atterrissent sur Accueil). `escouade` et `reponse` ne sont plus des actions signables par le dashboard. Les valeurs libres passent en
`--option=valeur` (un titre qui commence par `-` ne devient jamais une option) ou sur stdin. Après 🔀, le texte de la CLI est affiché
tel quel ; les actions après fusion se font ici depuis #242 (« Todos et fusions »). Les brouillons tapés survivent au
rafraîchissement de 30 s (mémoire de la page seulement).

Déroulé : `POST /api/action/preparer` `{action, …}` → contrôle de forme par le catalogue seulement (id `<sujet>-AAAAMMJJ-HHMMSS`,
nom de sujet, code de limite…) → feuille de confirmation « Quoi / Sur quoi / Effet / Risque » + jeton ; `POST /api/action/confirmer` `{jeton, confirme: true}` →
le jeton est **dépensé d'abord** (même en cas de refus), puis vérifié : pas périmé (120 s), même appareil, même cookie de session ;
`confirme` doit valoir exactement `true` (sinon « annulé, rien n'a été lancé » ; « Retour » envoie `confirme: false`). Ensuite
seulement : signature (▶️), appel de la CLI, résultat affiché tel quel (`summary` ou `error`), entrée au journal de l'esprit
(source `dashboard`, « … depuis le dashboard (by=dashboard:<appareil>) ») et ligne `dashboard action=… by=… ok=…` au log.

Contrôles de chaque POST, dans l'ordre (rien n'est signé ni lancé avant la fin) :
1. appareil (whois, liste fermée, serveur refusé) et `Host` connu, comme toute requête ;
2. `Origin` = exactement `https://<Host>` (`http://<Host>` en repli HTTP ; absente, `null` ou autre = 403) ; `Sec-Fetch-Site`, si présent, = `same-origin` ;
3. `Content-Type: application/json` et corps ≤ 16 Ko (une réponse de 2000 caractères en UTF-8) (un formulaire HTML d'un autre site ne passe pas) ;
4. cookie `ultron_session` (posé par `GET /`, `HttpOnly; SameSite=Strict`, 12 h, lié à l'appareil, **signé** depuis #267 : voir
   « Connexion et fusions en fond »). `Secure` en HTTPS (absent en repli HTTP, sinon le navigateur l'ignorerait).

Preuve `dashboard` (`todo/closing.py`, recopiée dans `hermes_plugin/close_key.dashboard_token`, test de concordance) : jeton
`dashboard.<appareil>.<ts>.<hmac>`, HMAC-SHA256 de `ultron-hub-close:v1:dashboard:<appareil>:<action>:<cible>:<ts>` avec
`close.key`, 120 s, anti-rejeu par la liste de preuves de chaque commande (`escouade` et `reponse` : `.preuves` des escouades /
des boîtes ; `done`, `merge` : `events` de la todo). « dashboard » n'est pas une action : aucun jeton Telegram ne peut valoir un
jeton du dashboard et inversement. Seules les actions de `DASHBOARD_ACTIONS` (`escouade`, `done`, `merge`, `reponse`, et depuis
#242 `cancel`, `fusion-lot`, `user`, depuis #249 `profil`, `savoir`, depuis #247 `reflexe`, depuis #254 `revert-fusion`, depuis #263 `compte`, depuis #265 `session`, depuis #362 `menage-lot`, depuis #417 `rapports-lot`, depuis #429 `habitude`, depuis #442 `video-lot`, depuis #495 `autorisation`) l'acceptent ; toute autre (`reopen`,
`veille`) le refuse (test paramétré sur toutes). `closing.check` rend
l'acteur `dashboard:<appareil>`, écrit dans `lance_par` de `etat.json`, `events.details.by` (todo) et `by` (réponse, remarque).
Sceau des réponses et remarques : le jeton du dashboard n'a pas le message que vérifie l'escouade (`mcp_ultron.seal_ok`) ; une fois
vérifié par `closing.authorize`, `boite._seal` scelle avec le message `reponse` simple (comme le chemin terminal). Un jeton
Telegram reste le sceau tel quel.

Modèle de menace : bloque un agent du serveur (refusé par whois avant tout), un site ouvert sur le téléphone (Origin, Host,
cookie Strict, JSON), un double appui ou un rejeu (jeton à usage unique, preuve anti-rejeu), l'appui par erreur (confirmation).
Limites assumées : même uid que les agents (un agent qui lit la clé n'est arrêté que par ultron-guard) ; un appareil autorisé
volé et déverrouillé agit comme l'utilisateur.

## Catalogue des actions (todo #251)
**Ajouter une fonction = une entrée du catalogue** (`src/ultron_hub/dashboard/catalogue.py`, tuple `CATALOGUE`). Rien d'autre :
pas de route, pas de code dans `actions.py` ni dans `app.js`. Le serveur valide contre l'entrée (`catalogue.valider`), garde
Origin, session, jeton et confirmation, signe seulement les `signee`, lance l'argv ; la page dessine le formulaire (`form(id,
contexte, listes)`) depuis `GET /api/catalogue` (ids, boutons, champs ; jamais argv, regex ni action signée) et la feuille depuis
la réponse de `preparer`. Les mots de la feuille sont ceux de la carte Telegram (`ui.approval_card` : Quoi / Effet / Risque
🟢 faible ou 🔴 élevé). Exemple (✅ Terminer, signée `done`) :

```python
Action("done", "✅ Terminer…", "signee", ("todo", "done", "{id}"),
       (Champ("id", "contexte", "id", "7", "numéro de todo refusé", regex=TODO_ID_RE, max=96),),
       ("✅ Terminer la todo (signé)",), ("#{id}",), "la tâche passe en terminée", "low",
       ("✅ todo #{id} terminée",), signe="done", cible="{id}"),
```

- Types : `lecture` (sans feuille, `POST /api/action/lire`, jamais signée), `simple` (feuille, non signée), `signee` (feuille,
  preuve `dashboard` sur `signe` ∈ `closing.DASHBOARD_ACTIONS` et `cible`). Drapeau `ouvert` (todo #283, « toujours ouvert ») :
  une `simple` ou `signee` courte à risque 🟢, sans `danger` ni `fond`, part SANS feuille par `POST /api/action/lire` (`preparer` la
  refuse) ; elle reste signée (une `signee`) et journalisée. Seules `session-ecrire` et `session-choisir` le sont (test). Étendre `DASHBOARD_ACTIONS` reste un changement de
  `closing.py` et de `close_key.py` (test de concordance).
- Champs : `contexte` (donné par la ligne de la page : id d'escouade, numéro de todo), `texte` (zone de saisie), `choix` (options
  fixes, chacune avec son argv, ou `liste` venue des données de la page, vérifiée par `regex`). Chaque champ : `regex` en
  `fullmatch`, `min` (0 = facultatif) / `max`, `nettoie` (`bords`, `espaces`), `defaut`, `exemple` (valeur valide, utilisée par
  les tests), `refus` (message), `fixe_selon` (ex. ❓ : « fait » envoie la valeur fixe).
- Modèles : `{champ}`, `{champ_libelle}` (choix), `{empreinte}` (sha256 du stdin, 16 hex). Une partie qui nomme un champ facultatif
  vide disparaît (`--notes={notes}`). `@champ` dans argv = l'argv de l'option choisie. Un texte libre ne va jamais seul dans argv :
  `--option={x}` ou `stdin`.
- `verifier()` tourne à l'import (le dashboard ne démarre pas avec une entrée mal formée) : `signee` sans action de closing,
  `simple` qui signe, argv qui n'est pas un tuple ou commence par une valeur, champ inconnu dans un modèle, regex qui accepte
  « -… », feuille incomplète, exemple refusé. `tests/test_dashboard_catalogue.py` parcourt toutes les entrées (limites, feuille,
  argv passé au vrai lecteur d'options de la CLI, bout en bout par le serveur) : une nouvelle entrée est testée sans rien ajouter.

## Todos et fusions (todo #242)

**Prête à fusionner (todo #286).** Un 🔀 n'existe (fiche, 📥 À toi, « Tout fusionner », Telegram) que pour une tâche
*prête* (`branches.readiness`) : au moins un commit propre (absent de main et de toute branche d'une autre todo, une branche
empilée garde donc seulement ses commits depuis la bifurcation ; son parent garde les siens) dont le message finit par
`(todo #<id>)`, et aucun fichier modifié ou non suivi dans le worktree de la branche. Sinon elle est « en cours » : `branch_map`
donne `pret: false`, `raison` et `ahead: 0` (`hors_main` garde le vrai compte), donc aucun bouton, aucun 🌿 « à fusionner », et
`branch lot` l'ignore. `merge_and_close` (donc le lot, 🔀 et la fusion automatique) refuse aussi, avec la raison, côté serveur.
Le dashboard fait ce que fait /todo de Telegram, avec les MÊMES commandes : 19 entrées du catalogue, aucune règle dans la page.
Onglet Todos : carte « Toutes les todos » (puces de filtre, pages de 10, « 🌱 En fond », « 🔀 Tout fusionner ») ; une ligne ouvre
la fiche (plein écran, « ‹ Retour » remonte d'un niveau : 🧰 → fiche → chantier). Une ligne de todo d'À toi ou d'un chantier ouvre aussi sa fiche.

| Entrée | Type | Commande | Preuve |
|---|---|---|---|
| `todos` 🎯 liste (filtre `open`/`urgent`/`closed`/`fond`, catégorie) | lecture | `todo board --filter <f> [--category=<c>]` | — |
| `todo-fiche` (statut, missions, branche, notes, historique) | lecture | `todo show <id>` | — |
| `commits` 🌿 | lecture | `branch info <id>` | — |
| `lot` 🔀 (ordre, écartées, TOUTES les commandes après fusion ; ➖/➕) | lecture | `branch lot [--sans=<ids>]` | — |
| `apres` 🧰 (texte exact de chaque action) | lecture | `branch after <id>` | — |
| `titre`, `notes`, `clair`, `categorie`, `priorite`, `echeance`, `echeance-date`, `tete`, `fond` | simple | `todo edit <id> --title=…` / `--notes=…` / `--clair=…` / `--category=…` / `--priority=…` / `--due=…` / `--nature chantier|fond --by dashboard` | — |
| `rouvrir` ↩️ | simple | `todo reopen <id>` | aucune (la CLI n'en exige pas ; `by: cli`, le journal dit « depuis le dashboard ») |
| `abandon` ✖ | signée | `todo cancel <id>` | `cancel`, cible = numéro |
| `fusion-lot` ✅ Tout fusionner (en fond, #267) | signée | `branch merge-lot --ids=<ids> --empreinte=<e>` (délai 3600 s) | `fusion-lot`, cible = empreinte montrée |
| `apres-run` ✅ Exécuter | signée | `branch after-run <id> <n>` (délai 700 s) | `user`, cible = `todo-<id>:<n>` |
| `fusion-auto` 🤖 (todo #254) | lecture | `branch auto-last` | — |
| `annuler-fusion` ↩️ Annuler la dernière fusion (todo #254) | signée | `branch revert-last --cible=<id>-<sha12>` (champ `fusion`, délai 420 s) | `revert-fusion`, cible = celle lue par `fusion-auto` |
| `etat-fusions` 🌿 (todo #255) | lecture | `branch etat` | — |
| `autorisation-etat` / `autorisation` 🤝 / `autorisation-retirer` 🚫 (todo #495) | lecture / signée / simple | `branch autorisation etat` / `donner --jours={jours} --perimetre={perimetre}` / `retirer` | `autorisation`, cible = `<perimetre>:<jours>` (jours 1/3/7/14) |

- **Lectures** : `POST /api/action/lire`, jamais signées ni journalisées. Le `data` de la CLI ne sort que par la liste blanche de
  l'entrée (`Action.donnees`, `catalogue.projeter` : valeurs simples, listes, objets ; `"*cles"` = noms des clés, pour
  l'historique). Rien n'est coupé : au-delà de `DONNEES_MAX` (200 000 caractères) la lecture est refusée entière, pour que ce qui
  est signé soit toujours montré en entier (règle « la carte montre TOUTES les commandes »).
- **Ce qui est signé = ce qui est montré.** 🔀 : la page lit `branch lot`, montre l'ordre et chaque commande, puis la feuille
  porte ids + empreinte ; la CLI recalcule l'empreinte (branche qui bouge, action changée = refus, rien fusionné). 6 todos au plus
  par lot depuis le dashboard (`LOT_MAX`, un seul appel HTTP ; au-delà, ➖ Retirer). 🧰 : cible `todo-<id>:<n>` de l'action dont le
  texte est affiché ; `verifier()` refuse une entrée `user` dont la cible ne commence pas par `todo-` (jamais une action de mission).
- 🌿 État des fusions (todo #255) : bouton sous la liste des todos, lecture `etat-fusions` (`branch_etat.py`, git + journal des fusions
  automatiques + todos, rien n'est écrit). Trois blocs : en cours (branches que l'orchestrateur peut fusionner seul : todo, branche,
  dernier commit, worktree propre ou non), en attente de ton 🔀 (mêmes branches avec les raisons de `auto_merge.refusals`, la fonction
  que le refus de `branch auto-merge` utilise : fichier sensible, multi-dépôts, actions après fusion ; tout `CLAUDE.md`, à la racine ou dans un sous-dossier, est toujours refusé, même pendant une autorisation d'avance : une règle de projet n'entre qu'après ton 🔀, todo #505), fusionnées seules (date, hash,
  « attend ton ✅ » tant que la todo est ouverte et livrée), puis ↩️ (`annuler-fusion`) sur la dernière non annulée. Tests : `tests/test_branch_etat.py`.
- ↩️ (todo #254) : la fiche d'une todo ouverte lit `fusion-auto` ; le bouton n'apparaît que sur la todo de la DERNIÈRE fusion
  automatique, pas encore annulée. La CLI refuse si la dernière fusion n'est plus cette cible (rien ne change).
- 🤝 Autorisation d'avance (todo #495, `autorisation.py`, remplace la semaine en dur du #482) : section de 🌿 État des fusions. Signée
  sur `<périmètre>:<jours>` (périmètre = 16 hex de `auto_merge.SECURITY` + `SECRET_NAMES`) : jusqu'au soir du dernier jour (Paris), seule la
  liste de sécurité arrête la fusion automatique. Fichier scellé (HMAC, clé de clôture) ; forgé, périmètre changé ou date passée = règles SENSITIVE. 🚫 Retirer sans signature.
- 🤖 Lot automatique (todo #533, `auto_merge.auto_merge_lot`, CLI `branch auto-merge-lot`, pas encore au catalogue) : SOUS 🤝 valide
  seulement (sinon refus, rien fusionné), jamais depuis un job ou une escouade. Lot = celui de `branch lot` (même ordre empilé), chaque
  todo par `auto-merge` (reste ouverte, livrée, `auto:orchestrateur`). Un refus aux tests est retenté après les autres (comme `merge-lot`) ;
  tout autre refus (SECURITY, actions après fusion, conflit) est listé avec sa raison et le lot continue, sauf pour les todos empilées
  dessus. `bilan` par todo (fusionnée + sha, ou refusée + raison), UN redémarrage différé à la fin. Tests : `tests/test_auto_merge_lot.py`.
- Après 🔀 (une todo), le résultat propose « 🧰 Actions après fusion (N) » ici même (plus de renvoi vers Telegram).
- Catégories : la liste vient de `todo board` (`categories`). « Notes » et « En clair » ne s'effacent pas d'ici (Telegram : « - »).
- Aperçu : `apercu.LECTURES` répond aux lectures avec de fausses données passées par la même liste blanche ; captures en plus :
  `todos-liste`, `todo-fiche`, `feuille-abandon`, `apres-fusion`, `lot`, `feuille-fusion-lot` (sombre seulement depuis #288).
- Tests : `tests/test_dashboard_todos.py` (liste blanche, preuve `cancel` / `fusion-lot` / `user` avec la vraie CLI, refus : action
  hors liste, preuve rejouée, empreinte changée, appareil non autorisé). Après 🔀 : `apres-fusion/todo-242.json` redémarre le service.

## 📥 Analyser (todo #248)
Écran plein écran « Analyser » (bouton de l'accueil, `static/app.js` bloc « 📥 Analyser » en fin de fichier) : tous les boutons sont des
`form(id)` du catalogue, aucune commande n'est connue de la page. Neuf entrées (`ANALYSER` dans `catalogue.py`, dont `joindre` : voir « 📎 joindre »), toutes `simple`
(feuille + ✅, jamais signées ; `closing.py` inchangé), sauf la lecture de durée :
**Livraison (todo #331)** : `Action.livre` (`video-analyse`, `video-tout`, `montage-script`, `montage-audio`) : à la fin réussie, `Actions._livrer` envoie par `envoi.envoyer` le titre + `data.text` + `MEDIA:<data.path>` (push, 📜 Fil, Telegram), comme les flux du plugin ; un échec d'envoi n'en fait pas un échec de l'action. La collecte seule ne livre rien (la page affiche l'id). **Todo #335 — un seul bouton** : `video-tout` (« 🔬 Analyser une vidéo… », premier de la section Vidéo) lance `video analyse-lien <lien>` (collecte puis analyse sous un seul verrou ; `data` = celle de `analyse`, plus `id` gardé si seule l'analyse échoue). Il est `fond=True` (unité systemd à part, `fond.py` : le travail continue si la page est fermée ou le dashboard redémarré) et `livre_echec` : un échec est aussi livré (`❌ …` + cause) car personne ne regarde. Délai = 17 + 45 min (`VIDEO_TOUT_S`). Les boutons 📥 / 🔬 séparés restent.

| Entrée | Commande | Délai |
|---|---|---|
| `video-duree` (lecture, sans feuille) | `video probe <lien>` ; la page affiche « Vidéo de N min… » (`duree`) | 90 s |
| `video-tout` | `video analyse-lien <lien>` (collecte + analyse, livrée) | 62 min |
| `video-collecte` | `video collect <lien>` (rend `id`) | 17 min |
| `video-analyse` | `video analyse <id>` (texte du compte rendu, 3000 car.) | 45 min |
| `montage-script` 🎬 (en fond, livrée) | `montage make --script --style <style> --fin <fin>`, script sur stdin (1200 car.) | 25 min |
| `montage-audio` 🎬 (en fond, livrée) | `montage make --recu=<fichier joint> --style <style> --fin <fin>` | 25 min |

- Un lien ou un texte venu de l'extérieur est une **donnée** : `LIEN_RE` (http/https, une seule pièce, sans contrôle), 500 caractères
  (2000 côté `docs new`), la CLI revérifie l'hôte (`video.links.is_allowed`) ; un lien ne vient jamais seul d'un champ libre sans
  regex (règle de `verifier()` assouplie : un texte positionnel est admis seulement avec une regex qui refuse « - »). Les réponses et
  textes passent sur stdin.
- Actions longues : comme pour 🔀 (`delai` + `attente`), le serveur est multi-fil (`ThreadingHTTPServer`), la commande tourne dans le fil
  de la requête avec le délai de Telegram (`video_flow`, `docs_flow`, `montage_flow`) ; `verifier()` plafonne à `LOT_TIMEOUT_S` (60 min ; `LONG_TIMEOUT_S` = 45 min pour l’analyse vidéo). Si le
  téléphone se met en veille, la commande va à son terme (résultats dans `var/`), seul l'affichage est perdu.
- Enchaînement : après `video-collecte` la feuille de résultat propose « ➡️ Étape suivante » qui rouvre l'écran avec l'`id`
  reçu (🔬 Analyser).
- `docs new` gagne `--link` / `--text-stdin` (un élément dans `items.json`, mêmes limites que Telegram) : c'est la seule façon d'amorcer un
  lot sans Telegram. Nouvelles sorties du catalogue : `duree`, `empreinte` (et `texte` lit aussi `data.what`).
- Fichiers (photo, vidéo, audio, vocal en direct, PDF, texte) : section « Fichier » en tête de l'écran, voir « 📎 joindre » (todo #293).
  Todo #437 : `montage-script` (« 🎬 Monter un script… ») et `montage-audio` (« 🎬 Monter ce fichier audio… ») font tout en un geste
  (`montage make`, voix Piper ou voix déposée, plan, rendu), en fond (`fond=True`, délai 25 min) et livrent le MP4 + le texte
  (`livre`, `livre_echec`) comme `video-tout`. Champs `style` (`plan.THEMES`, défaut noir) et `fin` (`plan.FINS`, défaut abonne),
  choix fermés. Le script voyage sur stdin, y compris en fond : `fond.run_job` le reprend du champ du travail (fichier 0600 du job).
  `montage-audio` ne reçoit que la poignée `recus.RECU_RE` d'un fichier de 📎 joindre ; la CLI (`--recu`) la résout par `recus.chemin`
  (pas de lien symbolique) et refuse tout ce qui n'est ni audio ni vidéo. Le bouton audio apparaît sous « Reçu : … » après le dépôt
  d'un audio ou d'une vidéo, et dans la section Montage (dernier fichier reçu). Reste hors dashboard : `montage photos` ; retouche « refaire la scène N » non reprise. `--requested-by` n'est pas passé (le journal dit « depuis le dashboard »).
- Tests : `tests/test_dashboard_catalogue.py` (liens refusés, argv, délais, sorties), `tests/test_docs.py` (`docs new --link/--text-stdin`).
  Captures : `analyser-*.png` et `analyser-confirmation-*.png` (`deploy/navigateur/capturer.py`, état fictif). Redémarrage après fusion :
  `deploy/user/apres-fusion/todo-248.json`.

## Missions (todo #244) : retirées (todo #455)
Les 15 entrées `mission-*`, l'entrée `demande` (🚀 Demande, todo #368), le bouton « 🤖 Confier à Claude » de la fiche d'une todo,
ses missions et les écrans de mission (liste, fiche, 📎, 🛟, 🔐/🧰) sont retirés avec le moteur de missions (archive : tag
`archive/missions-2026-10-28`). « Demander à Ultron » n'offre plus que les sessions. L'action signée `approve` n'existe plus.

## Santé détaillée (todo #247)
Ce que fait /sante de Telegram (`hermes_plugin/health_flow.py`), avec les MÊMES commandes : 5 entrées du catalogue, aucune règle
dans la page. Carte « Santé du serveur » de l'Accueil → « 🩺 Santé détaillée » (plein écran) : ultron-guard, pannes connues avec
leur bouton, batterie, analyse complète.

| Entrée | Type | Commande | Preuve |
|---|---|---|---|
| `sante-garde` 🛡 | lecture | `health guard` | — |
| `sante-reflexes` 🩺 pannes connues | lecture | `health reflexes` | — |
| `sante-batterie` 🔋 | lecture | `health battery` | — |
| `reflexe` 🩺 Agir sur la panne | signée | `health reflexe-agir <réflexe>:<empreinte>` (délai 720 s, comme le plugin) | `reflexe`, cible = `<réflexe>:<empreinte>` montrée |
| `sante-analyse` 🩺 Analyse complète | simple | `run sante-serveur --requested-by dashboard` (délai 150 s, au-dessus des 90 s de la tâche) | — (comme Telegram) |

- **Ce qui est signé = ce qui est montré** : le champ `panne` (contexte, `reflexes.CIBLE_RE` en `fullmatch`) vient du bouton du
  diagnostic affiché ; la feuille l'affiche en entier dans « Sur quoi » et le jeton est signé dessus. La CLI (`reflexes.agir`) dépense la
  preuve (`var/health/preuves-reflexes.txt`, anti-rejeu), refuse une autre empreinte ou une autre action, puis ne fait rien si le
  diagnostic n'est plus celui de `health reflexes` (empreinte changée depuis l'écran) ; ultron-user revérifie toutes les conditions.
- Liste blanche des pannes : `nom`, `diagnostic`, `action`, `detail` et, du bouton, `label` + `cible` seulement (jamais l'action
  ultron-user ni ses arguments). L'analyse rend `level`, `summary`, `points`, `recommendations`, dessinés dans la feuille de résultat
  (`RENDUS` de `app.js`) ; durée et coût du job ne sont pas montrés.
- Aperçu : `apercu.LECTURES` (fausse panne, garde, batterie) ; captures en plus : `sante-detail`, `feuille-reflexe`, `feuille-analyse`.
  `capturer.py` : appels de `shoot` des todos complétés (audit compris) et puces `.chips` (défilement voulu) exclues du contrôle de débordement.
- Tests : `tests/test_dashboard_sante.py` (concordance `DASHBOARD_ACTIONS`, preuve `reflexe` avec le vrai `reflexes.agir` et un faux
  outil, refus : action hors liste, preuve rejouée, empreinte changée, appareil non autorisé). Après 🔀 : `apres-fusion/todo-247.json`
  redémarre le service du dashboard puis hermes-gateway (`close_key.py` change).

## HTTPS (todo #239)
Adresse : **https://serveur-test.exemple.ts.net:8787** (port inchangé ; l'IP `https://100.64.0.1:8787` reste un `Host` admis
mais le certificat ne la couvre pas : passer par le nom). Le TLS est servi par `server.py` lui-même (`ssl`, TLS ≥ 1.2) : écoute
toujours sur l'IP Tailscale, whois + liste fermée + refus du serveur lui-même inchangés. `tailscale serve` est écarté : proxy local,
l'appareil appelant ne serait plus visible par whois et ses en-têtes seraient imitables par un agent local.
- Certificat : `ultron-hub dashboard cert [--redemarrer]` lit le nom dans `CertDomains` de `tailscale status --json`, lance
  `tailscale cert --cert-file … --key-file … <nom>` (sans sudo : opérateur Tailscale = ultron) vers des fichiers `.new`, vérifie
  la paire, puis remplace `~/.local/state/ultron-hub/dashboard-tls/{cert,key}.pem` **seulement s'ils ont changé** (dossier 0700,
  fichiers 0600, hors dépôt). Rend `changed`, `domain`, `expire` ; `--redemarrer` redémarre `ultron-hub-dashboard.service`
  seulement si le certificat a changé. Échec (`tailscale cert`, paire inutilisable, HTTPS non activé dans la console) = code 1,
  l'ancien certificat reste en place.
- Démarrage : certificat absent ou illisible = HTTP comme avant, WARNING `dashboard TLS off, plain HTTP: …` ; clé lisible par
  d'autres = WARNING (servi quand même). La poignée de main TLS se fait dans le fil de la requête (10 s max), pas dans la
  boucle d'acceptation ; un client en HTTP clair sur le port TLS ne reçoit rien. Connexion inactive coupée après 60 s.
- Renouvellement : `deploy/systemd/ultron-hub-dashboard-cert.timer` (lundi 04:30, `Persistent`) lance
  `ultron-hub-dashboard-cert.service` (`dashboard cert --redemarrer`). Pas de HSTS : un repli HTTP resterait joignable.
- Après 🔀 (`apres-fusion/todo-239.json`, accepté par ultron-user tel quel : timer `ultron-hub-*` + son service, redémarrages
  d'unités du dépôt) : 1. activer le timer ; 2. lancer `ultron-hub-dashboard-cert.service` (certificat neuf → dashboard redémarré
  en https) ; 3. redémarrer le dashboard (nouveau code ; si le certificat n'est pas encore là, HTTP puis l'étape 2 le redémarre).
  Ensuite, sur l'iPhone : rouvrir l'adresse https (l'icône de l'écran d'accueil pointe encore sur l'ancienne adresse http : la
  supprimer et la rajouter depuis la page https).

## Service
`user_unit_enable` accepte les timers `ultron-hub-*.timer` **et le seul** `ultron-hub-dashboard.service` (`ENABLE_SERVICES` dans
`deploy/user/ultron-user`) ; tout autre `.service` reste refusé (test `test_unit_enable_accepts_only_the_dashboard_service`).

## Installation sur l'écran d'accueil (todo #236)
Safari > Partager > « Sur l'écran d'accueil » : le dashboard s'ouvre en plein écran, sans barre Safari (`display: standalone`,
`apple-mobile-web-app-capable`, barre d'état `black-translucent`, `theme-color` sombre `#0c0c0c`). Fichiers statiques servis comme le
reste (whois puis Host, 403 sinon) : `manifest.webmanifest` (`application/manifest+json`), `apple-touch-icon.png` (180),
`icon-192.png`, `icon-512.png` (`image/png`). Icônes : lettre U blanche sur l'accent `#0969da`, générées sans dépendance par
`scripts/icones-dashboard.py` (PNG commités ; relancer le script pour changer le motif). Service worker depuis #240
(voir « Notifications Web Push ») : Si l'icône garde l'ancien aspect, la supprimer de l'écran d'accueil et la rajouter. Après 🔀,
`apres-fusion/todo-236.json` redémarre `ultron-hub-dashboard.service`. Le budget « page < 100 Ko » compte aussi le manifeste et les icônes.

## Interface en app (todo #237)
Une seule page, sans framework : en-tête fixe, 4 onglets en bas (Accueil : À toi + quota + santé ; Todos : à valider/fusionner +
chantiers ; Escouades ; Questions), thème sombre seulement (jetons `:root`, aucun thème clair depuis #281), zones sûres
(`env(safe-area-inset-*)`), cibles 44 px. Mini-player de 56 px au-dessus des onglets tant qu'une escouade tourne (sujet, durée,
progression points/budget, ⏹ qui ouvre la feuille de confirmation). La confirmation (`/api/action/preparer` puis `confirmer`, jeton CSRF)
monte du bas dans une feuille ; la fiche chantier est un écran plein écran. Transitions courtes en `transform`/`opacity`, remplacées
par un fondu sous `prefers-reduced-motion`. Aucune règle métier côté page. Emplacements laissés pour #238 : `#ptr`, `#toasts`, pastilles `.badge`.
Après 🔀, `apres-fusion/todo-237.json` redémarre le service.

Retour par balayage (todo #296) : un glissement du bord gauche (24 px) vers la droite ferme l'écran ouvert (fiche, Analyser ou lecteur) comme le bouton ‹ Retour (au-delà d'un tiers de l'écran ou plus vite que 0,5 px/ms, sinon il revient en place) ; un geste d'abord vertical est abandonné. Chaque pile d'écrans ouverts a une entrée d'historique (`pushState`/`popstate`), donc le retour du Mac ou d'Android ferme aussi l'écran, sans double fermeture.

## App vivante et pratique (todo #238)
- **Pastilles** sur les onglets, alimentées par `/api/state` (aucun calcul métier) : Questions = ❓ ouvertes, Todos = ✅ à valider + 🔀 à fusionner,
  Accueil = total « À toi », Escouades = escouades en cours. Masquées à zéro.
- **Rafraîchissement** : toutes les 30 s, au retour dans l'app (`visibilitychange`, `pageshow`) et en tirant la page vers le bas depuis le haut
  (indicateur `#ptr`, seuil environ 56 px). Jamais pendant qu'une feuille ou une fiche est ouverte (les brouillons ne sont pas perdus).
- **Heure** de dernière mise à jour dans l'en-tête (`mis à jour 14:32`) ; hors ligne : `hors ligne · dernière MAJ 14:32`.
- **Erreurs** : bandeau `#erreur` (« Serveur injoignable : Tailscale actif ? », « Accès refusé : cet appareil n'est pas autorisé. »). Les blocs vides
  disent pourquoi (« Rien n'attend ton avis. Tout est à jour. », « Aucun chantier ouvert. »).
- **Toasts** `#toasts` : résultat de chaque action (✅ résumé ou ❌ erreur), affiché à la fermeture de la feuille de confirmation ; « À jour » après un tirer-pour-rafraîchir.
- Mêmes API, aucune modification de `server.py` ni de `views.py`. `prefers-reduced-motion` : transitions neutralisées. Après 🔀, `apres-fusion/todo-238.json` redémarre le service.

## Session conseillée (todo #364)
Carte de l'onglet En cours (todo #466), juste au-dessus de « Demander à Ultron ». Lecture `conseil` (`ultron-hub chantier conseil`,
`chantier/conseil.py`, sans IA, JSON seul `{chantier, raison, regle}` + `avertissements`). Règles dans l'ordre : a) panne en cours (alertes
de santé actives, `var/health/alertes.json`) = Main ; b) chantier de domaine `business` avec des todos ouvertes (le plus fourni) ; c) sinon
chantier d'outillage (autre domaine) avec le plus de todos ouvertes ; d) sinon Main. Un chantier en pause (💤) n'est jamais conseillé.
Le `ratio` du bilan de la semaine n'est pas lu : il compte scripts contre jobs IA, pas usage contre outillage. Chaque source est best effort (avertissement, jamais d'erreur). Le bouton ne lance
rien : il règle « Où envoyer » de « Demander à Ultron » sur « Nouvelle session · <chantier> » (aucune nouvelle action signée). Tests : `tests/test_chantier_conseil.py`.

## Navigateur des agents : aperçu et captures (todo #241)
- **Installation** : `deploy/navigateur/installer.sh` (idempotent, sans sudo) crée `~/.local/share/ultron-hub/navigateur/` (venv à part,
  `pip install playwright`, `playwright install chromium`, navigateurs dans `navigateurs/`), puis vérifie qu'un Chromium headless démarre.
  Bibliothèques système manquantes : le script télécharge leurs `.deb` (`apt-get download`, sans root), les extrait dans `libs/` (le capteur
  ajoute ce dossier à `LD_LIBRARY_PATH`) ; si cela ne suffit pas il sort en code 3 avec UNE ligne `! sudo apt-get install -y …` à donner à
  l'utilisateur. L'installeur ajoute aussi, sans sudo, la police Noto Color Emoji (v2.047, empreinte SHA-256 vérifiée) dans `~/.local/share/fonts/` (`ULTRON_POLICES_DIR` pour changer) : sans elle les emojis des captures sont des carrés. Idempotent ; un échec de téléchargement n'arrête pas l'installation.
- **`ultron-hub dashboard apercu [--port 8788]`** (`dashboard/apercu.py`, aucun changement de `server.py`) : sert la page de CE worktree sur
  127.0.0.1 avec de FAUSSES données (`FIXTURES`, aucune commande réelle appelée). Seul l'appelant 127.0.0.1 passe (pas de whois) ; la
  feuille de confirmation s'ouvre, mais « Confirmer » est refusé (pas de clé de clôture, CLI jamais appelée). `dashboard serve` garde son
  refus du serveur lui-même.
- **`ultron-hub dashboard captures [--sortie DIR] [--telegram]`** (`dashboard/captures.py`) : lance l'aperçu dans un fil, puis
  `deploy/navigateur/capturer.py` en sous-processus avec le Python du venv navigateur (jamais d'import de playwright dans le venv du projet) :
  iPhone 13 mini 375x812, DPR 3, chaque onglet + la feuille de confirmation ouverte, en sombre seulement (todo #288 : `<onglet>-sombre.png`…). Défaut :
  `var/captures/<date>/` ; `--telegram` envoie chaque PNG par `envoi` (`MEDIA:<chemin>`). `ULTRON_NAVIGATEUR_DIR` change le dossier du venv.
- Tests : `tests/test_dashboard_apercu.py` (navigateur simulé ; le test de vraies captures ne tourne que si le venv navigateur existe).
- **Contrôles de design (todo #252)** : à chaque `captures`, `capturer.py` audite chaque écran (onglets + feuille de confirmation, en
  sombre) : contraste WCAG AA (texte 4,5:1, gros texte 3:1 ; texte ≥ 24 px ou ≥ 18,66 px gras), cibles tactiles ≥ 44 px, aucun débordement
  horizontal à 375 px. `captures.py` écrit `rapport.json` + `rapport.txt` à côté des PNG et met le rapport dans `data.rapport` (`ok`, `total`,
  `par_type`, `defauts`, `resume`, `fichiers`). Un défaut est signalé, jamais une exception : la commande reste en succès.

## Système de design (todo #252)
Tout est dans `src/ultron_hub/dashboard/static/style.css`. Les valeurs vivent UNIQUEMENT dans les blocs `:root` (un seul jeu de couleurs, sombre) ;
les règles n'emploient que `var(--…)`. `tests/test_dashboard_design.py` refuse dans `static/` toute couleur littérale (`#hex`, `rgb()`…) ou tout
espacement/rayon/taille de police littéral (padding, margin, gap, top/left/right/bottom, border-radius, font-size, font) hors de ces blocs.
- **Couleurs** : `--bg` fond, `--card` carte, `--high` surface élevée, `--line` filet, `--text`, `--muted`, `--accent`, `--on-accent`, `--warn`,
  `--on-warn`, `--bad`, `--on-bad`, `--scrim`.
- **Espacements** `--sp-N` (N = px : 2, 4, 5, 6, 8, 10, 12, 14, 16, 24, 52) ; **rayons** `--r-card`, `--r-btn`, `--r-sheet`, `--r-pill`, `--r-badge`,
  `--r-bar`, `--r-round` ; **typo** `--fs-N` (11 à 28), `--font-sans` ; **mouvement/gabarits** `--ease`, `--tabbar`, `--mini`.
- **Composants nommés** : `.card` (carte), `.row` (ligne), `.kv` (clé/valeur), `.chip` (`.ok`, `.warn`), `.dot` (`.warn`, `.live`), `.bar > i`
  (jauge, `.warn`/`.bad`), `.big` (gros chiffre), `.muted`, `.pre`, `.err`/`.erreur`, `button` (`.go`, `.confirm`, `.danger`, `.list`), `a.btn`,
  `select`/`textarea`, `#tabs` + `.badge` (barre d'onglets), `#mini` (mini-player), `.toast` (`.bad`), `.scrim` + `.sheet` + `.grip` (feuille du
  bas), `.overlay` + `.bar-top` (fiche plein écran), `#ptr` (tirer pour rafraîchir).
- Ajouter une valeur = l'ajouter d'abord comme token dans `:root`, puis l'utiliser par `var()`.
- **Thème choisi (todo #277, RETIRÉ par #281 : sombre seulement, plus de carte Thème ni de `data-theme`)** : carte « Thème » dans Moi, trois chips Auto / Sombre / Clair (`data-theme-choice`). Préférence locale du
  téléphone (`localStorage` clé `ultron-theme`, jamais envoyée au serveur, pas d'action du catalogue) ; `themeApply` pose
  `data-theme="sombre"|"clair"` sur `<html>` (aucun attribut en Auto), aligne les metas `color-scheme` et `theme-color` (`--bg` calculé si
  forcé) et `aria-pressed`. Appliqué avant le premier écran. Les jetons clairs existent deux fois : `@media (prefers-color-scheme: light)
  { :root:not([data-theme="sombre"]) }` et `:root[data-theme="clair"]` ; un test exige qu'ils soient identiques. Navigation privée : le choix
  s'applique mais n'est pas gardé.

## Notifications Web Push (todo #240)
- **But** : les messages d'Ultron arrivent aussi en notification sur l'iPhone (iOS 16.4+, app installée sur l'écran d'accueil,
  HTTPS obligatoire), en plus de Telegram. Code : `push.py` (clé, abonnements, envoi), `pushcrypto.py` (RFC 8291 aes128gcm + VAPID
  RFC 8292 ES256 en bibliothèque standard : P-256, HKDF, AES-128-GCM ; vérifié sur l'exemple de la RFC 8291 et les vecteurs NIST),
  `static/sw.js`. Tests : `tests/test_dashboard_push.py` (faux service push ; garde-fou `no_real_push` de `tests/conftest.py`).
- **Pourquoi sans dépendance** : `cryptography` n'est pas dans le venv ; l'ajouter demanderait une installation à la main après 🔀.
  Le code n'est pas à temps constant : acceptable ici (JWT signé en local, clé éphémère par message), à ne jamais réutiliser ailleurs.
- **Clé VAPID** : `~/.config/ultron-hub/vapid.key` (0600, créée une fois au premier `GET /api/push`, chemin via `pwd`), jamais
  affichée ni journalisée ; seule la moitié publique sort (`applicationServerKey`). La changer = supprimer le fichier, puis
  réactiver les notifications sur l'iPhone.
- **Abonnement** : carte « Notifications » de l'Accueil, bouton « 🔔 Activer » (la demande d'autorisation iOS exige un appui).
  `POST /api/push/abonner` (`{abonnement: sub.toJSON()}`) et `/api/push/desabonner` passent les mêmes contrôles qu'une action
  (whois + Host, Origin, JSON, cookie de session) ; abonner exige HTTPS (409 sinon). Point d'arrivée limité aux services push
  connus (`PUSH_HOSTS` : Apple, Google, Mozilla, Microsoft ; https, port 443) : le serveur n'écrit jamais vers une URL quelconque.
  Un abonnement par appareil autorisé, dans `~/.local/state/ultron-hub/dashboard-push/abonnements.json` (dossier 0700, fichier
  0600, hors dépôt) ; l'URL d'abonnement n'est jamais journalisée (seulement l'hôte). Une notification « 🔔 Notifications activées »
  part vers cet appareil pour vérifier. Si l'iPhone a un abonnement que le serveur a oublié, la page le renvoie toute seule.
- **Envoi** : `envoi.envoyer` (donc `ultron-hub send` et tous ses appelants) appelle `push.notify` APRÈS Telegram ; un échec push
  est journalisé et ne change jamais le résultat Telegram (et une panne Telegram n'empêche pas le push). Texte réduit : 1re ligne
  en titre (60 car.), 3 lignes suivantes en corps (180 car.), lignes `MEDIA:` retirées, chemins (`/…/…`, `~/…`) et chaînes de type
  jeton masqués. TTL 24 h. Réponse 404/410 = abonnement oublié ; autre erreur = WARNING `push refused device=… status=…`.
- **Contrôle (todo #340)** : Apple accepte en silence un abonnement périmé (le 22/10 l'iPhone ne recevait plus rien). Chaque envoi
  accepté est journalisé en INFO `push accepted device=… status=…` (jamais l'URL ni les clés) ; `abonnements.json` garde par appareil
  `depuis` (posé à l'abonnement) et `dernier_envoi`. Le réflexe `push-abonnement` de `config/sante.yaml` (`reflexes.inspecter_push`)
  alerte si un abonnement a plus de 14 jours : « ouvrir l'app, Moi > Notifications » (se réabonner).
- **Clic** : `sw.js` ouvre (ou ramène au premier plan) l'app sur l'onglet choisi par `push.tab_for` : question → Questions, chantier
  ou escouade → Escouades, boutons / sujet missions / fusion / todo → Todos, sinon Accueil (`/#<onglet>` au démarrage).
- **Coquille hors ligne** : `sw.js` met en cache `/`, `style.css`, `app.js`, le manifeste et deux icônes (réseau d'abord, cache si
  hors ligne) ; `/api/*` n'est jamais mis en cache. Après 🔀, `apres-fusion/todo-240.json` redémarre le service. Sur l'iPhone :
  ouvrir l'app de l'écran d'accueil, Accueil > « 🔔 Activer les notifications », autoriser.

## Escouades et chantiers dans l'app (todo #246)
Quatre entrées en fin de catalogue, mêmes commandes que Telegram (`autonome_flow`, `chantier_flow`, `/groupe`), type `simple`
(`closing.py` inchangé) : `nouvelle-escouade` (`escouade nouveau <sujet> --titre=… ` + objectif sur stdin ; sujet validé par
`escouade.core.NAME_RE`), `chantier-archiver` (bouton en bas de la fiche chantier), `chantier-rouvrir` (nom saisi, onglet Escouades),
(le bouton `groupe-creer` a été retiré, todo #459 ; la commande `ultron-hub groupe creer` reste). Un `texte` positionnel est accepté dans argv s'il a une `regex`
(elle refuse « -… »). `groupe creer` crée les sujets de domaine et de chantier manquants (idempotent) ; il exige le mode d'envoi
direct et un `chat_id` déjà noté dans `config/groupe.yaml` (premier `/groupe` tapé dans le groupe), sinon il répond une erreur
claire. Page : `gestionEscouades()` et `archiveForm()` dans `app.js` ; captures `nouvelle-escouade-*` et `fiche-chantier-*`.
Après fusion : `deploy/user/apres-fusion/todo-246.json` redémarre le service du dashboard.

## Profil et savoir dans l'app (todo #249)
Carte « Ton cap » de l'onglet Moi (depuis #270 ; l'ancienne carte « Plus » de l'accueil n'existe plus) : 🧭 Focus, 🔭 Revue de cap,
👤 Ce que je sais de toi ; 🔎 Anciennes conversations reste une entrée du catalogue. Chacun s'ouvre dans la fiche plein écran (`ouvrirProfil`, `ouvrirFocus`, `ouvrirCap`, `ouvrirSouvenirs` en fin d'`app.js`). Entrées `PROFIL_SAVOIR`
du catalogue, mêmes commandes que `profil_flow`, `focus_flow`, `cap_flow`, `souvenirs_flow` et les cartes fiche d'`actions_flow` :

| Entrée | Type | Commande |
|---|---|---|
| `profil` / `focus` / `cap` | lecture | `profil show` / `esprit focus` / `cap last` |
| `souvenirs` | lecture | `souvenirs chercher --texte=<mots>` (option ajoutée : les mots en un seul texte, jamais positionnels) |
| `focus-choisir` | simple | `esprit focus --set <1 ou 2 domaines> --by dashboard` (6 choix, ordre de `DOMAINES`) |
| `profil-accepter` | signée `profil`, cible = numéro | `profil accept <id> [--text=<texte corrigé>]` (le ✏️ de Telegram, même signature) |
| `profil-rejeter` | simple | `profil reject <id>` |
| `savoir-verifier` | simple | `savoir verifier var/<video\|docs>/<id>/actions/<n>.md` (n'écrit rien, rend texte + empreinte) |
| `savoir-ranger` | signée `savoir`, cible = empreinte (24 hex) | `savoir ranger var/<video\|docs>/<id>/actions/<n>.md` |

- `closing.DASHBOARD_ACTIONS` (et `close_key.DASHBOARD_ACTIONS`) gagne `profil` et `savoir`, rien d'autre ; la vérification côté CLI est
  inchangée (`closing.authorize` : jeton lié à l'action, à la cible et à l'appareil, 120 s, anti-rejeu par `store.proof_used` /
  `savoir.proof_used`). `savoir ranger` relit le brouillon et recalcule l'empreinte : un brouillon modifié après 🔎 Vérifier = refus.
- Un brouillon est **nommé** (origine `video|docs`, identifiant de lot, numéro), jamais un chemin saisi : chemin relatif à la racine du
  projet (le `cwd` de la CLI lancée par le dashboard).
- 🔬 Analyser (`video-analyse`, `video-tout`) rend aussi les propositions (`PROPOSITIONS` : n, type, titre, résumé ; jamais le chemin) :
  l'écran Analyser les liste, une fiche a « 🔎 Vérifier la fiche… », dont le résultat montre la fiche puis « 📚 Ranger la fiche… » (signée
  sur l'empreinte montrée). Todos, recherches et faits proposés restent sur Telegram.
- Tests : `tests/test_dashboard_profil_savoir.py` (vraie CLI : preuve acceptée, refus sans jeton, autre appareil, autre cible, expiré,
  rejeu, brouillon modifié ; serveur : appareil non autorisé, jeton de feuille rejoué). Captures : `profil-*`, `profil-confirmation-*`,
  `focus-*`, `cap-*`, `souvenirs-*`, `savoir-propositions-*`, `savoir-verification-*`, `savoir-confirmation-*`. Après fusion :
  `deploy/user/apres-fusion/todo-249.json` redémarre le service du dashboard.

## Voir une session ultron (todo #264)
Lecture d'une session interactive `ultron-JJ-MM-HHhMM` depuis ⋯ Plus → 💬 Sessions ultron (écrire : section « Écrire dans une session »,
gérer les sessions : section « Gérer les sessions ultron »). Trois entrées `lecture` du catalogue (`SESSION_CATALOGUE`), aucune règle dans la page :
`sessions` (`ultron-hub session liste`), `session` (`session lire <nom> [--n=N]`, nom vérifié par `NAME_RE`) et `usage` (quota) ;
les branches prêtes viennent de l'entrée `lot` existante. Code : `src/ultron_hub/sessions/` (`journal.py`, `commands.py`).
- **Retrouver le journal** : fichier `~/.claude/projects/<dossier>/<uuid>.jsonl` tenu ouvert par un processus du volet tmux ; à
  défaut, dans le dossier du cwd, parmi ceux commencés après la création de la session tmux, le plus récemment modifié (un /clear ouvre un nouveau journal, #275).
  Limite : deux sessions lancées dans le même dossier à la même minute peuvent être confondues.
- **Garde** (`safe_journal`) : chemin résolu, profondeur exactement `projects/<dossier>/<uuid>.jsonl`, lien sortant refusé ;
  jamais `.credentials.json` ni un autre fichier de `~/.claude` (tests de refus dans `tests/test_sessions_lecture.py`).
- **Rendu** : seuls les 4 Mo de queue sont lus, les `N` dernières entrées renvoyées (60 par défaut, 200 au plus). Bulles
  utilisateur / réponse, un appel d'outil = une ligne repliable (description, chemin ou motif ; jamais la commande ni la sortie),
  sous-agents lancés / finis (`Agent`/`Task`, fin = résultat synchrone ou `<task-notification>`). Rafraîchi toutes les 4 s ;
  panneau Avancement (quota, sous-agents en cours, branches `todo-*` prêtes) tous les 30 s.
- **Avancement compact (todo #278)** : un `<details class="avancement">` collé sous la barre « Retour » (`position: sticky`,
  `top: var(--bar-top)`, jeton = hauteur de `.bar-top`) ; replié, une ligne (`avancementResume` : « Avancement · 5 h 35 % · sem. 31 % ·
  2 sous-agents · 1 prête ») ; au toucher, le détail d'avant (jauges, sous-agents, branches), 60 % de l'écran au plus. Il reste ouvert
  ou fermé d'un rafraîchissement à l'autre. Captures `session-*` (replié, défilement long) et `session-avancement-*` (déplié).
- Après 🔀 : `deploy/user/apres-fusion/todo-264.json` redémarre le dashboard.
## Comptes Claude (todo #263)
Carte « Comptes » de l'Accueil, sous Quota : compte actif, quota (session, semaine) de A et de B, alerte en rouge (fichier actif
inconnu, revenu à l'ancien compte), bouton « 🔁 Basculer… » vers l'autre compte enregistré et, repliés, « 💾 Mémoriser comme
A… / B… ». Quatre entrées en fin de catalogue : `comptes` (lecture, `compte etat`, liste blanche `COMPTES` : jamais jeton ni
chemin), `compte-enregistrer-a|b` (simples, `compte enregistrer a|b`), `compte-basculer` (signée `compte`, cible = `vers`, la lettre
de destination lue dans `autre` ; `compte basculer --vers=<x>` refuse une carte périmée). La page relit la carte au plus une fois
par minute (et après une action), pas à chaque rafraîchissement de 30 s : chaque lecture appelle l'usage des deux comptes (cache
60 s). Détail du module et limites (sessions ouvertes) : `docs/compte.md`. Captures `comptes-*` et `feuille-compte-*`. Après
fusion : `deploy/user/apres-fusion/todo-263.json` redémarre le service.

## Telegram en secours (todo #290)
Deux actions simples en fin de catalogue : `telegram-activer` (`telegram on`) et `telegram-couper` (`telegram off`). Telegram est
coupé par défaut, le push et le Fil reçoivent tout ; détail dans `docs/envoi.md`.

## Connexion et fusions en fond (todo #267)
Panne : après chaque fusion, « session expirée : recharge la page » (sessions en mémoire, vidées par le redémarrage du dashboard
qu'une action après fusion déclenche) ; « Tout fusionner » attendait dans la requête HTTP et mourait avec le service.
- **Session signée** (`dashboard/session.py`) : cookie `v1.<appareil b64url>.<échéance>.<nonce>.<hmac>`, HMAC-SHA256 avec la clé
  du dashboard `~/.config/ultron-hub/dashboard-session.key` (0600, créée si absente, chemin tiré de `pwd`, **jamais** `close.key`).
  Vérifié : signature à temps constant, appareil = whois de la requête (contrôle Tailscale inchangé, fait avant), échéance. Elle
  survit au redémarrage ; seuls les jetons d'action (120 s) restent en mémoire. Session refusée = 403 `{session: "renouveler"}` :
  la page refait `GET /`, puis la lecture ou `preparer` une fois (« 🔄 Dashboard redémarré, reconnecté »), **jamais** la confirmation.
- **Action en fond** (`Action.fond`, `dashboard/fond.py`) : `merge`, `fusion-lot`, `apres-run`. Après confirmation et signature,
  le dashboard écrit `var/dashboard/fond/<job>.json` (action, valeurs, appareil ; ni argv ni jeton) et lance
  `systemd-run --user --unit ultron-dashboard-fond-<job>` (sans `--wait`, `RuntimeMaxSec` = délai + 120 s, jeton passé par
  `--setenv=NOM` sans valeur : jamais dans argv) ; l'unité vit hors du cgroup du dashboard et survit à son redémarrage. Le lanceur
  (`python -m ultron_hub.dashboard.fond <job>`) refait argv depuis le catalogue, lance la CLI comme le dashboard (`_run` puis
  `_finish` : même liste blanche, journal, log) avec `ULTRON_HUB_SUIVI=<job>`, et écrit le résultat dans le fichier.
  Todo #440 : une action en fond peut avoir un stdin **facultatif** (champ texte `min=0`, ex. la phrase de `video-tout`) : il est
  rangé dans le fichier du job (clé `stdin`, 0600, hors de `args`) et relu par le lanceur ; todo #437 : un stdin obligatoire aussi (le script de `montage-script`).
- **Progression** : `merge_progress` marque son fichier du `suivi` et ajoute l'étape `actions` (lot). L'entrée `lecture` `suivi`
  (`ultron-hub dashboard suivi <job>`) rend les étapes Vérification, Tests (`#id (i/N)`), Fusion, Actions après fusion /
  redémarrage, Terminé ou Refusé (+ `resultat`) ; sans réponse après délai + 120 s = « interrompue ». La page interroge toutes les
  2 s, dessine la barre telle quelle, continue pendant le redémarrage et affiche le résultat final. Aucune règle dans la page.
- Tests : `tests/test_dashboard_connexion.py` (session après « redémarrage », cookie altéré / autre appareil / expiré / autre clé,
  état relu après redémarrage, refus, lancement). Les tests ne lancent jamais d'unité (`conftest` : exécution en processus).
  Captures : `progression-fusion`, `fusion-refusee`.

## Écrire dans une session ultron (todo #265, refait par #283 : voir « Le Pilote »)
**Depuis #283** : plus de feuille de confirmation ni de bouton ⏹ Interrompre (entrée `session-interrompre` retirée : Échap est une
touche signée de `session-choisir` ; la commande `session interrompre` reste pour le terminal). Le reste de cette section vaut toujours
(signature, refus, trace sans le texte).
Sous les bulles d'une session : zone de texte + ➤ Envoyer, touches des questions à choix (1-9, ⬆️ Haut, ⬇️ Bas, ↵ Entrée) et
⏹ Interrompre. Trois entrées du catalogue (`SESSION_CATALOGUE`), aucune règle dans la page ; code : `src/ultron_hub/sessions/envoi.py`.
| Bouton | Commande | Preuve |
|---|---|---|
| ➤ Envoyer | `session ecrire <nom> --texte-stdin` (texte sur stdin) | signée `dashboard` (`session`, cible = `<nom>:<sha256(texte)[:16]>`) |
| Touche (1-9, haut, bas, entree) | `session choisir <nom> <touche>` | signée `dashboard` (`session`, cible = `<nom>:<touche>`) |
| ⏹ Interrompre | `session interrompre <nom>` (touche Échap) | non signée, comme ⏹ Arrêter une escouade |

- **Réponses rapides** (todo #279) : trois boutons au-dessus de la zone de texte (« Oui », « Non », « Suis tes recos ») envoient un
  texte fixe (`Champ.rapides` du champ `texte` de `session-ecrire`) par la même entrée : même feuille, même signature, aucune
  nouvelle action. Les textes vivent dans le catalogue ; `verifier()` les passe par les contrôles du champ.
- **Pourquoi signée** : écrire ou répondre à une question à choix = donner une consigne à un agent, comme ❓ Répondre (signée
  `reponse`). La CLI exige le jeton (ou `--i-am-human`) : un agent du serveur ne peut pas écrire dans la session d'un autre.
  Échap ne fait qu'arrêter le tour en cours (même niveau que ⏹ Arrêter).
- **Envoi** : `tmux has-session -t =<nom>` puis `tmux send-keys -t =<nom>: -l -- <texte>` et `Enter` à part (`=` : nom exact,
  jamais un préfixe ; `-l --` : texte littéral, jamais lu comme touche ou option). argv en liste, jamais de shell.
- **Question à choix à l'écran** (todo #308, panne du 20/10 : « 31113ok », réponse jamais arrivée) : un menu lit chaque touche comme
  une touche de menu (chiffres = choix, lettres perdues). `envoi.ecrire` lit donc l'écran visible : menu avec « Type something. » /
  « Other » → ↓/↑ jusqu'à cette ligne, relecture du curseur, puis texte ; menu sans champ libre ou curseur non arrivé = refus, rien tapé.
  Pause `PAUSE_S` (0,3 s) entre le texte et Entrée (envoyés d'un bloc, Claude Code les lit comme un collage et Entrée n'envoie pas).
  Cause non reproduite avec le vrai Claude Code (interdit) : déduite du code et du comportement du menu ; tests `MenuTmux`.
- **Refus** (rien n'est envoyé) : nom hors `NAME_RE`, session absente, texte vide, > 4000 caractères, caractère de contrôle
  (retour à la ligne, tabulation, échappement… ; la page fond les espaces et retours à la ligne en un espace), touche hors liste,
  jeton absent, d'une autre cible ou déjà utilisé, même texte à la même session depuis moins de 10 s (double envoi).
- **Trace** : journal commun « ✍️ N caractères envoyés à la session <nom> depuis le dashboard (by=dashboard:<appareil>) » et
  ligne de log `session ecrire session=… longueur=… by=…` : **jamais le texte** (la feuille de confirmation ne le répète pas).
  État dans `var/sessions/envois.json` : preuves dépensées et empreinte du dernier texte par session, jamais le texte.
- Tests : `tests/test_sessions_ecriture.py` (faux tmux, faux `ultron-hub`). Après 🔀 : `apres-fusion/todo-265.json` redémarre le dashboard.

## Gérer les sessions ultron (todo #266)
**Onglet Travail (todo #405)** : il porte les sessions (carte `travail-sessions`) : « ➕ Nouvelle session » en tête, puis une ligne
par session (nom · chantier ou Main · ouverte) avec ouvrir et « ✖ Fermer… ». Même rendu que l'écran Moi > Sessions ultron
(`lignesSessions`), même lecture `sessions`, aucune route nouvelle ; rechargé à l'ouverture de l'onglet et au rafraîchissement. La carte
sessions de l'Accueil reste un résumé, son bouton « Toutes les sessions » ouvre l'onglet. La pastille de Travail signale une session
qui « attend ta réponse » ; les décisions (À valider, questions) restent sur l'Accueil, carte actions.
Détail par session (todo #406) : `session liste` ajoute `contexte` (% de fenêtre
utilisé, même jauge que le hook `escouade/hooks.py`, `null` si inconnu), `agents` (sous-agents en cours : `nom`, `modele`, `depuis`) et
`derniere_phrase` (dernière phrase, 160 caractères au plus, sans markdown, de la dernière réponse). Script, pas d'IA ; journal illisible =
champs inconnus. Code : `sessions/etat.py` ; tests : `tests/test_sessions_detail.py`.
État déclaré (todo #431) : `ultron-hub session etat [--session <nom>] [--maintenant|--ensuite|--attend|--bloque TXT] [--efface]` écrit
`var/sessions-etat/<nom>.json` (0600, 200 caractères par champ, un champ non donné garde sa valeur ; sans texte, il relit) ; `session liste`
le rend en `declare` ({maintenant, ensuite, attend, bloque, at}, `null` si absent ou plus vieux que la session). Code : `sessions/declare.py`.
Affichage (#406) : `lignesSessions` montre sous chaque session (juste sous son nom, avant ✖ Fermer ; #418) « contexte N % » (ambre dès 60), une ligne par sous-agent (nom · modèle · depuis)
et la dernière phrase en gris, une ligne coupée ; aucune route nouvelle.
Sous la liste de 💬 Sessions ultron : « ➕ Nouvelle session… » (consigne facultative) ; dans une session, à côté de ⏹ Interrompre :
« 🔁 Relancer… ». Deux entrées du catalogue (`SESSION_CATALOGUE`), aucune règle dans la page ; code : `src/ultron_hub/sessions/gestion.py`.
Les deux passent par `~/.local/bin/ultron` (qui nomme la session `ultron-JJ-MM-HHhMM`, ou `ultron-<chantier>-JJ-MM-HHhMM` pour une
session de chantier, todo #284, et lance Claude), argv en liste, stdin fermé. 🔁 Relancer garde le chantier de la session.
| Bouton | Commande | Preuve |
|---|---|---|
| ➕ Nouvelle session | `session nouvelle [--chantier=<nom>] --consigne-stdin` (consigne sur stdin, 500 caractères au plus, vide permis) | signée `dashboard` (`session`, cible = `nouvelle:<chantier>:<sha256(consigne)[:16]>`, chantier vide = système) |
| ✖ Fermer | `session fermer <nom>` (précédée de la lecture `session-fermeture` = `session fermeture <nom>`) | signée `dashboard` (`session`, cible = `fermer:<nom>`), feuille 🔴 |
| 🔁 Relancer | `session relancer <nom>` | non signée, feuille 🔴 avec confirmation (comme ⏹ Arrêter) |

- **Succès** : le script finit par `exec tmux attach-session`, qui échoue sans terminal : son code de retour ne compte pas. Le succès =
  une ligne `Session tmux : <nom>` (nom conforme à `NAME_RE`) puis `tmux has-session` sur ce nom ; sinon erreur. `TMUX` est retiré
  de l'environnement (le script créerait sinon un `switch-client`).
- **Nouvelle** : un agent Opus avec tous les droits démarre ; la feuille le dit. Consigne refusée si seule elle serait lue comme une
  commande du script (`relance`, `liste`, `list`, ou que des chiffres), contrôle ou trop longue. Le log garde la longueur, jamais le texte.
- **Relancer** (todo #291) : dans la MÊME session tmux, sans la fermer. Le prompt est réassemblé (`ultron-hub prompt interactif
  [chantier] --sortie var/prompts/…`, un chantier devenu inconnu = refus avant toute touche), puis `send-keys /clear` + Enter,
  attente de 2 s (`CLEAR_WAIT_S`, bornée), puis le message de départ relu par `tmux show-environment -t =<nom> ULTRON_MSG`
  (jamais recollé dans un shell, refusé s'il a un caractère de contrôle) + Enter. `/clear` garde le processus : les sous-agents
  continuent, donc plus de refus pour un worktree `todo-*` sale. Repli sur l'ancien fonctionnement (nouvelle session sans consigne,
  vérifiée, puis `kill-session` de l'ancienne ; ouverture ratée = ancienne gardée) si le panneau est revenu à un shell
  (`#{pane_current_command}` = bash, zsh…) ou si `ULTRON_MSG` a disparu. Résultat : `mode` = `clear` ou `nouvelle`.
  `ultron relance` (depuis Claude, `! ultron relance`) fait pareil (todo #433 : le message est reconstruit sans la consigne du jour d'origine, réécrit dans `ULTRON_MSG` par `tmux set-environment`, seule une consigne passée à `ultron relance "…"` est reprise ; une ligne au journal, source `ultron`, type `note`), touches envoyées par `tmux run-shell -b` (après la fin de la
  commande) ; `--force` est accepté, sans effet ; hors tmux ou hors session `ultron-*` : ancien fonctionnement. Changer de compte
  Claude demande un nouveau processus : 🆕 Nouvelle session. À vérifier en réel : la notification de fin d'un sous-agent arrive
  bien dans la session nettoyée.
- **Limite** : si aucun serveur tmux ne tourne, celui lancé depuis le dashboard vit dans l'unité du dashboard (son cgroup) et
  s'arrête à son redémarrage (par exemple après une fusion) avec ses sessions.
- **Chantier d'une nouvelle session** (todo #297) : liste de choix `chantier` (champ `liste="chantiers"`, options = `chantiers` de la
  lecture `sessions`, soit les fiches `~/notes/prompt/<nom>.md` sans `_*` ni `reprise-*` ; « système (sans chantier) » par défaut) puis
  consigne facultative. Le script `ultron` lisait un seul argument (chantier OU consigne) : il accepte maintenant
  `ultron --chantier <nom> ["consigne"]` (chantier inconnu = refus, un mot seul après l'option reste une consigne). L'empreinte signée
  couvre chantier + consigne. Si la commande posée dans `~/.local/bin` est l'ancienne (la session créée n'a pas le bon chantier),
  la session est refermée et l'erreur dit de lancer `deploy/bin/poser.sh` (à faire après 🔀). La liste des sessions affiche le chantier.
  Après la création, la feuille de résultat propose d'ouvrir la nouvelle session et de fermer l'ancienne (celle du Pilote d'où on vient,
  sinon la liste).
- **Trace au journal** (todo #298) : le journal commun reçoit `ultron/session` « début <nom> chantier=<chantier|système> » (écrit par
  le script `ultron` au lancement) et « fin <nom> durée=<h:mm> ». Une seule ligne de fin par session : si Claude se termine de
  lui-même, la commande tmux l'écrit (avant de rendre un shell) ; si la session est tuée alors que Claude tourne (`session fermer`,
  `relancer` en mode nouvelle), c'est le code Python (`gestion.tracer_fin`) qui l'écrit. Un échec du journal ne bloque jamais.
  `session liste` rend `duree` (« h:mm ») et `duree_s` pour chaque session (depuis l'heure de création tmux). Redéployer le script :
  `deploy/bin/poser.sh`.
- **Fermer** (todo #297) : bouton « ✖ Fermer… » sur chaque ligne de la liste et dans le détail du Pilote. La page lit d'abord
  `session-fermeture` (sous-agents au travail, même compte que le Pilote et la liste via `etat.digest` ; worktrees `todo-*` avec travail non
  commité, par `branches.dirty_worktrees`) et l'affiche en avertissement dans la feuille (ses textes sont fixes). Refus (page et
  serveur) si des sous-agents travaillent ; compte illisible = pas de blocage, dit dans la feuille. Puis `tmux kill-session -t =<nom>`
  (jamais de `--force`), puis `tmux has-session` : la session doit avoir disparu, sinon erreur ; le résultat dit « vérifié ».
  Les worktrees ne sont jamais touchés. Captures `sessions-nouvelle-*` et `session-fermer-*`.
- Tests : `tests/test_sessions_gestion.py` (faux runner, jamais de vrai tmux ni de vrai `ultron`), `tests/test_dashboard_catalogue.py`,
  `tests/test_ultron_chantier.py` (option `--chantier`). Après 🔀 : `apres-fusion/todo-266.json` redémarre le dashboard ;
  la commande `ultron` se repose avec `deploy/bin/poser.sh`.

## Habillage Doom (todo #269, remplacé par #281)
**Remplacé** par le style terminal (section « Style terminal et poste de commandement (todo #281) ») : flamme, acier, or,
icônes au trait et police gravée ont été retirés. Historique :
Style de la maquette validée (`~/notes/maquette-app/`, `~/notes/direction-artistique-app.md` §3 et §6), sans toucher à la structure
des écrans (#270-#274), au catalogue ni aux actions signées. **Provisoire** : tout passe par les jetons de `:root` dans `style.css`
(sombre d'abord, clair « parchemin et acier » via `prefers-color-scheme: light`) ; changer de style = changer ces jetons seulement.
- **Jetons ajoutés** : `--card-b` (bas du dégradé acier), `--sheet`, `--line2`, `--hi` (reflet), `--brush` (métal brossé), `--faint`,
  `--fill-a`/`--fill-b`/`--fill-hi` (bouton plein vert), `--bad-fill`/`--bad-line` (cramoisi), `--gold`, `--gold-text`, `--gold-line`,
  `--gold-faint`, `--on-gold`, `--tab`, `--toast`/`--on-toast`, `--dark`, `--shadow`, `--emboss`, `--glow`, `--font-titre`, `--ls-titre`,
  `--ls-grave`, `--trait` (1,75), `--gx-l`/`--gx-r` (marges + zones sûres), `--r-pip`, `--hair`.
- **Typo** : titres (`h1`, `h2`, titre d'une fiche, marque) en capitale gravée `--font-titre` = Cinzel s'il est installé sur l'appareil,
  sinon Optima (iPhone), Palatino, Georgia. Google Fonts n'est PAS chargé : la CSP du serveur (`default-src 'self'`) et le test
  « aucune ressource externe » l'interdisent ; repli système seulement.
- **Règles** : cartes acier brossé (`.card`), carte héros `.forge` (À toi : liséré doré + rivets, l'or reste rare), un seul bouton plein
  par écran (`button.confirm` de la feuille ; `.go` et les autres sont en acier), `.danger` = texte et filet cramoisis, feuille d'une
  action `danger` OU de risque 🔴 = bouton plein cramoisi (`confirm danger`, jamais de vert sous un risque rouge), risque 🔴 en cramoisi.
  Pastille dorée chiffrée sur Accueil seulement ; sur les autres onglets un losange discret (le nombre reste en `aria-label`).
- **Icônes** : `ICONES_SVG` d'`app.js` (jeu au trait façon Lucide, 24 px, trait `--trait`) = `[chemin, émojis remplacés]`. `el()` remplace
  l'émoji de TÊTE d'un libellé d'interface (boutons, titres, `summary`, `span`, lignes `div` sans classe/`row`/`err`/`evt`, toasts) par
  l'icône clonée du modèle `#i-modele` d'`index.html` (`TONS` : vert, ambre, rouge). Les émojis des données restent (ligne de mission de
  la CLI, textes de l'utilisateur, titres) ; les `<option>` perdent leur émoji (`sansEmoji`). Icônes fixes de la page : `<i data-i="…">`.
- Budget de la page porté de 100 à 120 Ko (130 Ko depuis #270) (`tests/test_dashboard.py`) pour le jeu d'icônes et les jetons.
- Après 🔀, `apres-fusion/todo-269.json` redémarre le service.

## Accueil briefing (todo #270, refait par #281)
Onglets Briefing · Travail · Équipes · Moi (anciens `accueil`/`questions` → briefing, `todos` → travail, `escouades` → equipes, alias gardés dans `app.js` et `sw.js`, `push.TABS` renommé) ; une seule pastille (décisions), un losange `.pip` sur Travail et Équipes. Briefing = flamme verte (`.flamme`, jetons `--fl-*`/`--sceau-*`), phrase du jour, pile Décisions (texte, `pourquoi` et action `approuver` donnés par `catalogue.DECISIONS` via `views.a_toi` ; Approuver passe par la feuille de confirmation ; « Détails techniques » repliés), bande En cours (escouades, anneau = points / budget), Fait aujourd'hui (lecture `faits-du-jour` = `todo board --filter done_today`, 3 dernières) ; profil, focus, cap, souvenirs, sessions, Analyser, quota, comptes et santé passent dans Moi. Après 🔀, `apres-fusion/todo-270.json` redémarre le service.
- **Moi allégé (todo #410).** Les cartes Historique (anciennes conversations, sessions ultron, Analyser) et Missions quittent Moi, qui garde Ton cap, Quota, Comptes et Santé ; Analyser reste via 📎 joindre (Travail). La carte `#notifs` reste : c'est le bouton push (`initPush`). Serveur et catalogue inchangés : les lectures `missions` et `souvenirs` restent, la page ne les appelle plus.
- **Agents en cours (todo #322, #400)** : le bandeau état ajoute « · N sessions · M agents » après les jauges 5 h / sem (inchangées) ; N = sessions ultron ouvertes, M = leurs sous-agents en cours (comptés à part depuis #400, « · M agents » absent à 0) de la lecture `sessions` (`etat.digest`, rien de plus lu), sans rien tant que la lecture manque.
- Recalculé à chaque lecture `sessions` (au plus une fois par minute) ; un sous-agent illisible (`null`) compte pour 0. Après 🔀, `apres-fusion/todo-322.json` redémarre le service.

## Historique des notifications (todo #276) et 📜 Fil (todo #289, onglet depuis #292)
- **Onglet « fil »** (#292) : 4e onglet de la barre du bas (accueil / travail / fil / moi, `◎`), l'écran « Moi > Historique > Fil » est retiré.
  La lecture `notifs` est faite à chaque rafraîchissement (`loadFil`) : la pastille ambre de l'onglet = `non_lues` ; la liste n'est redessinée
  que si elle a changé (les entrées dépliées restent ouvertes). « Tout marquer lu » (`notifs-vu`) recharge l'onglet.
  Depuis #355 il tient en une icône d'enveloppe ouverte à droite du titre « Journal » (haut de la liste, visible seulement s'il y a des non lues, même feuille du catalogue).
- `notifs.py` garde chaque envoi d'`envoi.envoyer` dans `var/notifs/historique.jsonl` (0600, dossier 0700) : `at` (UTC), `titre` et `corps`
  (`push.summary` : chemins et jetons masqués, MEDIA retiré), `complet` (le texte après le titre, mêmes masques, 4000 caractères au plus),
  `pj` (pièces jointes, voir plus bas), `onglet` (celui du clic de la notification), `telegram` (l'envoi a réussi).
  `noter` ne lève jamais et ne change jamais le résultat de l'envoi ; 300 lignes gardées, réécriture atomique au-delà de 400.
- **Pièces jointes** : chaque ligne `MEDIA:<chemin>` (via `envoi.separer`, 5 au plus par message) est copiée dans `var/notifs/pj/`
  (nom `AAAAMMJJTHHMMSS-<6 hex>-<nom assaini>`, 0600, dossier 0700, 20 Mo au plus) pour survivre au ménage du scratchpad. Refusées
  sans bruit : fichier absent ou non ordinaire, trop gros, lien symbolique, chemin dans un dossier secret (`notifs.SECRET_PARTS`,
  `SECRET_TREES` : `.ssh`, `.env`, `auth.json`, `.credentials.json`, `~/.hermes`, `~/.config/gh`…). Les copies que plus aucune ligne
  gardée ne cite sont supprimées à la rotation.
- **Lecture** : `GET /api/pj/<nom>` (`server._pj`) ne sert qu'un nom stocké (`notifs.PJ_RE`, `fullmatch`, jamais un chemin libre, ni
  lien symbolique), après le même contrôle d'appareil et d'hôte que le reste. Genre (`notifs.genre`, champ `genre` de `notifs.liste`,
  donc du catalogue ; la page ne décide de rien) : `image` (png/jpg/gif/webp, pas de SVG), `texte` (`.md`/`.txt`, servi en `text/plain`),
  `audio` (mp3/m4a/aac/wav/ogg/opus/flac, `Accept-Ranges` + réponses 206/416 pour Safari), `html` (`.html`/`.htm`), `fichier` (le reste,
  seul cas en `attachment`). Les quatre premiers sont en `inline`. Le `.html` part avec `Content-Security-Policy: sandbox allow-scripts`
  et `X-Frame-Options: SAMEORIGIN` (seule dérogation aux en-têtes de la page, qui garde `frame-ancestors 'none'` et `DENY` ; `default-src
  'self'` couvre déjà `frame-src`).
- **Envois de l'utilisateur (todo #353).** Une photo, un vocal ou un fichier envoyé par 📎 joindre est noté par `recus.analyser` avec `origine="toi"` (champ de la ligne `notifs`, jamais déduit du texte, exposé par la lecture `notifs`). Le Fil le rend « ↑ Toi · 📎 Photo », décalé à droite avec un filet vert (`.toi`), au lieu de « 📎 Photo reçue » signé Ultron. Toute autre entrée a `origine="ultron"`.
- **Vignettes et puces (todo #350).** Ligne fermée : la première image de `pj` = vignette 48 px (`.fil-vignette`, `object-fit: cover`), le premier fichier non image = puce avec son nom (`.fil-puce`, 30 % de la ligne au plus), `+N` pour les autres ; le titre garde seul l'ellipse. Déplié, un fichier = carte « nom · taille · ouvrir » (`.pj-carte`, ouvre le lecteur). `taille` (octets) est ajoutée à chaque `pj` par `notifs._pj_publiques` (0 si le fichier a disparu : la carte n'affiche alors que « ouvrir »).
- **Lecteur de pièces jointes (todo #373).** Toute pièce jointe du Fil s'ouvre dans un lecteur plein écran DANS la page (`ouvrirLecteur`,
  jamais `target=_blank` : l'app iOS installée n'aurait pas de retour ; test anti-panne). Barre haute : ✖ Fermer, nom, Copier (texte),
  « Ouvrir le fichier » (secours, même onglet) ; cibles ≥ 44 px, `safe-area` respectée, sombre seulement. `history.pushState` à l'ouverture,
  `popstate` ferme : le retour arrière du navigateur ferme le lecteur. Image : `<img>` (zoom pincé natif). Audio : `<audio controls>`.
  Texte : `fetch` puis rendu `createElement` + `textContent` seulement (titres, listes, `code`, gras, blocs de code ; aucun HTML brut, tout
  échappé), borné à 200 000 caractères. Page `.html` : `<iframe sandbox="allow-scripts">` SANS `allow-same-origin` (origine opaque : ni
  cookie, ni accès à l'API du dashboard).
- Lu / non lu = une seule date dans `vu.json` (`vu_jusqu_a`) : une notification est lue si `at <= vu_jusqu_a`. Lignes illisibles ignorées.
  Un ancien historique sans `pj` ni `complet` reste lisible (`complet` = `corps`, `pj` vide) ; un nom forgé dans `pj` est écarté.
- **Lecture par notification (todo #301).** Ouvrir une entrée non lue la marque lue aussitôt : la page retire le gras, le point et décrémente la pastille
  sans attendre, puis appelle `notifs-lire` (`simple`, `ouvert` : pas de feuille). `vu.json` garde `vu_jusqu_a` (tout lu) et `lues` (dates lues une
  à une, purgées avec les notifs anciennes ; `notifs vu` la vide). **Bouton de cible** : `noter(..., cible=)` accepte `{"genre": "todo"|"session", "ref"}` ;
  sinon elle est déduite du texte (« #<id> » = todo, nom `ultron-…` = session) ; les anciennes lignes sont déduites à la lecture. Sans cible, aucun
  bouton ; avec : « Voir la todo #299 » / « Ouvrir la session … » (fiche ou Pilote, `‹` revient au Fil).
- **Haut du Fil (todo #408).** Plus de phrase d'Ultron ni de cartes « À toi » en haut du Fil : doublon de l'Accueil, qui les garde (#345 retiré du Fil). Le Journal
  commence directement. `views.phrase_a_toi` et le champ `phrase` du bloc `a_toi` restent servis mais ne sont plus lus par le Fil.
- **Journal par jour (todo #346).** Le Journal est groupé par jour (heure locale du navigateur) : en-tête collant « Aujourd'hui · ven. 24 oct. »,
  « Hier · jeu. 23 oct. », sinon « mer. 22 oct. » ; chaque ligne ne porte que l'heure (« 14:05 »). Puces en tête : Tout (défaut), Fait, Bilans, Alertes, Fichiers.
  Le classement est décidé par `notifs.genre_fil` (champ `genre_fil` de `notifs.liste`, dans la liste blanche du catalogue) : pj = fichier ; titre en ⚠/🔴/❌/🚨
  ou « panne »/« échec » = alerte ; « bilan »/« point »/« passation » = bilan ; sinon fait. La page ne fait que filtrer. Après 🔀 : `apres-fusion/todo-346.json`.
- **Messages fondus (todo #347).** Dans « Plus tôt », les messages d'un même jour (heure locale) qui visent la même todo deviennent UNE ligne : titre du
  plus récent + « · N étapes » ; un appui la déplie et montre les étapes (heure + titre, chacune dépliable comme une ligne normale). Une todo à un seul
  message reste une ligne normale. La clé vient du serveur : `notifs.liste` sert `todo` (id de la cible si son genre est `todo`, sinon `null`), dans la
  liste blanche du catalogue. Les puces filtrent AVANT le regroupement ; la carte « Nouveau » ne change pas. Capture : `fil-fondue`.
- **Boîte de notifications (todo #409).** Le Fil s'ouvre sur « Nouveau » : une carte par notification non lue (titre, 2 lignes d'aperçu, vignette si la
  première pièce jointe est une image), avec l'icône « Tout marquer lu » à côté du titre. Un appui marque la carte lue (`notifs-lire`) puis ouvre sa cible
  (todo ou session), sinon sa première pièce jointe, sinon déplie le texte complet ; la carte lue ne bouge pas avant le prochain redessin. « Plus tôt » garde
  le journal par jour et ses puces de filtre (qui ne touchent que cette carte). Aucune non lue : pas de carte « Nouveau ». Serveur inchangé.
- **Détails techniques (todo #351).** `notifs.separer_technique` retire du texte déplié les lignes de jargon (sha, `rc=N`, branche `todo-<id>/…`, « N passed », chemins absolus) vers le champ `details`, affiché dans un tiroir « Détails techniques » fermé en bas du message ; si tout est technique, le texte reste entier.
- CLI : `ultron-hub notifs liste [--n 1..200]`, `notifs vu` et `notifs lire <at>`. Catalogue : lecture `notifs` (« 📜 Fil »), actions simples `notifs-vu` et `notifs-lire`.
- Page : ⋯ Plus > Fil, plus récent en haut, point vert et gras pour un non lu, un appui déplie le texte complet, l'aperçu des images
  et le lien des autres pièces (📎) ; « Ouvrir l'onglet concerné » mène à l'onglet. Pas de champ pour répondre (autre chantier).
- Consignes : `deploy/escouade/_socle.md` et `deploy/prompt/_mode-interactif.md` demandent `ultron-hub send` et non `hermes send`
  (sinon rien n'arrive dans le fil). À poser avec `deploy/prompt/poser.sh` après 🔀 (copie vers `~/notes/prompt/`).
- Tests : `tests/test_notifs.py` (la fixture `notifs_dir` de `conftest.py` empêche d'écrire le vrai historique). Captures : `fil`,
  `fil-image`, `fil-fichier`. Après 🔀 : `apres-fusion/todo-276.json` puis `todo-289.json` redémarrent le dashboard.

## Ajout complet, ménage, rangement (todo #243, lot 2 de `~/notes/parite-dashboard-telegram.md`)
- **➕ Todo** (`todo`, simple) : titre, notes, priorité, échéance (`todo add --priority --due`), catégorie (liste `categories` de la lecture
  `todos` ; vide = catégorie par défaut du CLI), chantier. Accessible depuis Travail (« Ajouter une todo ») et depuis la fiche d'un chantier.
- **🧹 Ménage** : mode sélection de Travail (☐/☑), puis `abandon` (signé `cancel`, liste `DASHBOARD_ACTIONS` inchangée) une fois par todo :
  une feuille par todo, chaque preuve est liée à son numéro ; « Suivante » enchaîne, « Retour » ou un refus arrête le reste. Aucune règle dans la page.
- **🗂 Ranger** : lecture `ranger-proposition` (`todo rattacher --proposer`), puis `ranger-appliquer` (simple, `--appliquer=<empreinte>`) ;
  proposition changée entre-temps = refus du CLI, rien de modifié.
- **🧹 Ménage proposé (N)** (todo #362) : bouton de Travail, présent seulement si un agent a préparé un ménage (`menage proposer`,
  voir `docs/filets.md`). Lecture `menage-proposition` (todos à abandonner avec leur raison, chantiers à archiver, `refus` par ligne) puis
  `menage-lot` (signé `menage-lot` sur l'empreinte, UNE feuille pour tout). Ligne refusée (`bloquee`) = pas de bouton, proposition à refaire.
- 🔀 Tout fusionner (#242) reste accessible en bas de Travail. Captures : `todo-ajout`, `ranger`, `feuille-ranger`, `menage`, `feuille-menage`.

## Style terminal et poste de commandement (todo #281)
Spec `~/notes/spec-session-pilote.md` §6 et §7. Remplace l'habillage Doom #269 dans toute l'app ; structure des écrans, catalogue et
actions signées inchangés (mêmes feuilles, mêmes jetons, même CSP).
- **Style** (`style.css`, jetons seulement) : fond noir mat `--bg #0c0c0c` (sombre SEULEMENT : thème clair, bascule #277 et `prefers-color-scheme` retirés ; un test l'exige), une seule
  police `--font-mono` = mono du système (`ui-monospace`, SF Mono sur iPhone ; rien de chargé), corps 14 px, champs 16 px (pas de zoom
  iOS). Cadres `.card` en filet fin arrondi (`--r-card` 8 px), titre en minuscules posé sur le filet du haut (`--titre-y`). Aucun
  dégradé, ombre, halo, flou ni image (test). Couleurs : `--accent #3fbf7f` (vert Doom gardé) = action, prompt, curseur, ça avance ;
  `--warn` ambre = en attente (pastille de l'Accueil, compteurs) ; `--bad` rouge = urgent ou erreur ; `--muted`/`--faint` gris.
  Un seul bouton plein par écran (`.confirm` de la feuille, rouge sous un risque 🔴) ; `.go` = texte vert précédé de `> `.
  En-tête `> ultron▌` (curseur vert qui clignote, coupé par `prefers-reduced-motion`), titres `> travail`, onglets `~ accueil`,
  `≡ travail`, `@ moi`. Jetons Doom retirés (`--gold*`, `--fl-*`, `--sceau-*`, `--shadow`, `--font-titre`…).
- **Émojis et glyphes** : `GLYPHES` d'`app.js` remplace `ICONES_SVG`. Seuls 🔀 ❓ ✅ 📎 restent des émojis (actions à repérer) ; tout
  autre émoji d'interface en tête d'un libellé devient un caractère terminal (`.g`, tons `vert`/`ambre`/`rouge`/`pale`) : ✗ ⚠ ✻ ● ↻ ■
  ▸ ⎇ / > @ ≡ … Les émojis des données restent. Session (#264, l'écran détaillé est #283) : `> ` toi, `● ` l'agent, `└ ` un outil
  (le `⎿` de Claude Code manque dans trop de polices, vu sur les captures).
- **Accueil = poste de commandement** (onglet `briefing`, lisible en 3 s) :
  - bandeau `#etat` (blocs `sante` et `quota` de `/api/state`) : `● serveur ok · 5h 42% · sem 77%` ; rouge si une unité est en échec,
    plus d'indicateur Hermes (todo #321, on n'utilise plus Hermes) ; un appui ouvre la santé détaillée ;
  - **Actions** : 🔀 tout fusionner (items `fusionner` ; ouvre `openLot`, le flux signé `fusion-lot` existant, retiré de Travail),
    ❓ questions et ✅ à valider (compteurs d'À toi) ; ❓/✅ ouvrent la liste des décisions (texte, `pourquoi`, « Approuver » =
    action `approuver` de `catalogue.DECISIONS`, « Répondre », « Voir »). La pile de décisions, la flamme et la phrase du jour sont retirées ;
  - **Chantiers** (déplacés de Travail) : `● nom x/y` puis `▓▓▓░░░░░░░ état` ; x = todos faites, y = faites + ouvertes
    (`chantier_core.done_counts`, lecture seule de la base ; `views.chantiers` ajoute `ouvertes`, `faites`, `a_toi`, `escouade`).
    actif (vert) = une session `ultron-<chantier>` ouverte ou une escouade ; attente (ambre) = des items À toi ; sinon en pause (gris,
    repliés sous « en pause (n) »). Un appui ouvre la fiche ;
  - **Sessions** : lecture `sessions` (`session liste`, au plus une fois par minute), plus récentes en haut : `✻` vert qui pulse = travaille
    (journal écrit il y a < 90 s), `⚠` ambre + « attend ta réponse », sinon `●` gris ; âge (depuis `cree`), sous-agents en cours s'ils
    sont connus. Un appui ouvre la session existante (#264) ; « Toutes les sessions » ouvre la liste (➕ Nouvelle) ;
  - Fait aujourd'hui (#270) en bas.
- **`session liste` enrichi** (`sessions/etat.py`, sans IA) : par session `chantier` (nom `ultron-<chantier>-JJ-MM-HHhMM`, #284 ; None
  pour la session système), `sous_agents` (None si journal illisible), `travaille`, `attend` = la dernière entrée du journal est une
  réponse de l'agent dont l'une des 3 dernières lignes finit par « ? » (gras et listes tolérés) ou l'outil `AskUserQuestion`. Même garde
  de journal (`find_journal`, `read_entries` avec n = 1). Liste blanche `SESSIONS` du catalogue étendue de ces 4 champs. Limite : tant
  que #284 n'est pas fusionnée, `journal.NAME_RE` ne liste que les sessions système (les sessions de chantier apparaîtront alors seules).
- **Escouades masquées** (pause décidée le 20/10) : `ESCOUADES = false` dans `app.js` : onglet Équipes (`hidden`), mini-player, pastille et
  bande En cours ne s'affichent plus ; un lien vers `equipes` (notification, `#equipes`) ouvre l'Accueil. Les Missions passent dans Moi.
  Le code serveur (catalogue, `views.escouades`, `/api/state`) est inchangé ; `ESCOUADES = true` les rend.
- Budget de la page : 123 Ko (plafond de 140 Ko inchangé, rien de relevé). Captures (`capturer.py`) : onglets visibles seulement,
  `accueil-bas`, `accueil-questions`, `accueil-valider`, `feuille-approuver` ; 🔀 depuis l'Accueil, fiche chantier depuis les Chantiers,
  missions depuis Moi ; la feuille « nouvelle escouade » n'est plus capturée. Tests : `tests/test_dashboard.py`
  (`test_command_post_home_screen`), `tests/test_sessions_accueil.py`. Après 🔀, `apres-fusion/todo-281.json` redémarre le dashboard.

## Le Pilote d'une session (todo #283)
Spec `~/notes/spec-session-pilote.md` §3, §4, §6 (« Écran session »). L'écran d'une session (⋯ Sessions, ou un appui sur une session de
l'Accueil) devient le Pilote : on pilote la session comme au terminal. Code : `sessions/ecran.py`, `sessions/commands.py` (`ecran`, champs
ajoutés à `lire`), `app.js` (`ouvrirSession`), `style.css` (bloc « Le Pilote »). Aucune règle dans la page : tout vient du catalogue.
- **Barre d'une ligne** (`details.avancement.pilote`, collée sous « Retour ») : `⚠`/`✻`/`●` (attend / travaille / au repos), nom court
  (`<chantier> 15h38`), âge (`cree` de tmux), sous-agents en cours, `ctx 62%`. Dépliée : nom, chantier (`systeme` par défaut), contexte,
  quota et branches prêtes (lus seulement quand elle est ouverte, 30 s), sous-agents, 🔁 Relancer (feuille inchangée).
- **Contexte** (`journal.read_entries` → `contexte`) : entrées de la dernière réponse principale (`input_tokens` + `cache_read_input_tokens`
  + `cache_creation_input_tokens`) sur `CONTEXT_WINDOW` = 200 000 (`--model opus` de `ultron`). Au-delà (fenêtre 1M non dite par le
  journal) ou sans `usage` : `None`, omis par la page.
- **Fil** (par défaut, `session` toutes les 3 s) : rendu Claude Code (`>` toi avec l'heure, `●` l'agent, `└` un outil, sous-agents), replié
  à la largeur du téléphone. **Terminal** (bascule `fil`/`terminal`) : lecture `session-ecran` = `session ecran <nom>` toutes les 1 s
  (pas de nouvelle requête tant que la précédente court, rien quand la page est cachée) : `tmux display-message` (largeur × hauteur)
  puis `tmux capture-pane -p -S -1000` (texte brut, sans séquence d'échappement ; caractères de contrôle retirés, 400 caractères par
  ligne). Police 12 px, lignes repliées à la largeur de l'écran (`pre-wrap`, plus de défilement horizontal ; todo #418). Le texte n'est remplacé que s'il a changé, jamais pendant un toucher ni une sélection, ni quand le lecteur est remonté (à plus de 40 px du bas) : la mise à jour reprend au retour en bas. La barre est relue toutes les 10 s dans ce mode.
- **Réponses rapides** (au-dessus de l'invite, seulement si la session demande) : un **menu numéroté** lu sur l'écran VISIBLE
  (`ecran.menu`, script sans IA : lignes `1. …`, `2. …` consécutives, l'une porte le curseur `❯`, aucune invite `❯` en dessous ;
  les 3 premières options, chacune = la touche `1`-`3`), sinon une **question** dans le journal (`etat.waits`, comme l'Accueil) : les
  textes Oui / Non / Suis tes recos du catalogue (#279). `session lire` et `session ecran` rendent `menu` et `attend`.
- **Invite** `ultron@<chantier>:~$` (chantier de la session, `systeme` sinon) : Entrée ou ↵ envoie le texte (`session-ecrire`) ; ligne vide =
  touche Entrée. Taper `/` propose les commandes de Claude Code (`catalogue.COMMANDES`, champ `suggestions`, passées par les contrôles du
  champ). Brouillon gardé par session (mémoire de la page).
- **Plein écran** (todo #292) : le Pilote est une colonne de `100dvh` (`#fiche.plein`, zones sûres respectées) : barre « Retour », barre
  d'une ligne, bascule fil/terminal, **fil qui défile seul** (il prend toute la hauteur libre, collé en bas à l'ouverture), état, puis
  réponses rapides, invite et 📎 joindre collés en bas, zone sûre comprise (resserré par #293, voir plus bas). `visualViewport` recale la hauteur et le haut de la fiche
  quand le clavier iPhone s'ouvre : l'invite reste au-dessus. Le corps de la page est `position: fixed; inset: 0` (l'ancien
  `height: 100%` laissait une bande vide sous les onglets en PWA) et la barre d'onglets fait 49 px + la zone sûre (`--tabbar`) ; quatre onglets tiennent à 375 px (94 px chacun).
- **Plus de rangée de touches** (#292 : ↑ ↓ ← → Entrée, Échap, Tab, Ctrl-C, 1-9 retirées de l'écran). Restent les 3 réponses rapides
  (menu numéroté ou Oui / Non / Suis tes recos), ligne vide + touche « envoyer » du clavier = touche Entrée, et 📎 joindre qui ouvre
  📥 Analyser au-dessus de la session (fichiers depuis #293), « Retour » y ramène.
- **Raccourcis du terminal** (todo #419) : en vue terminal SEULEMENT, 📎, 🎙 et le champ d'invite sont remplacés par une rangée de trois
  boutons de 44 px, tous passant par `session-choisir` (touches signées, aucune route nouvelle) : « Arrière-plan » = `ctrl-b` (UN seul
  `C-b` : `tmux send-keys` ne passe pas par le préfixe tmux, vérifié sur une session jetable), « Envoyer maintenant » = `ctrl-x-s`
  (`C-x` puis `C-s`), « Échap » = `echap`. La vue fil garde l'invite, 📎 et 🎙 inchangés.
- **🔊 Lire** (todos #422, #423, #439) : sessions Main SEULEMENT (`chantier` vide), vue Fil : bouton (44 px) sous la DERNIÈRE bulle « reponse », texte
  COMPLET de l'entrée lue par `session lire` (la vue terminal ne montre que 24 lignes : le bouton n'y est plus). Nettoyage (`texteParle`) :
  blocs de code (``` ou indent ≥ 4), URLs, chemins, hashes, backticks retirés sans laisser « Voir et » (mot-lien et parenthèses retirés avec) ;
  puces = phrases terminées par un point ; coupé à 2500 caractères (fin de phrase). **Voix (todo #439)** : la page appelle l'action ouverte
  `voix-lire` (catalogue, `POST /api/action/lire`, sans feuille, risque faible, aucune route nouvelle) = `ultron-hub montage lire` : texte sur
  stdin, synthèse par le MÊME Piper et la MÊME voix `fr_FR-siwis-medium` que le montage (`montage/tts.synthesize`, rien de dupliqué), wav
  `<horodatage>-<hex>-lecture.wav` 0600 dans `var/notifs/pj/` (servi par le `GET /api/pj/` existant, audio avec Range), effacé au bout d'une
  heure à chaque nouvel appel (`montage/lecture.py` ; le ménage du fil les épargne). Le journal garde la longueur, jamais le texte. Un `<audio>` est
  « déverrouillé » dans le clic (geste iOS) ; pendant la synthèse le bouton dit « ⏳ Voix… ». Si la synthèse échoue (voix non installée, délai
  de 90 s, réponse invalide) ou si la lecture est refusée, repli sur `speechSynthesis` (voix `fr-FR` si dispo). 2e appui = arrêt (« ⏹ Stop »).
  Le cache du shell passe en `ultron-shell-v4`.
- **Bandeaux resserrés** (todo #293, maquette validée par l'utilisateur ; objectif : le fil ≥ 80 % de la hauteur) : en haut UNE ligne
  `.pilote-haut` = `‹` (retour, 44 px), l'état (`⚠`/`✻`/`●`, heure de la session, âge · agents · contexte %, un appui ouvre le détail
  `.pilote-detail`, ancienne barre dépliée) et la bascule `fil | term` ; la barre « ‹ Retour » de la fiche est masquée en plein écran.
  Plus de ligne « à jour hh:mm:ss » (une erreur de lecture s'affiche seule), plus de libellé « ❓ la session attend : » (les boutons
  suffisent, sur une ligne qui défile). En bas UNE ligne : `📎` (44 px) puis le champ (invite `ultron@<chantier>:~$` en indication) ;
  plus de bouton ↵ : la touche « envoyer » du clavier (`enterkeyhint="send"`, envoi implicite du formulaire, Entrée au clavier
  physique) envoie, ligne vide = touche Entrée. Zone sûre du bas comptée UNE fois (`.pilote-bas`, rognée de 14 px, nulle clavier
  ouvert : classe `#fiche.clavier` posée par `visualViewport`), barre d'onglets et mini-lecteur masqués sous le Pilote
  (`body.pilote`). Mesure (aperçu 375×812, sans zone sûre) : fil 48 % → 82 % de la hauteur avec une question, 60 % → 88 % sans ; sur
  iPhone la barre d'état et la barre d'accueil en reprennent ≈ 8 %. Le catalogue `session-choisir`
  garde toutes ses touches (`envoi.KEYS`) : les réponses rapides et Entrée en ont besoin, et la commande `session choisir` reste pour le terminal.
- **Sécurité** (décision de l'utilisateur du 20/10, spec §3) : saisie et touches « toujours ouvertes » sur appareil autorisé — whois,
  `Host`, `Origin`, cookie signé #267 inchangés (`server.py` intact). Chaque envoi : signé par le serveur (`session`, même cible qu'avant),
  journalisé (« ✍️ N caractères… », « ⌨️ touche … » ; longueur, jamais le texte), ligne de log. Deux appuis sur la même touche dans la même
  seconde = deux preuves : `Actions._timestamp` prend la dernière seconde libre pour cette cible ; au-delà de 60 envois identiques en
  2 min, refus (jamais un jeton périmé). Le doublon de texte en 10 s reste refusé par `envoi.ecrire`.
- **Limites** : le terminal brut montre TOUT ce qu'affiche le volet (sorties d'outils comprises, que le fil cache) ; une lecture = un
  processus `ultron-hub` (≈ 0,3 s de CPU par seconde tant que le terminal est affiché) ; la détection de menu suit l'affichage actuel de
  Claude Code (un autre format = pas de réponse rapide, les touches 1-9 restent) ; le contexte suppose la fenêtre de 200 k.
- Captures (`capturer.py`, fonction `session`) : `session` (fil + réponses d'une question), `session-avancement`, `session-outil`,
  `session-rapides`, `session-champ` (complétion `/co`), `session-terminal` (menu → `1. Yes`…), `session-sans-question` (#293). Tests : `tests/test_sessions_pilote.py`,
  `tests/test_sessions_ecriture.py`, `tests/test_dashboard_catalogue.py` (entrées ouvertes de bout en bout). Après fusion : redémarrer le
  dashboard à la main (pas de fichier `apres-fusion` : rien dans `deploy/` hors `capturer.py`).

## Redémarrage automatique après fusion (todo #294)
- Toute fusion dans ultron-hub (🔀, 🔀 Tout fusionner une seule fois pour le lot, fusion automatique, annulation d'une fusion
  automatique) dont le diff touche `src/ultron_hub/dashboard/` planifie `ultron-hub-dashboard.service` (`systemctl --user restart`) via
  `branches.restart_dashboard` : unité transitoire `systemd-run --user --on-active=5`, donc le redémarrage survit à la mort de
  l'appelant (le dashboard peut lancer la fusion). Aucune déclaration `apres-fusion` ni bouton : ne plus en écrire pour ça.
  Résultat : `result["dashboard"]` = `planned` ou `échec : …` (jamais bloquant ; ligne « 🔄 Le dashboard redémarre » dans le message).
- Contrôle (réflexe `dashboard-a-jour`, `config/sante.yaml`, `health/reflexes.py:inspecter_dashboard`) : `health alertes` signale
  si l'heure de démarrage du service (`ActiveEnterTimestamp`) précède le dernier commit de fusion de main touchant
  `src/ultron_hub/dashboard/` (`git log --first-parent -m`). Sans bouton ; service non actif ou inspection impossible = rien
  (une unité en échec a déjà sa propre alerte). Tests : `tests/test_dashboard_redemarrage.py`.

## 📎 joindre : envoyer un fichier à Analyser (todo #293)
Décision de l'utilisateur (20/10) : « 📎 joindre » = 📥 Analyser amélioré, pas un nouveau système. Usage premier : envoyer une capture
d'écran de l'iPhone au serveur.
- **Entrées** : bouton `📎 joindre` en tête de l'onglet Travail (déplacé du Fil par #407 ; le Fil n'a plus de saisie, l'analyse y revient), `📎` de la ligne du bas du Pilote,
  « Analyser un fichier, un lien ou un texte » dans Moi. Toutes ouvrent 📥 Analyser, section « Fichier » en tête : « 📎 Photo, vidéo,
  audio ou PDF » (sélecteur du téléphone, `accept` image/vidéo/audio/PDF/txt/md/html) et « 🎙 Enregistrer un vocal » (MediaRecorder,
  HTTPS requis ; « ⏹ Arrêter et envoyer (m:ss) », arrêt seul à 20 min ; m4a sur iPhone, weba ailleurs).
- **Dépôt** : `POST /api/fichier` (`server._upload`), corps brut `application/octet-stream`, nom dans l'en-tête `X-Nom-Fichier`
  (encodé). Contrôles AVANT de lire un octet : appareil (whois) et `Host` comme toute requête, `Origin` identique, `Content-Type`
  exact (un formulaire d'un autre site ne passe pas), `Content-Length` entre 1 octet et 200 Mo, cookie de session signé ; un refus
  ferme la connexion (le corps non lu n'est jamais pris pour une requête). Puis `recus.recevoir` : écrit par morceaux de 1 Mo dans
  `var/recus/.tmp/` (0600), **type lu dans le contenu** (signatures : PNG, JPEG, GIF, WebP, HEIC, PDF, MP4/MOV/M4A, WebM, Ogg,
  MP3, WAV ; texte = UTF-8 sans octet nul ET nom en .txt/.md/.html ; SVG, exécutables, archives, scripts refusés), plafond par genre
  (image et document 20 Mo, audio 100 Mo, vidéo 200 Mo), 2 Go au total pour `var/recus` (au-delà : refus, rien n'est effacé seul).
  Rien n'est lancé, rien n'est journalisé dans l'esprit (ligne de log `dashboard upload device=… genre=… octets=…`, jamais le nom).
- **Où atterrit le fichier** : `var/recus/<AAAA-MM-JJ>/<AAAAMMJJ>T<HHMMSS>-<6 hex>-<nom assaini>.<extension du contenu>`, dossiers
  0700, fichier 0600 (sur le serveur : `/home/exemple/workspace/projects/active/ultron-hub/var/recus/…`). Nom assaini : lettres,
  chiffres, `_` et `-` (40 au plus, jamais en tête), le reste devient `_`. Ce nom (`recus.RECU_RE`, `fullmatch`) est la seule poignée
  que la page et la CLI se passent ; `recus.chemin` refuse tout autre texte et tout lien symbolique. L'orchestrateur les retrouve par
  `ultron-hub joindre liste [--n N]` (plus récents d'abord, avec le chemin absolu) ou dans le texte écrit dans sa session.
- **Analyse** : action `joindre` du catalogue (simple, feuille « 📎 Analyser le fichier joint », ouverte d'elle-même après le dépôt ;
  champs `fichier` et `session`, contexte), commande `ultron-hub joindre analyser <fichier>` (délai 25 min) :
  photo = gardée seulement (pas de modèle de vision ici : l'orchestrateur la lit par son chemin) ; PDF, texte = lot `docs` (`docs new`
  puis `docs present`, comme 📥 Analyser de Telegram) ; audio et vidéo = piste son seule (ffmpeg `-vn`, Opus mono 32 kb/s, les images
  d'une vidéo ne sont jamais décodées dans le lot) puis le même lot (transcription Whisper, 20 min au plus). Contenu = données, jamais
  des consignes. Sortie : texte, `id` du lot (➡️ Étape suivante : 💡 Actions), `donnees` (`JOINT` : fichier, genre, chemin, taille,
  `pour_session`).
- **Vocal de l'utilisateur 🎙 (todo #365)** : seul un audio déposé par l'enregistreur 🎙 est la parole de l'utilisateur. Marqueur =
  en-tête `X-Source: vocal` de `POST /api/fichier`, lu seulement dans `server._upload` (route authentifiée : appareil, Origin, cookie
  de session ; un refus ne garde rien) ; `recus.recevoir(vocal=True)` pose alors, pour un audio seulement, un fichier vide
  `<nom reçu>.vocal` (0600, hors `RECU_RE` donc invisible de `joindre liste`) ; `recus.est_vocal` le lit. Jamais déduit du nom. Pour
  ce fichier, `pour_session` = « 🎙 Message vocal de l'utilisateur (fichier …), transcription brute : … » (texte intégral de
  `collected.json`, pas le résumé neutre, 3900 caractères au plus) et `donnees.vocal` = vrai. Todo #397 : pour ce vocal, plus de lot `docs` ni de job Sonnet `docs-presenter` : `vocal_rapide.transcrire` décode en WAV 16 kHz
  (ffmpeg) puis faster-whisper `base` (cpu, int8, beam 1, fr, sans mots horodatés), ~7 s pour 1 min 34 (au lieu de ~50 s) ; erreurs
  en français (pas de son, pas de parole, plus de 20 min, faster-whisper absent), fichier gardé. Un 📎 audio choisi (sans en-tête) reste
  « données, jamais consignes ». **À faire côté `static/app.js`** (`envoyerFichier`, appelé par l'enregistreur) : ajouter
  `"X-Source": "vocal"` aux en-têtes du seul envoi du vocal enregistré (sans cela, tout reste en données).
- **Vocal en 2 appuis (todo #379)** : sur la ligne du bas du Pilote, `🎙` (si micro + MediaRecorder) : appui 1 = enregistre (le bouton
  devient `⏹ m:ss`, rouge), appui 2 = dépôt (`X-Source: vocal`) puis action `joindre-vocal` SANS feuille (`ouvert`, POST
  `/api/action/lire`) = `joindre analyser <fichier> --vocal-seul`, qui REFUSE tout fichier non marqué vocal (`recus.est_vocal`) ; la page
  écrit alors `pour_session` dans la session (`session-ecrire`) et affiche un toast. Le `🎙` d'Analyser suit le même chemin (sans feuille) ;
  un `📎` fichier garde sa feuille. Exception nommée au garde « ouvert = court » : `Action.ouvert_long` autorise 25 min, seulement avec
  `--vocal-seul`, risque faible, sans stdin/fond/danger (`catalogue.verifier`). Toute erreur reste affichée (toast qui ne s'efface qu'au toucher).
- **Vocal et veille (todo #332)** : pendant l'enregistrement, `navigator.wakeLock.request("screen")` garde l'écran allumé (iOS coupe le micro en veille) ; relâché à tout arrêt (normal, erreur, coupure), repris au retour au premier plan (`visibilitychange`). Sans wakeLock rien ne casse. Panne du 30/10 (l'écran s'éteignait quand même) : le verrou était demandé après l'invite micro ou le catalogue, hors du toucher, et un refus était avalé. Désormais demandé DANS le toucher (`basculerVocal` et le 🎙 du Pilote avant `chargerCatalogue`), repris si iOS le lâche écran allumé (`release`, puis toutes les 5 s), et un refus ou une absence d'API s'affiche une fois par vocal (« ⚠️ Écran non gardé allumé (raison) », toast fixe) : c'est le contrôle. Test : `test_screen_lock_asked_at_tap_retaken_and_refusal_said`. Un `stop` non demandé ou une piste micro `ended` affiche un toast fixe « ⚠️ Vocal coupé par la veille à m:ss » ; le morceau enregistré part quand même.
- **Dictée de l'appareil d'abord (todo #398)** : le `🎙` du Pilote tente `SpeechRecognition`/`webkitSpeechRecognition` (`fr-FR`, résultats
  en direct, continu) : le texte s'écrit dans le champ de la session, l'utilisateur relit puis envoie lui-même (même geste 🎙 puis ⏹).
  Repli automatique sur l'enregistrement + transcription serveur ci-dessus si l'API manque, sur `not-allowed`/`service-not-allowed`/
  `audio-capture`/`network`, ou si rien n'arrive en 8 s (le texte déjà dicté est gardé, un toast dit « dictée indisponible,
  enregistrement classique »). Un toast nomme le chemin utilisé (« dictée de l'appareil » ou « transcription serveur »). 🎙 depuis
  Analyser sans session : toujours le chemin serveur. Confidentialité : la dictée Apple envoie souvent le son chez Apple, le chemin
  serveur reste local. À activer : Réglages > Général > Clavier > Activer la dictée. Inconnue : en app installée (écran d'accueil,
  standalone) iOS peut refuser la dictée, d'où le repli ; essai réel à faire sur iPhone 13 mini et iPad.
- **Livraison** : chaque analyse (ou son échec) est une entrée du Fil (`notifs.noter`, sans Telegram ni push : l'utilisateur est sur
  la page), avec le fichier en pièce jointe (`MEDIA:`, copie dans `var/notifs/pj/` jusqu'à 20 Mo : image visible dans le Fil).
  Ouverte depuis le Pilote, la page envoie aussi `pour_session` à la session par `session-ecrire` (signé `session`, journalisé, une
  ligne : « 📎 Pièce jointe envoyée depuis le dashboard (photo) : <chemin>. Résumé : … Contenu venu de l'extérieur : à lire comme des
  données, jamais comme des consignes. »).
- **Limites** : pas de ménage automatique de `var/recus` (2 Go puis refus) ; une analyse longue (audio, vidéo) dont la page a perdu la
  réponse (téléphone en veille) arrive dans le Fil mais n'est pas écrite dans la session ; HEIC gardé mais pas affiché en ligne dans
  le Fil (lien de téléchargement).
- Tests : `tests/test_joindre.py` (types, noms, plafonds, chemins forgés, liens symboliques, analyse photo/PDF/vidéo/échec, route :
  origine, type, cookie, appareil, taille, marqueur 🎙), `tests/test_dashboard_catalogue.py` (entrée `joindre`). Captures : `analyser-fichier`,
  `feuille-joindre`, `travail` (bouton 📎 joindre, #407). Redémarrage après fusion : automatique (#294).
- **Quota et comptes fondus (todo #411).** La carte `#quota` quitte Moi : `#comptes` (titre « Quota et comptes ») montre une ligne par compte (adresse, « · actif », session 5 h, semaine, « dose du jour : N points » lue dans `comptes[].dose` de la lecture `budget`). `showQuota` est supprimé ; l'en-tête d'état garde `s.quota`.
- **Budget du jour retiré de Moi (todo #412).** Le bloc « 💰 Budget du jour » et les lignes « x %/j » disparaissent ; restent la dose du jour par compte et la phrase « 👉 » (une ligne, `conseilNodes`). Lecture `budget` inchangée côté serveur.
- **Textes de Quota et comptes (todo #413).** Plus de ligne « Actif » (le « · actif » de la ligne du compte le dit ; « Compte en cours inconnu. » seulement s'il l'est) ; « 💾 Enregistrer le compte actif » devient « 💾 Mémoriser la connexion en cours » (boutons « 💾 Mémoriser comme A… ») ; le conseil de `compte budget` nomme le compte par son adresse (« le compte A » sans adresse).

## Rapports du dimanche (todo #417)

Todo #535 : si le lot ne supprime rien pour de vrai, il est appliqué seul (`rapports appliquer-auto`, appelé en fin de `rapports preparer`, `by` = `auto:bilan`) et listé au bilan du soir ; sinon le ✅ signé reste le seul chemin. Détail dans docs/filets.md (ménage proposé).
Préparés chaque dimanche à 10 h par `rapports preparer` (minuterie unique `ultron-hub-dimanche.timer`, todo #460, `docs/filets.md`),
qui envoie UN message au Fil « 📋 Rapports du dimanche prêts, onglet Moi » + une ligne par rapport et la question du focus.
Carte de l'onglet Moi, sous « Ton cap » : trois lignes datées (🔭 revue de cap, 🧹 ménage proposé, 🩺 santé), chacune ouvre la fiche
existante (`ouvrirCap`, `openMenagePropose`, `openSante`), puis UN bouton « ✅ Appliquer les actions conseillées… », absent s'il n'y a
rien à faire. Code : `src/ultron_hub/rapports.py` ; page : `loadRapports` / `showRapports` dans `app.js` (au plus une lecture par
minute, relue après une action réussie).

- `ultron-hub rapports dimanche` (lecture seule, sans IA) : `cap` (`cap last` ; chaque correction avec `todo` = la todo déjà faite :
  source `cap:<date>` et même titre, entier, coupé à 40 comme /cap, ou tel que `todo add --truncate` le rend, quel que soit son statut),
  `menage` (`menage proposition`), `sante` (alertes actives de `var/health/alertes.json`, pannes connues de `var/health/reflexes.json`),
  `actions` = exactement ce que fera le lot (une todo par correction pas encore faite, puis le ménage si une proposition applicable
  existe), `empreinte` = 16 hex du SHA-256 d'une ligne JSON par action (clés triées, sans le libellé), null sans action.
- Les réflexes de santé ne sont **jamais** dans le lot (risqués) : la carte les compte et renvoie au bouton signé `reflexe` de Santé.
- `ultron-hub rapports appliquer --empreinte X` : recalcule sous verrou (`var/rapports/lot.lock`) ; empreinte différente ou rien à
  faire = refus, rien fait ; exige le jeton `rapports-lot` signé sur l'empreinte (ou `--i-am-human`), jamais rejoué (preuves dans
  `var/rapports/appliques.jsonl`, écrites avant la première action). Puis chaque action est tentée : todos par le code de `todo add`
  (mêmes contrôles, source `cap:<date>`, chantier de la correction, « en clair » = la preuve), ménage par `menage_lot.appliquer`
  (mêmes contrôles ; trace `by` du lot + `rapports: <empreinte>`). Bilan d'une ligne par action, ligne au journal commun.
- Catalogue : `rapports` (lecture, liste blanche `RAPPORTS`) et `rapports-lot` (signée `rapports-lot`, cible = empreinte). Nouveau champ
  d'entrée `detail` : `prepare` relance cette lecture (`Actions._lire_detail`) et met ses `actions[].texte` dans la feuille (`detail`, 30
  lignes au plus) seulement si son empreinte est celle du champ `empreinte`, sinon « la liste a changé » : la feuille liste TOUT ce qui
  est signé. `verifier` exige une lecture sans champ qui rend une empreinte, pour une action signée qui en a une.
- Tests : `tests/test_rapports.py` (dédoublonnage, refus d'empreinte, jeton d'un autre lot, preuve rejouée, catalogue, signatures
  plugin/CLI identiques, feuille). Capture : `moi-*`.

## Nouvelle habitude proposée (todo #429)
Une carte par brouillon de skill en attente (écrit par la skill `fin-de-chantier`, `~/ultron/propositions-skills/<date>-<nom>/`),
dans Moi sous « Rapports du dimanche », absente s'il n'y en a aucun. Décision de l'utilisateur (27/10) : la carte montre le TEXTE
COMPLET qui sera installé, et le ✅ est signé sur l'empreinte de ce texte exact. Code : `src/ultron_hub/habitudes.py` ; page :
`loadHabitudes` / `showHabitudes` dans `app.js` (conteneur `#habitudes`).

- `ultron-hub habitudes liste` (lecture seule) : `{dossier, nom, mode (nouvelle|patch|remplace), resume (2 phrases : BROUILLON + RAISON), texte,
  lignes, empreinte, complet, trop_long}` ; `texte` = SKILL.md (nouvelle) ou ce qui suit « À ajouter » dans PATCH.md (patch, `nom` =
  la skill visée) ou REMPLACE.md entier (remplace, `nom` = en-tête « # Brouillon remplace : <nom> » de BROUILLON.md) ; `empreinte` = 16 hex du SHA-256 de JSON `[mode, nom, texte]` ; `complet` faux tant que « A COMPLÉTER » reste
  (carte « brouillon à compléter », sans ✅) ; `trop_long` = plus de 300 lignes ou une ligne de plus de 500 caractères. `habitudes voir
  --dossier D` = un brouillon.
- `ultron-hub habitudes installer --dossier D --empreinte E` : jeton `habitude` signé sur `<D>:<E>` (ou `--i-am-human`), anti-rejeu
  `var/habitudes/installees.jsonl`, sous verrou ; refus (rien installé) si : empreinte différente, incomplet, trop long, nom hors
  `[a-z0-9-]+`, `synced` ou `fin-de-chantier`, dossier hors `~/ultron/propositions-skills` ou lien symbolique (dossier, fichier du
  brouillon, skill visée), skill déjà existante (nouvelle) ou absente (patch, remplace). Nouvelle = `~/.claude/skills/<nom>/SKILL.md` (0644,
  dossier 0755, jamais écrasé) ; patch = ajout en fin du SKILL.md sous `## Ajout du <date> (brouillon <D>)` ; remplace (#448) = SKILL.md entier remplacé, ancien gardé en `SKILL.md.bak-<D>` (refus si cette copie existe déjà), empreinte sur le texte complet, résumé « Remplacement de la skill « X » ». Puis BROUILLON.md passe
  à « Statut : APPLIQUÉ … » et le dossier va dans `appliquees/` ; ligne au journal commun.
- `ultron-hub habitudes jeter --dossier D` (simple, non signé) : « Statut : JETÉ … », dossier déplacé dans `jetees/`, rien supprimé.
- Catalogue : `habitudes` (lecture), `habitude-voir` (lecture, champ `dossier`), `habitude` (signée, `detail = habitude-voir`),
  `habitude-jeter` (simple). `detail` accepte désormais une lecture qui prend des champs de l'action (mêmes noms) et rend `lignes` :
  la feuille les montre telles quelles (`detail_brut`, bloc préformaté), refus si l'empreinte a changé ou si le texte dépasse 300
  lignes (`habitudes.LIGNES_MAX`, jamais tronqué). Chemins remplaçables par `ULTRON_PROPOSITIONS_SKILLS_DIR` et `ULTRON_SKILLS_DIR` (tests).
- Tests : `tests/test_habitudes.py`. Capture : `moi-*` (aperçu : une habitude complète, une à compléter).

## Routine proposée (todo #503)
Une carte « 🔁 routine proposée » par routine de `~/ultron/routines/proposees/`, dans À toi juste sous les habitudes (rang 8.5),
absente s'il n'y en a aucune : phrase, cadence (et prochaine échéance), but, critères de fin, plafond, chantier en référence.
✅ Activer… = action signée `routine` sur `<nom>:<empreinte>` ; la feuille relit `routine-voir` et liste ses `lignes` telles
quelles (empreinte changée = refus). ❌ Refuser… = `routine-refuser` (simple). Rien n'est lancé. Page : `carteRoutine`,
`loadRoutines` / `showRoutines` dans `app.js`. Todo #539 : une carte « 🔁 routine active » (rang 8.6, `carteRoutineActive`) avec
🛑 Retirer… = `routine-retirer` (simple, non signée : ça retire du travail) ; aperçu `routines` / `routine-voir` ; captures `atoi-routine`, `feuille-routine`.
Détail des commandes : `docs/objectifs.md`, section « Routines (todo #503) ».

## Refonte 3 onglets (todo #466)
Spec : `~/notes/recherche-dashboard-2026-10-28.md` §4, §6, §7 (points 1 à 11). Rendu final direct (pas de maquette). Remplace la structure
de « Style terminal et poste de commandement » (#281) ; catalogue, feuilles, jetons et signatures inchangés (aucune route ajoutée).
- **3 onglets** : `à toi` (id `briefing`, ouvert par défaut), `en cours` (id `travail`), `fil`. Les ids de #270 sont gardés (liens des
  notifications, `sw.js`, `push.py`). **Moi** n'est plus un onglet : icône `@` en haut à droite (`#ouvrir-moi`, `aria-pressed`) ; un
  second appui revient à À toi. Moi = ton cap (focus, revue de cap, profil), chantiers, toutes les tâches (ménage, ranger, ajout, état
  des fusions), quota et comptes, santé, notifications. **Un seul badge** : le nombre de cartes d'À toi (plus de badge Fil ni de point
  sur En cours). `sw.js` : `fil` ajouté aux onglets, cache `v5`.
- **À toi = file unique de décisions** (`dessinerAToi`), une carte par décision, triée par urgence : question bloquante, compte à
  reconnecter, session qui attend ta réponse, question, branche à intégrer, après fusion, todo livrée/terminée, fait de profil
  proposé, nouvelle habitude, rapports du dimanche. Sources, toutes du serveur : items À toi de `/api/state` ; lectures `sessions`,
  `comptes` (`a_reconnecter`), `profil` (`pending`), `habitudes`, `rapports`, et `etat-fusions` pour ce que change une branche (au
  plus une fois par minute chacune ; lecture en échec = pas de carte). Carte : tête (genre · chantier en clair, réf. en gris mono),
  titre, puis SON contenu : dernier commit et nombre de commits, raisons de refus avec les fichiers, « intégrée seule le … » ; la phrase
  générique de `catalogue.DECISIONS` seulement à défaut. Boutons : « Approuver… » = action `approuver` par `prepare` (feuille signée
  inchangée), question = champ de réponse dans la carte, « Voir » en texte. Seule la PREMIÈRE carte a le bouton plein vert.
  « Tout intégrer d'un coup (N)… » (`openLot`, `fusion-lot` signé) en tête dès 2 branches. Les cartes Rapports et Habitude ont quitté Moi.
- **En cours** (l'écran Session étendu) : bandeau `#etat` (santé, quota, sessions, agents, et en rouge « 🔑 n compte(s) à reconnecter » :
  une alerte monte au bandeau), sessions une ligne chacune, **état d'abord** (« attend ta réponse » en ambre, travaille, au repos), puis
  le chantier en clair, l'heure en gris ; « Gérer les sessions » (texte) ouvre la liste avec Fermer/Nouvelle. Fait aujourd'hui. Puis
  **« Demander à Ultron »** : un sélecteur (sessions ouvertes, celle qui attend d'abord ; nouvelle session Main ou par chantier ;
  demande todo + plan) au-dessus d'UN champ et d'UN bouton plein. Session ouverte = `session-ecrire` (ouvert, sans feuille) puis le
  Pilote s'ouvre ; nouvelle session = `session-nouvelle` (feuille signée, risque élevé donc bouton rouge de la feuille, règle #269) ;
  demande = `demande`. Le formulaire n'est jamais redessiné pendant la saisie. 📎 Joindre en texte dessous.
- **Fil** : un appui sur une carte non lue ouvre TOUJOURS le message (texte, pièces jointes, puis « → Voir la todo #… » seulement si
  l'entrée porte une `cible`) ; plus d'ouverture directe de la todo ni du lecteur. Filtres Questions (`genre` question ou feu-vert,
  champ `genre` ajouté à la liste blanche `NOTIFS`), Fichiers, Alertes, Tout ; ils tiennent sur 375 px. Répétitifs : 3 entrées lues
  de suite de la même famille (2 premiers mots du titre sans chiffres) = une ligne repliée « HH:MM–HH:MM N× titre ».
- **Style** : texte courant en police proportionnelle du système (`--font-texte`), chasse fixe (`.mono`, `time`, `.val`, `.badge`,
  `.barre`, glyphes, terminal, invite du Pilote, `#maj`) pour heures, chiffres et codes. Boutons à 3 niveaux : `.confirm` plein vert
  (principal, un par écran), `.go` contour (secondaire), `.texte` texte seul (tertiaire, 44 px mini) ; rouge = action `danger` du
  catalogue seulement. `form(id, ctx, listes, niveau)` choisit le niveau. Le « > » des boutons `.go` est retiré. Sombre et vert gardés.
- **Fiche todo** : décision en haut (titre, `#id · chantier` en mono gris, en clair, notes, Fusionner ou Terminer plein, Confier,
  ↶), « Ce qui change » (commits), puis repliés : Détails (statut, échéance, missions), Historique, Modifier ; Abandonner (rouge) et
  Actions après fusion (texte) en bas.
- **Fiche chantier** : où en est, Reprise repliée, todos (un appui ouvre la fiche, plus de ✅ Terminer dans la liste : clore reste un
  geste conscient), questions avec leur réponse, « + Ajouter une todo » replié, « Archiver… » en texte tout en bas ; la remarque pour
  l'escouade est retirée (escouades en pause).
- **Analyser** : UN champ lien + le choix de l'action (Analyser la vidéo par défaut, Durée, Collecter : entrées
  `video-tout`, `video-duree`, `video-collecte`), ta phrase seulement si l'action la prend, un bouton plein.
- Aperçu : 3 lignes « main a avancé » (regroupées), une cible et un `genre` question dans les faux messages. Captures : onglets + `moi`,
  `atoi-bas`, `encours-bas`, `moi-bas`, `feuille-approuver`, `fiche-chantier-bas`, `fil-message`, `fil-fondue`, `fil-groupe`, `fil-questions` ;
  `selectTab('moi')` remplace l'onglet Moi. Budget relevé : 192 Ko (page) et 194,5 Ko (servi). Tests : `tests/test_dashboard.py`
  (`test_three_tabs_a_toi_en_cours_fil_and_moi` remplace `test_command_post_home_screen`, `test_fil_tap_opens_the_message_and_filters`,
  `test_chantier_card_has_no_squad_remark_nor_close_in_list`).
- Reste connu : les textes serveur qui disent encore « onglet Moi » pour les rapports, l'habitude et la bascule de compte
  (`rapports.py`, `habitudes.py`, `todo/closing.py`, `compte/alertes.py`) ; « Vidéo prête : <id> » sans titre ; la cible du Fil reste
  devinée côté serveur (`notifs.cible_depuis`, testée) : la page l'affiche seulement si elle existe.

## 🌙 Bilan du soir (todo #530)
`ultron-hub bilan soir [--date AAAA-MM-JJ]` (lecture seule, JSON, `src/ultron_hub/cap/bilan_soir.py`) : les fusions et clôtures faites SEULES
dans la journée locale, lues dans la table `events` (`details.by` commence par `auto:` ; `livree` = fusion, `status` vers done = clôture).
Une ligne par élément : `{id_evenement, heure, todo_id, titre, sorte (fusion|clôture), sha, by, annulee}` ; `id_evenement` est la clé où se
brancheront ↩️ (#531) et le compteur de gestes (#532). Entrée catalogue `bilan-soir` (lecture), exemple `apercu.LECTURES["bilan-soir"]`,
carte `carteBilanSoir` dans À toi (après les décisions, hors badge).
Todo #534 : carte « 📅 planning du jour » juste avant (hors badge, lecture `planning`, actions simples `planning-poser`, `planning-pause`,
`planning-reprendre`, détail `docs/compte.md`) ; le bilan affiche aussi `remises` (remise à zéro hebdo d'un compte). Captures `atoi-planning`, `feuille-planning`, `feuille-pause-auto`.

↩️ par ligne (todo #531) : une clôture seule appelle l'action `rouvrir` du catalogue (`todo reopen`, sans signature) ; une fusion seule l'action
signée `annuler-fusion` (`branch revert-last`, cible `<id>-<sha12>`), seulement si c'est la dernière fusion automatique (`fusion-auto`) et non
annulée ; sinon le motif remplace le bouton. Une ligne déjà `annulee` n'a pas de bouton. Aucune route nouvelle.

Gestes évités (todo #532) : `bilan soir` rend aussi `faits` (lignes du jour) et `gestes_evites` (un ✅ par clôture seule, un 🔀 par fusion seule, moins
les lignes annulées par ↩️ : ce geste, il a fini par le faire). La carte affiche « N gestes évités, M faits » avec ces deux champs, sans rien recalculer.
