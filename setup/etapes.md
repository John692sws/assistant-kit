# Étapes de l'installation

Dans l'ordre. Chaque étape finit par sa **commande de contrôle** : KO = on s'arrête et on corrige avant la suivante.
Chaque étape est rejouable : relancée, elle ne casse rien et n'écrase rien de sa partie.
Jamais `sudo` ni `brew` lancés par l'agent : il les donne à taper, précédés de `!`.

Chaque commande de ce fichier commence par ces lignes (un shell neuf ne les garde pas) :

```sh
KIT="$HOME/assistant-kit"                          # noyau : ce dépôt
HUB="$HOME/workspace/projects/active/ultron-hub"   # code installé
ETAT="$HOME/.config/assistant-kit"                 # sa partie : réponses et gabarits remplis
R="$ETAT/gabarits-remplis"
CLI="$HUB/.venv/bin/ultron-hub"
PY="$(for p in python3.14 python3.13 python3.12 python3; do q="$(command -v "$p")" && "$q" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null && { echo "$q"; break; }; done; true)"   # premier Python 3.12+ trouvé
```

## 0. Questionnaire
Suivre `questions.md`. Il écrit `$ETAT/reponses.json` et `$ETAT/choix.json`.
Contrôle : `"$PY" -c 'import json,sys; a=json.load(open(sys.argv[1])); assert all(isinstance(v,str) and v.strip() for v in a.values()); json.load(open(sys.argv[2]))' "$ETAT/reponses.json" "$ETAT/choix.json"`

## 1. Prérequis
Python 3.12 ou plus, `git`, `tmux`, `claude` (déjà connecté : c'est lui qui lit ce fichier).
- Linux (Debian, Ubuntu) : il manque quelque chose = lui donner `! sudo apt install python3 python3-venv git tmux`.
  Python trop vieux : `! sudo apt install python3.12 python3.12-venv`.
- Mac : regarder d'abord le processeur, `uname -m`.
  - `arm64` (puce Apple M1, M2…) : `command -v brew` vide = étape manuelle M0 (installer Homebrew depuis brew.sh, la
    commande affichée sur le site). Puis `! brew install python git tmux` (la formule `python` est un Python 3.12 ou plus).
  - `x86_64` (Mac Intel) : PAS de Homebrew (Intel en « Tier 3 » depuis septembre 2026 : sans support, beaucoup d'outils se
    compilent sur place, lent et fragile ; arrêt prévu en 2027). Si `brew` est déjà là ET que `brew install tmux` marche, le
    garder ; sinon, dans l'ordre, une chose à la fois :
    1. `git` absent = `! xcode-select --install` (outils de ligne de commande d'Apple ; MacPorts en a besoin aussi).
    2. Python absent ou trop vieux = étape manuelle M0-Intel-a : sur python.org/downloads/macos, le dernier « macOS 64-bit
       universal2 installer » (.pkg), double-clic, puis « Install Certificates.command » dans la fenêtre Python ouverte.
       Rouvrir le terminal (le PATH change), relancer la détection de `PY` ci-dessus.
    3. tmux absent = étape manuelle M0-Intel-b : MacPorts depuis macports.org/install.php (le .pkg de SA version de
       macOS : `sw_vers -productVersion`), rouvrir le terminal, puis `! sudo port install tmux` (quelques minutes).
  - `/usr/bin/python3` d'Apple est trop vieux (3.9) : la détection de `PY` le saute, c'est voulu.

Contrôle : `"$PY" -c 'import sys; assert sys.version_info >= (3, 12), sys.version' && command -v git tmux claude`

## 2. Le code
```sh
mkdir -p "$(dirname "$HUB")"
[ -d "$HUB" ] || cp -R "$KIT/ultron-hub" "$HUB"
"$PY" "$KIT/setup/remplir.py" "$ETAT/reponses.json" "$HUB"
rm -rf "$R" && cp -R "$KIT/gabarits" "$R" && "$PY" "$KIT/setup/remplir.py" "$ETAT/reponses.json" "$R"
[ -f "$HUB/config/settings.yaml" ] || cp "$R/settings.yaml" "$HUB/config/settings.yaml"
[ -f "$HUB/config/dashboard.yaml" ] || cp "$R/dashboard.yaml" "$HUB/config/dashboard.yaml"
[ -x "$HUB/.venv/bin/python" ] || "$PY" -m venv "$HUB/.venv"
"$HUB/.venv/bin/pip" install -q -e "$HUB[dev]"
if [ ! -d "$HUB/.git" ]; then
  git -C "$HUB" init -q -b main && git -C "$HUB" add -A &&
  git -C "$HUB" -c user.name="$(whoami)" -c user.email="$(whoami)@localhost" commit -q -m "Installation du kit"
fi
cd "$HUB" && .venv/bin/python -m pytest -q --continue-on-collection-errors
```
- `config/settings.yaml` et `config/dashboard.yaml` ne sont pas dans le code exporté : ils viennent des gabarits remplis.
- `remplir.py` passe aussi sur le code : l'export y a mis `{{HOME}}` et `{{MACHINE}}` à la place des valeurs d'origine.
- Mac : la couche plateforme (`common/plateforme.py`) remplace systemd par launchd et lance les tests sans bwrap. Seule
  exception à la règle ci-dessous : un FAILED dont le nom ou l'erreur parle de bwrap, `/proc`, systemd ou `systemctl` est
  attendu sur Mac. Le noter (nombre et noms, pour le rapport à la fin) et continuer. Tout autre FAILED ou ERROR = KO.

- Les tests qui dépendent de fichiers hors kit (`hermes_plugin`, `deploy/user`, unités d'autres services) sont écartés ou
  ignorés par l'export : `pytest` doit passer sans erreur. Un FAILED ou un ERROR est un KO.

Contrôle : `"$CLI" --help >/dev/null && test -f "$HUB/config/settings.yaml" && ! grep -rIl --exclude-dir=.venv --exclude-dir=.git '{{[A-Z]' "$HUB" "$R"`

## 3. Sa partie : prompts, esprit, consignes
```sh
mkdir -p "$HOME/notes/prompt/demarrage" "$HOME/notes/prompt/reprises" "$HOME/ultron/esprit" "$HOME/.claude"
[ -e "$HOME/notes/prompt/_noyau.md" ] || cp "$R/socle.md" "$HOME/notes/prompt/_noyau.md"
[ -e "$HOME/notes/prompt/demarrage/orchestrateur-ultron.md" ] || cp "$R/prompt/orchestrateur.md" "$HOME/notes/prompt/demarrage/orchestrateur-ultron.md"
[ -e "$HOME/notes/prompt/reprise-orchestrateur.md" ] || cp "$R/prompt/reprise-orchestrateur.md" "$HOME/notes/prompt/reprise-orchestrateur.md"
[ -e "$HOME/notes/prompt/_socle.md" ] || cp "$R/prompt/_socle.md" "$HOME/notes/prompt/_socle.md"
[ -e "$HOME/ultron/esprit/charte.md" ] || cp "$R/charte.md" "$HOME/ultron/esprit/charte.md"
"$CLI" esprit init --profil-from "$R/profil.yaml"
[ -e "$HOME/.claude/charte.md" ] || ln -s "$HOME/ultron/esprit/charte.md" "$HOME/.claude/charte.md"
[ -e "$HOME/.claude/socle.md" ] || ln -s "$HOME/notes/prompt/_noyau.md" "$HOME/.claude/socle.md"
```
`~/.claude/CLAUDE.md` :
- absent : `cp "$R/CLAUDE.md" "$HOME/.claude/CLAUDE.md"` ;
- présent et déjà complété (`grep -qF "$(head -n 1 "$R/CLAUDE.md")" "$HOME/.claude/CLAUDE.md"`) : rien ;
- présent sans nos consignes : `cp -p "$HOME/.claude/CLAUDE.md" "$HOME/.claude/CLAUDE.md.avant-ultron"` (si cette copie
  n'existe pas déjà), puis `{ echo; cat "$R/CLAUDE.md"; } >> "$HOME/.claude/CLAUDE.md"`. Jamais d'écrasement.

`esprit init` ne remplace jamais un fichier existant ; la charte doit être posée avant lui (il la cherche sinon dans
`deploy/esprit/`, absent du kit).

Contrôle : `"$CLI" prompt mesure && "$CLI" esprit etat && test -f "$HOME/ultron/esprit/profil.md" && grep -qF "$(head -n 1 "$R/CLAUDE.md")" "$HOME/.claude/CLAUDE.md"`

## 4. Todos et chantiers
```sh
"$CLI" todo list
```
crée la base vide `var/ultron.db` à la première lecture. Puis, pour chaque chantier de `choix.json` (nom, titre, but) :
```sh
[ -f "$HOME/notes/prompt/<nom>.md" ] || printf '%s' "<but>" | "$CLI" escouade nouveau <nom> --titre "<titre>"
```
(`escouade nouveau` est la commande qui crée la fiche de sujet `~/notes/prompt/<nom>.md` d'un chantier ; elle vérifie que la
fiche s'assemble avec `~/notes/prompt/_socle.md`, posé à l'étape 3.)

Contrôle : `test -f "$HUB/var/ultron.db"`, puis pour chaque chantier `"$CLI" chantier tableau | grep -qF '"sujet": "<nom>"'`.

## 5. Hooks Claude Code
```sh
"$PY" "$KIT/setup/hooks.py" "$HUB" --essai   # montre ce qui serait ajouté, n'écrit rien
"$PY" "$KIT/setup/hooks.py" "$HUB"
```
Ajoute seulement les hooks manquants (SessionStart, SessionEnd, PostToolUse) dans `~/.claude/settings.json`, garde tout le
reste, sauvegarde l'original une fois dans `settings.json.avant-ultron`. Refus (fichier illisible, lien) : on s'arrête et on explique.

Contrôle : `"$PY" "$KIT/setup/hooks.py" "$HUB" --essai` rend `"added": []`.

## 6. La commande `ultron`
```sh
bash "$HUB/deploy/bin/poser.sh"
ln -sfn "$CLI" "$HOME/.local/bin/ultron-hub"
```
`poser.sh` copie `deploy/bin/ultron` dans `~/.local/bin/ultron` puis fait un essai à sec. `~/.local/bin` doit être dans le PATH :
- Linux : déjà le cas sur Ubuntu après une nouvelle connexion (`~/.profile`).
- Mac (zsh) : `grep -qs '.local/bin' "$HOME/.zprofile" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zprofile"`,
  puis ouvrir un nouveau terminal.

Contrôle : `"$HOME/.local/bin/ultron" --essai` (dit « Essai, aucune session ouverte. » et la session prévue).

## 7. Dashboard du téléphone
Seulement si Tailscale est prêt (`choix.json` : `tailscale: true`, étape manuelle M1 faite, HTTPS activé dans la console).
Sinon : SAUTÉ, on le dit, on continue.
1. Appareils : si `config/dashboard.yaml` contient encore `appareils: []`, y écrire ceux de `choix.json`
   (`appareils:` puis une ligne `  - nom: <nom>` et `    id: <ID>` par appareil). La machine elle-même n'y va jamais.
2. Certificat : `"$CLI" dashboard cert`.
3. Service :
   - Linux :
     ```sh
     mkdir -p "$HOME/.config/systemd/user"
     [ -e "$HOME/.config/systemd/user/ultron-hub-dashboard.service" ] || cp "$R/systemd/ultron-hub-dashboard.service" "$HOME/.config/systemd/user/"
     systemctl --user daemon-reload && systemctl --user enable --now ultron-hub-dashboard.service
     ```
   - Mac : agent launchd. Copie `"$R/launchd/ultron-hub.dashboard.plist"` dans `~/Library/LaunchAgents/` s'il est absent
     (jamais d'écrasement), puis `launchctl bootstrap gui/<uid>` s'il n'est pas déjà chargé :
     ```sh
     "$HUB/.venv/bin/python" -m ultron_hub.common.plateforme installer dashboard --gabarits "$R"
     ```
     Dire aussi : un Mac qui dort ne répond plus, le dashboard n'est joignable que Mac allumé et éveillé. Journal :
     `~/Library/Logs/ultron-hub-dashboard.log`.
4. Sur le téléphone : ouvrir `https://<nom de la machine dans Tailscale>:8787` (le nom est dans `CertDomains` de
   `tailscale status --json`), puis « Ajouter à l'écran d'accueil ».

Contrôle (Linux) : `systemctl --user is-active ultron-hub-dashboard.service` puis il ouvre la page sur son téléphone.
Contrôle (Mac) : `launchctl print "gui/$(id -u)/ultron-hub.dashboard" | grep -q 'state = running'` puis il ouvre la page sur son téléphone.

## 8. Filets (santé)
- Linux :
  ```sh
  for u in ultron-hub-health.service ultron-hub-health.timer; do
    [ -e "$HOME/.config/systemd/user/$u" ] || cp "$R/systemd/$u" "$HOME/.config/systemd/user/"
  done
  systemctl --user daemon-reload && systemctl --user enable --now ultron-hub-health.timer
  ```
  Machine allumée en permanence (serveur) : `loginctl show-user "$USER" -p Linger` vaut `Linger=no` = lui donner
  `! sudo loginctl enable-linger <utilisateur>` (sinon les services s'arrêtent à sa déconnexion).
- Mac : agent launchd `"$R/launchd/ultron-hub.health.plist"` (toutes les 30 min, à h00 et h30 ; une échéance manquée
  pendant la veille est rattrapée une fois au réveil) :
  ```sh
  "$HUB/.venv/bin/python" -m ultron_hub.common.plateforme installer sante --gabarits "$R"
  ```
  Pas de `enable-linger` sur Mac : rien ne tourne Mac endormi ou session fermée.
- Sauvegarde (`ultron-hub-backup.*`) : v2, pas posée en v1.

Contrôle (Linux) : `systemctl --user is-enabled ultron-hub-health.timer`
Contrôle (Mac) : `launchctl print "gui/$(id -u)/ultron-hub.health" >/dev/null`

## 9. Contrôle final et première session
```sh
bash "$KIT/setup/verifier.sh"
```
Tout doit être OK ou SAUTÉ (avec sa raison). Puis :
- le style de `choix.json` devient une proposition de profil : `"$CLI" profil propose "Préfère des réponses <style>." --source setup --no-notify`
  (il la valide lui-même, carte Profil du dashboard) ;
- première session : `ultron "fais-moi visiter"` (il la rejoint plus tard par `ultron liste`).

## À faire à la main par lui (expliqué pas à pas, une chose à la fois)
- M0 (Mac) : installer Homebrew depuis brew.sh.
- M0-Intel (Mac Intel) : Python depuis python.org, puis MacPorts et `! sudo port install tmux` (étape 1).
- M1 Tailscale : créer le compte, installer l'app sur l'ordinateur et le téléphone, se connecter des deux côtés, activer
  HTTPS dans la console Tailscale (DNS, HTTPS Certificates).
- M2 Claude Code : connecté avec son compte (déjà fait s'il lit ceci).
- M3 Commandes `! sudo …` ou `! brew …` données par l'agent : tapées par lui.
