# Bot de secours Telegram : À toi, /todo, /sante, /usage

Réduit le 29/10/2026 (todo #463) : le dashboard fait tout, le bot ne sert que si le dashboard tombe. Il n'a plus de fusion, de cap,
de veille, de profil, de vidéo, de documents, de montage, de chantiers, de ménage, de groupe à sujets ni de texte libre ; ces flux
ne sont plus que dans le dashboard et la CLI (`docs/dashboard.md`).

## Ce qui reste
- **📥 À toi** (`hermes_plugin/atoi_flow.py`, préfixe `hub:at:`) : `/start`, `/atoi` et 🏠 Accueil. Texte seul tiré de `chantier atoi`
  (8 par page, `list` / `p:<n>`), une ligne d'état (`usage` : session et semaine, best effort) et « N branche(s) prête(s) à fusionner »
  (`branch lot`) ; aucun bouton de décision, la dernière ligne renvoie au dashboard (« Pour répondre, fusionner ou clore : le dashboard »).
  `⋯ Plus` (`/plus`, `hub:at:plus`) = 🎯 Toutes les todos · 🩺 Santé · 📊 Usage.
- **/todo** (`todo_flow.py`, `hub:todo:list:<page>`) : les todos ouvertes en lecture seule (`todo board`, 10 par page), sans fiche ni formulaire.
- **/sante** (`health_flow.py`) : santé, batterie et réflexes de dépannage (carte d'approbation, ✅ signé par `close_key`, action `reflexe`).
- **/usage** (`usage_flow.py`) : limites Claude. **/telegram [on|off]** : coupe ou rétablit les envois non urgents (réglage de la CLI).
- **Alertes** : `ultron-hub send` envoie du texte, sans bouton (`boutons.py` ne fabrique plus rien ; revue de cap et veille en pause
  disent la commande à taper). `cle_alerte` (alerte réglée = message retiré) est inchangé : voir `docs/envoi.md`.
- Pied d'écran, verbes figés, carte d'approbation et « (écran remplacé) » : `hermes_plugin/ui.py`, voir CLAUDE.md « Bot de secours ».

## Commandes
`PLUGIN_COMMANDS` (`deploy/user/ultron-user`) = `todo sante usage atoi plus start`. `start` est une commande intégrée de Hermes que le
plugin prend en charge avant lui (pas de `register_command`). Les anciennes commandes (`/escouade /profil /analyser /video /montage
/chantiers /menage /cap /groupe`) ne répondent plus ; les déclarations `apres-fusion/` qui les citaient (todos 56, 74, 94, 109, 151,
174…) sont de l'histoire déjà jouée.

## Tests
`tests/test_atoi_flow.py`, `test_hermes_plugin.py` (/todo, allowlist, HubClient, isolation sans `ultron_hub`, message de reconnexion),
`test_ui.py` (crawl de tous les écrans), `test_cards.py` (remplacement d'écran), `test_health*.py`, `test_usage.py`, `test_envoi_boutons.py`.
