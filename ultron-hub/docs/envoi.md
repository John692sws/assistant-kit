# Envoi Telegram commun (todo #194)

Spec : `~/notes/spec-porte-directe.md` §3 étape 2 et §4. Code : `src/ultron_hub/envoi.py` ; tests : `tests/test_send.py`.

- **Un seul point d'envoi** : `envoi.envoyer(texte, target="telegram", binary=None, style="arg"|"stdin")` renvoie un
  `Resultat` (`status` ok/error, `detail`, `envoyes`) et ne lève jamais. Appelants : `todo.service.send_message`
  (digest, missions, santé, ménage, esprit, veille), `escouade.garde.alerter` (alertes et bilans, `MEDIA:<chemin>`
  compris), `escouade.hooks.send_telegram` (jauge de contexte ; fichier figé en bibliothèque standard : passe par la
  commande `ultron-hub send`), `branches.restart_gateway` (l'unité transitoire appelle `ultron-hub send` en mode direct).
- **Telegram coupé par défaut** (todo #290) : `envoyer` saute l'envoi Telegram (résultat `ok`, `detail` « envoyé au Fil (push : N appareil(s)) » ou « (push absent) », jamais « coupé » : les sessions le lisaient comme un échec, todo #447,
  `envoyes` 0) ; le Web Push et l'historique du Fil (`var/notifs`) restent. Passent toujours : sujet `alertes` et tout message
  avec `cle_alerte` (alertes de panne). Les réponses du bot aux commandes ne passent pas par `envoyer` : jamais coupées.
  État : `var/telegram-etat` (`on`/`off`, absent = coupé), variable `ULTRON_TELEGRAM` prioritaire (les tests la posent à `on`).
  Commandes : `ultron-hub telegram on|off|etat`, `/telegram on|off` sur le bot, entrées `telegram-activer` / `telegram-couper`
  du catalogue du dashboard.
- **Sujets du groupe** (todo #199) : `envoyer(..., sujet="missions")` ou `ultron-hub send --sujet missions` range le message
  dans le sujet du groupe à sujets (`config/groupe.yaml`, écrit par `/groupe`) : en mode direct, `chat_id` du groupe et
  `message_thread_id` dans `sendMessage` / `sendDocument` etc. Noms acceptés : systeme, business, perso, missions, alertes,
  revue-de-cap (casse et accents libres). **Mode `hermes`** : `hermes send` ne connaît pas les sujets, le message part en
  conversation privée comme avant. Groupe absent, fichier absent ou illisible, sujet inconnu = conversation privée, sans erreur.
  Table : missions -> Missions ; garde (`garde.alerter`), santé (`health alertes`), veilleur -> Alertes ; revue de cap
  (`cap report --send`) -> Revue de cap ; digest, ménage, esprit -> privé (pas de sujet).
- **Boutons** (todo #201) : `envoyer(..., boutons=[[(libellé, callback_data), ...], ...])` (`todo.service.send_message(..., boutons=...)`
  les relaie). **Mode direct** : `reply_markup` `inline_keyboard` sur le DERNIER morceau de texte (jamais sur une pièce jointe) ; un bouton
  mal formé (libellé vide, callback_data vide ou > 64 octets, clavier vide) = statut `error`, rien n'est envoyé. **Mode `hermes`** : texte
  seul, inchangé. `ultron-hub send` n'a pas d'option boutons. Depuis le todo #463 (bot de secours) **aucune alerte n'en porte** :
  `src/ultron_hub/boutons.py` ne fabrique plus rien (carte de fusion, revue de cap et relance de veille retirées du bot) ; branche prête,
  revue de cap et veille en pause s'envoient en texte et la décision se prend sur le dashboard. Le mécanisme reste pour un futur
  bouton dont le `callback_data` existerait côté plugin.
- **Sujet de chantier** (todo #203) : `envoyer(..., chantier="nom", sujet="systeme")` ou `send --chantier nom` : en mode direct, le message
  va dans le sujet du chantier (section `chantiers:` de `config/groupe.yaml`) s'il existe, sinon dans celui du domaine `sujet`, sinon
  en privé. Rattaché : alertes et bilans d'une escouade (`escouade/garde.py`, chantier = `sujet` de l'escouade).
- **Commande** : `ultron-hub send [--sujet <nom>] [--chantier <nom>] [-t telegram|telegram:<chat>|<chat>] ["texte"]` (stdin si pas de texte), sortie JSON.
- **Réglage** : `envoi.mode` dans `config/settings.yaml` (ou variable `ULTRON_ENVOI_MODE`) :
  - `hermes` (défaut) : `hermes send` exactement comme avant ;
  - `direct` : Bot API Telegram en HTTPS (`urllib`), sans Hermes. La bascule réelle est la todo #197.
- **Repli (todo #215)** : seul `ultron-bot` reçoit le jeton (EnvironmentFile). En mode direct sans jeton dans l'environnement
  (timers, missions, sessions), `envoyer` passe par `hermes send` (marche sans le gateway) : texte seul, conversation privée.
- **Mode direct** : jeton dans `TELEGRAM_BOT_TOKEN` (environnement, jamais dans un fichier du dépôt, jamais journalisé :
  il est masqué dans toute erreur) ; destination : `envoi.chat_id`, sinon `ULTRON_TELEGRAM_CHAT_ID`, sinon
  `TELEGRAM_HOME_CHANNEL`. Texte en `sendMessage`, découpé en morceaux de 4096 caractères au plus (coupe sur fin de
  ligne) ; chaque ligne `MEDIA:<chemin>` est une pièce jointe (photo, vidéo, vocal `.ogg`, audio, sinon document),
  envoyée après le texte. Un échec (réseau, refus Telegram, fichier absent) donne le statut `error`.
- **Web Push (todo #240)** : après Telegram, `envoyer` pousse aussi un résumé nettoyé vers le dashboard installé sur
  l'iPhone s'il est abonné (`dashboard.push.notify`, détail dans `docs/dashboard.md`) ; un échec push ne change jamais le résultat.
- **Tests** : `tests/conftest.py` interdit tout appel réseau (`envoi._urlopen` échoue) ; un test fournit son faux.
- **Alerte réglée (todo #208)** : en mode direct seulement, `health alertes` garde l'id du message Telegram (renvoyé par
  `sendMessage`) par clé d'alerte dans `var/health/alertes-messages.json` (hors git). Quand l'alerte est vue réglée,
  `envoi.appeler("editMessageText")` change le message en « ✅ réglée à HHhMM » + texte d'origine, puis la clé est
  oubliée (si un même message couvre plusieurs alertes, il n'est modifié que quand elles sont toutes réglées). Mode
  `hermes` : rien ne change. Une erreur Telegram (message supprimé, trop vieux) est journalisée, jamais remontée.
  Seul le contrôle de santé est concerné (le veilleur et la garde n'ont pas de notion d'alerte réglée).

## Alerte réglée sur place (todo #219)

Mécanisme commun, pour les alertes autres que `health alertes` (qui garde son suivi propre, todo #208) :
- `envoyer(..., cle_alerte="<clé stable>")` (aussi `service.send_message`) : en mode direct, `envoi.enregistrer` garde
  `{chat_id, message_id, texte (première ligne), envoyee_a}` sous la clé dans `var/envoi/alertes.json` (écriture atomique).
- `envoi.marquer_reglee(clé)` : `editMessageText` une fois, le message devient « ✅ réglée à HHhMM » suivi de la première
  ligne d'origine, puis la clé est oubliée. « message to edit not found » ou « message can't be edited » = oubliée sans
  erreur ; toute autre erreur est journalisée et la clé reste pour un nouvel essai. Ne lève jamais. Mode `hermes` : rien.
- Branché : l'alerte « Veille en pause après 3 échecs » (clé `veille:<nom>:pause`), réglée par `ultron-hub veille resume <nom>`.
  `veille remove <nom>` oublie la clé (`envoi.oublier`) sans toucher au message : la veille n'existe plus.
  Une veille qui annonce une nouveauté n'a pas d'état « réglé » : non branchée. Le garde des escouades n'a pas de point
  de résolution net : à brancher avec `cle_alerte` et `marquer_reglee` quand il en aura un.
- Tests : `tests/test_alerte_reglee.py`.

## Genre d'une notification (todo #343)

- `ultron-hub send --genre feu-vert|question|alerte|fait|bilan|fichier` : le genre est gardé dans l'entrée de
  `var/notifs/historique.jsonl` (`envoi.envoyer(..., genre=)`). Genre inconnu = statut `error`, rien n'est envoyé.
- Sans `--genre`, `notifs.deduire_genre(texte)` (script pur, premier cas qui correspond) : « prête » ou 🔀 = `feu-vert` ;
  « tu décides » ou « ? » final = `question` ; 🚨 ou ❌ = `alerte` ; ligne `MEDIA:` = `fichier` ; « bilan » ou
  « En clair » = `bilan` ; sinon `fait`. Un genre invalide passé à `noter` est ignoré (déduit).
- `notifs.liste` (donc l'API du Fil) renvoie `genre` pour chaque entrée ; une ancienne entrée sans genre est déduite à la lecture.
- Titre et résumé (todo #344) : `notifs.liste` renvoie `titre` et `resume`. Si le titre gardé est vide ou « Ultron », `titre`
  est la 1re phrase du message (jusqu'à `.` `!` `?` ou fin de ligne), sans émoji ni « #340 » de tête, sans ligne `MEDIA:`,
  coupée au mot à 60 caractères (« … ») ; `resume` = la suite, 140 caractères au plus. Calculés à la lecture (anciennes
  entrées comprises), après le masquage des chemins et jetons. Fonctions pures `titre_sens` et `resume_sens`.
- Redémarrages repliés (todo #348) : « ✅ Le bot redémarré (fusion de #N) » n'est plus une ligne de `notifs.liste` ; il devient
  `etiquettes: ["bot redémarré"]` sur la fusion la plus proche avant lui qui cite la même todo (sinon la fusion la plus proche
  dans les 30 min). Sans fusion trouvée, ou en échec (❌, « échec »), il reste une ligne. Replié, il est lu avec sa fusion
  (pas de non-lu caché). Fonctions pures `redemarrage_todos` et `replier_redemarrages`.
- Tests : `tests/test_notifs.py`.

## Tests et garde-fou (todo #393)

Aucun test n'envoie un vrai message : la fixture autouse `no_real_hermes` (tests/conftest.py) met un faux `hermes` en tête de PATH
(et dans ULTRON_HUB_HERMES_BIN), retire le jeton, et fait échouer le test en teardown si ce faux binaire est appelé
(« test sent a real Telegram message »). Marqueur d'exemption : `sends_telegram`. `fil_seul=True` : push et Fil, rien sur Telegram.
