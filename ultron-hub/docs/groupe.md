# Groupe Telegram à sujets

**Carte** : `src/ultron_hub/groupe/tableau.py` (tableaux épinglés, todo #202), `groupe/commands.py` (`ultron-hub groupe tableau`) ;
tests `tests/test_tableau.py`. Ailleurs : `/groupe` = `hermes_plugin/groupe_flow.py` (écrit `config/groupe.yaml`), envoi dans un
sujet = `envoi.py` (voir `docs/envoi.md`). Spec : `~/notes/spec-groupe-sujets.md` §6.

## Tableau épinglé par sujet (todo #202)
- Un message par sujet de domaine (Système, Business, Perso) : todos ouvertes des chantiers du domaine (domaine lu dans le
  fichier du sujet, `Domaine : …`), branches `todo-*` en avance sur main (🌿 prêtes), quota `usage` en une ligne, heure.
  Les todos sans chantier n'apparaissent dans aucun tableau.
- Envoyé une fois (`sendMessage` dans le sujet), épinglé sans notification (`pinChatMessage`), ensuite modifié sur place
  (`editMessageText`). Rien n'est appelé si le texte n'a pas changé (l'heure ne compte pas).
- Ids gardés dans `var/groupe/tableau.json` (jamais dans `config/groupe.yaml`). « message to edit not found » = recréé et
  réépinglé ; « message is not modified » = ok ; autre erreur (réseau, quota) = signalée, l'id est conservé. Épinglage refusé =
  réessayé au passage suivant.
- **Mode `hermes` ou groupe absent : ne fait rien** (`status: skipped`, résumé clair). Jeton lu dans l'environnement, jamais journalisé.
- **Quand** : `ultron-hub groupe tableau` à la main ; automatiquement par `menage report` et après `todo add/done/cancel/reopen`
  (au mieux, n'échoue jamais ; pas de timer dédié). Limite : un message supprimé à la main n'est vu qu'au prochain changement de texte.
- **Nom du sujet = état (todo #207)** : après le tableau, le sujet est renommé (`editForumTopic`) « 🛠 Système · 2 prêtes »
  (nombre de branches 🌿 prêtes du domaine, même calcul que le tableau ; 0 prête = nom de base `🛠 Système`, `💼 Business`,
  `🏠 Perso`, comme créés par /groupe). Seulement si le nom voulu diffère du dernier posé (`nom` dans `tableau.json`) et si le
  dernier renommage date d'au moins 600 s (`nom_at`). « TOPIC_NOT_MODIFIED » = ok ; autre erreur = signalée, nom non mémorisé
  (réessayé), sans gêner le tableau ni les autres sujets.

## Un sujet par chantier (todo #203)
- Code : `src/ultron_hub/groupe/chantiers.py` (`creer`, `fermer`), hooks dans `escouade nouveau` (création), `chantier archiver` et
  `chantier rouvrir` ; tests `tests/test_groupe_chantiers.py`, `tests/test_groupe_flow.py`.
- **Création** d'un chantier : `createForumTopic`, nom = icône du domaine (🛠/💼/🕌, ❔ si pas de ligne `Domaine :`) + nom du chantier ;
  l'id est rangé dans `config/groupe.yaml` sous la section `chantiers:` (`nom: id`), à côté de `sujets:`.
- **Archivage** : `closeForumTopic` ; l'id reste noté, `chantier rouvrir` fait `reopenForumTopic` sur le même sujet.
- **`/groupe`** crée aussi les sujets des chantiers existants non archivés (fichiers de `~/notes/prompt`, hors `_socle`), idempotent ;
  une erreur sur un chantier est signalée sans masquer les sujets de domaine.
- **Envois** : voir `docs/envoi.md` (`chantier=`) : sujet du chantier s'il existe, sinon celui du domaine.
- **Mode `hermes` ou groupe absent : rien ne change** (`status: skipped` dans `data.groupe`). Une erreur Telegram ne casse jamais la
  commande de chantier : elle est dans `data.groupe` (`status: error`). Le sujet n'est pas renommé si le domaine change ensuite.

## Texte libre dans le sujet d'un chantier (todo #204)
- Un texte libre écrit dans le sujet d'un chantier (`sujet.chantier_of`, lu dans `config/groupe.yaml` section `chantiers:`, groupe
  noté seulement) reçoit l'offre 🚀 Demande / 📥 Analyser / 📝 Todo avec la ligne « Chantier : <nom> ». 🚀 passe
  `mission demande --chantier <nom>` (la todo créée est rangée dans le chantier), 📝 ouvre le formulaire `addc:<nom>`
  (`todo add --chantier`). Hors d'un sujet de chantier (domaine, Entrée, message privé) : comportement inchangé.

## Un sujet par mission (todo #206)
- Code : `src/ultron_hub/groupe/missions.py` (`ouvrir`, `fermer`, réutilise `chantiers._contexte` et `envoi.appeler`), branché dans
  `missions/engine.py` (`open_topic`, `close_topic`) ; tests `tests/test_groupe_missions.py`.
- Au passage en `running` (`mission execute`, escouades de type `squad` comprises) : `createForumTopic` « 🚀 #<todo> <titre court> »
  (40 caractères de titre au plus). L'id est gardé dans l'état de la mission (`mission["topic"]` de `mission.json`), **pas** dans
  `config/groupe.yaml` : un sujet de mission est éphémère, rien à nettoyer dans le fichier.
- Le bilan de fin (et celui d'une mission orpheline) part dans ce sujet (`envoi.envoyer(..., thread=<id>)`, prioritaire sur `sujet=`
  et `chantier=`) ; si Telegram le refuse (sujet supprimé), nouvel essai dans le sujet « missions ». Après chaque étape réussie
  sauf la dernière (todo #220), un court message « ✅ Étape i/n : <titre> » + résumé (200 caractères au plus) part dans ce sujet,
  sans repli vers « missions » et sans sujet = rien ; un échec d'envoi est journalisé (WARNING) et ne gêne pas la mission.
- `closeForumTopic` à la fin : succès, échec, orphelin ou arrêt (`mission cancel`).
- Repli : mode `hermes`, groupe absent ou création refusée = pas de `topic`, tout reste dans « missions ». Aucune exception ne remonte.
- Bilans de passation des escouades (`escouade/garde.py`) : inchangés, ils vont dans « missions » (ou le sujet du chantier).

## Questions d'escouade dans le sujet du chantier (todo #210)
- `escouade questions --a-envoyer` ajoute `destination: {chat_id, thread}` à chaque question quand le mode d'envoi est `direct` et
  qu'un sujet existe (celui du chantier, sinon celui de son domaine, `envoi.destination_sujet`). Sinon pas de clé : comportement
  d'avant (message privé à chaque utilisateur autorisé). Mode `hermes` : rien ne change.
- Le plugin (`QuestionPusher`, `send_question`) envoie alors UN message dans le sujet (`message_thread_id`). Si l'envoi dans le
  groupe échoue, repli sur les chats privés. Rappel et « décidée seule » vont au même endroit (mémoire du processus).
- Boutons, callbacks et signatures inchangés ; l'id marqué par `question-envoyee` reste celui du message envoyé. Le plugin n'importe
  jamais `ultron_hub` : la destination vient du JSON de la CLI, validée (entier de chat, sujet entier > 0) sinon ignorée.
- Autorisation : par identifiant d'utilisateur (`filters.User`), donc les boutons et la réponse « en répondant au message » marchent
  dans le groupe (le filtre `_ReplyToQuestion` ne regarde pas le type de chat).

## `ultron-hub groupe creer` (todo #246)
Crée les sujets manquants (domaines puis chantiers non archivés), idempotent, mode direct et `chat_id` noté requis
(`src/ultron_hub/groupe/sujets.py`, tests `tests/test_groupe_sujets.py`). Appelé par le dashboard (`docs/dashboard.md`).
