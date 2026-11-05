# Bot Telegram direct, sans Hermes

Carte : `src/ultron_hub/bot/run.py` (Application PTB, ctx minimal, masquage du jeton), `src/ultron_hub/bot/commands.py`
(`ultron-hub bot run`), `deploy/systemd/ultron-bot.service` (non activée), `tests/test_bot_run.py`. Spec :
`~/notes/spec-porte-directe.md` (chantier porte-directe ; étapes #193 à #197).

## Lancer le bot à nous (todo #193)
- `ultron-hub bot run` : long polling au premier plan jusqu'à SIGTERM/SIGINT (`run_polling`, arrêt propre `stop` puis
  `shutdown`), `bootstrap_retries=-1` (réessaie sans fin au démarrage si le réseau manque ; ensuite l'Updater de PTB réessaie
  seul après une coupure). Mises à jour traitées une par une, comme sous Hermes ; les `block=False` du plugin restent valables.
- Il appelle le même `hermes_plugin.register(ctx)` que Hermes avec `BotContext` : `register_platform_handler("telegram",
  _wire)` est gardé, `_wire(application, None)` est appelé dans `post_init` (boucle lancée : relance des questions et
  « ✅ Bot reconnecté. » partent comme sous Hermes). `register_command` est noté sans effet : le menu `/` reste celui posé
  chez Telegram par Hermes. `register_hook` et `register_system_prompt_section` sont ignorés (journalisés au démarrage) : ils ne
  servent qu'à la conversation de Hermes (le plugin n'en enregistre plus depuis le todo #463).
- `hermes_plugin/` est importé depuis la racine du projet (il n'est pas dans `src/`) ; python-telegram-bot est l'extra
  `bot` du `pyproject.toml` (22.x, comme Hermes), à installer dans le `.venv` avant la bascule.
- **Jeton** : seulement `TELEGRAM_BOT_TOKEN` de l'environnement, donné par systemd (`EnvironmentFile=%h/.hermes/.env`).
  ultron-hub ne lit aucun fichier de secrets (test). Le jeton n'est jamais écrit ni journalisé : httpx/httpcore à WARNING
  (leurs lignes INFO contiennent l'URL `…/bot<jeton>/…`) et `TokenRedactor` le remplace par `<jeton masqué>` dans tout
  enregistrement (message, arguments, trace). Le gestionnaire d'erreurs ne journalise que le type de l'exception.
- **Autorisés** : `hermes_plugin.allowed_user_ids()` (`TELEGRAM_ALLOWED_USERS`, même fichier d'environnement), les mêmes
  `filters.User` que sous Hermes. Liste vide = refus de démarrer. Un commentaire en fin de ligne (`7 # moi`) est ignoré :
  python-dotenv (Hermes) le retire, systemd `EnvironmentFile` (ultron-bot) le garde (panne du 18/10, todo #212).
- Refus de démarrer (code 78, `RestartPreventExitStatus=78` : pas de relance en boucle), message clair dans le JSON de stdout
  et dans le journal : jeton absent, autorisés vides, python-telegram-bot absent.
- Journal : stderr, donc `journalctl --user -u ultron-bot`.
- Unité `ultron-bot.service` : `Conflicts=hermes-gateway.service` (Telegram n'accepte qu'UN lecteur par bot), installée
  nulle part, ni activée ni démarrée ; la bascule et le retour sont la todo #197.

## Bot de secours (todo #463)
Le plugin ne branche plus que `/start /atoi /plus /todo /sante /usage /telegram` (voir `docs/telegram.md`). Retirés : vocaux,
texte libre (3 boutons), 📥 Analyser, 🚀 Demande, montage, etc. `BotContext.standalone` et `voice_transcriber` subsistent mais le
plugin ne les lit plus (`bot/voice.py` n'est plus appelé par un flux, à retirer avec son extra `voice` si on veut finir le ménage).

## Bascule et retour (todo #197)
Telegram n'accepte qu'un lecteur par bot : `ultron-bot` et `hermes-gateway` (unité utilisateur) ne tournent jamais ensemble.
- **Bascule** : `scripts/bascule-bot.sh` installe les extras (`pip install -e '.[bot,voice]'`), lie `deploy/systemd/ultron-bot.service`
  dans `~/.config/systemd/user`, pose le mode d'envoi `direct` dans `var/envoi-mode` (hors git), arrête `hermes-gateway`, démarre `ultron-bot`,
  puis observe 10 s (`BASCULE_VERIF_S`) : unité active et journal sans traceback / erreur / conflit. En cas d'échec, il appelle
  lui-même `retour-hermes.sh` (code de sortie 1).
- **Retour** : `scripts/retour-hermes.sh` supprime `var/envoi-mode` (retour au mode `hermes`), arrête `ultron-bot`, relance `hermes-gateway` et vérifie qu'il est actif.
- Les deux sont idempotents ; `--dry-run` affiche les actions sans rien faire. Si `hermes-gateway` était une unité système,
  le script affiche `! sudo systemctl stop|start hermes-gateway.service` (code 3) au lieu de l'exécuter.
- Le jeton n'est jamais lu par les scripts : l'unité le reçoit de systemd (`EnvironmentFile=%h/.hermes/.env`) ; `~/.hermes` n'est pas touché.
- Mode d'envoi : `ULTRON_ENVOI_MODE` > `var/envoi-mode` > `envoi.mode` de `config/settings.yaml`. `var/` est ignoré par git et lu par
  tous les envoyeurs (bot, timers, escouade, missions) : `config/settings.yaml` n'est jamais modifié, main reste propre.
- Contrôle préalable (todo #211) : avant tout arrêt de Hermes (et en `--dry-run`), `systemd-run --user -p EnvironmentFile=…` vérifie que `TELEGRAM_BOT_TOKEN` et `TELEGRAM_ALLOWED_USERS` sont non vides tels que systemd les voit (code de sortie seul, aucune valeur lue) ; sinon « bascule refusée » et Hermes intact.
  Surcharges de test : `BASCULE_ENV_FILE`, `BASCULE_SYSTEMD_RUN`.
- Tests : `tests/test_bascule.py` (faux `systemctl`, `journalctl`, `pip`, `systemd-run` dans le PATH).

## Ce qui reste à Hermes (dépendances relevées le 18/10)
- Envoi sortant : `hermes send` dans les modules d'ultron-hub (garde, escouade, ménage, esprit, souvenirs, branches…) : #194.
- Vocaux et textes libres : le plugin ne les prend plus ; sous Hermes ils lui reviennent, sous `ultron-bot` personne n'y répond (secours strict).
- `hermes_will_announce()` est forcé à faux par `bot run` (Hermes ne tient plus Telegram : l'annonce est toujours la
  nôtre) ; sous Hermes il lit toujours les marqueurs de redémarrage de `HERMES_HOME`. `allowed_user_ids()` essaie d'abord
  `gateway.platforms._shared.platform_gate_env` (absent hors Hermes : repli sur l'environnement).
- `HubClient` lance `.venv/bin/ultron-hub` avec un environnement minimal : le jeton n'atteint pas la CLI.
