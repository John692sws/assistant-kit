#!/usr/bin/env bash
# Dry-run install in a throw-away HOME (todo #391): replays etapes.md, then verifier.sh. No Mac, no network, no sudo.
# Usage: essai_blanc.sh <linux|mac> <kit folder: ultron-hub/ + setup/ + gabarits/> <work folder, absent or empty>
# Env:   ESSAI_PYTHON   python 3.12+ that already has pyyaml and pypdf (default: python3); its site-packages are
#                        linked into the new venv by a .pth file instead of `pip install` (offline)
#        ESSAI_PYTEST=1 also run the test suite of the installed code (step 2)
# Never touched: the real HOME, the real claude, tmux, systemd, launchd (fake binaries first in PATH).
# "mac" forces platform.system() to Darwin (sitecustomize) and KIT_OS=Darwin, with a fake launchctl.
set -eu

VARIANTE="${1:?linux|mac}"; KIT="$(cd "${2:?kit folder}" && pwd)"; WORK="${3:?work folder}"
case "$VARIANTE" in linux|mac) ;; *) echo "variant must be linux or mac" >&2; exit 2;; esac
mkdir -p "$WORK"; WORK="$(cd "$WORK" && pwd)"
[ -z "$(ls -A "$WORK")" ] || { echo "work folder not empty: $WORK" >&2; exit 2; }

REAL_PY="$(command -v "${ESSAI_PYTHON:-python3}")"
export HOME="$WORK/home"
export XDG_CONFIG_HOME="$HOME/.config" LANG=C.UTF-8
mkdir -p "$HOME" "$WORK/bin" "$WORK/state"
unset ULTRON_HUB_ROOT ULTRON_HUB_DIR ULTRON_PROMPTS_DIR ULTRON_ESPRIT_DIR TELEGRAM_BOT_TOKEN

# --- fake external programs -------------------------------------------------------------------------------------
fake() { printf '#!/bin/sh\n%s\n' "$2" > "$WORK/bin/$1"; chmod +x "$WORK/bin/$1"; }
fake claude 'echo "fake claude $*" >> "'"$WORK"'/state/claude.log"; exit 0'
fake tmux 'exit 1'
fake systemctl 'S="'"$WORK"'/state/systemd"; mkdir -p "$S"; shift; case "$1" in
  daemon-reload) ;; enable) for a; do case "$a" in *.timer|*.service) : > "$S/$a";; esac; done ;;
  is-enabled|is-active) [ -e "$S/$2" ] || { echo disabled; exit 1; } ;; *) ;; esac'
fake launchctl 'S="'"$WORK"'/state/launchd"; mkdir -p "$S"; case "$1" in
  bootstrap) b=$(basename "$3" .plist); case "$b" in *dashboard) echo "state = running";; *) echo "state = not running";; esac > "$S/$b" ;;
  print) b="${2##*/}"; [ -e "$S/$b" ] && cat "$S/$b" || exit 113 ;;
  bootout|kickstart) ;; *) exit 1 ;; esac'
export PATH="$WORK/bin:$(dirname "$REAL_PY"):/usr/bin:/bin"
if [ "$VARIANTE" = mac ]; then
    mkdir -p "$WORK/sitecustomize"
    printf 'import platform\nplatform.system = lambda: "Darwin"\n' > "$WORK/sitecustomize/sitecustomize.py"
    export PYTHONPATH="$WORK/sitecustomize" KIT_OS=Darwin
    SYSTEME=Darwin
else
    SYSTEME=Linux
fi
PY="$REAL_PY"

# --- step 0: fixed answers --------------------------------------------------------------------------------------
ETAT="$HOME/.config/assistant-kit"; R="$ETAT/gabarits-remplis"
HUB="$HOME/workspace/projects/active/ultron-hub"; CLI="$HUB/.venv/bin/ultron-hub"
mkdir -p "$ETAT"
cat > "$ETAT/reponses.json" <<JSON
{"PRENOM": "Essai", "NOM_ASSISTANT": "Assistant", "UTILISATEUR": "essai", "HOME": "$HOME",
 "MACHINE": "machine-blanche", "LANGUE": "français", "FUSEAU": "Europe/Paris"}
JSON
cat > "$ETAT/choix.json" <<JSON
{"systeme": "$SYSTEME", "chantiers": [{"nom": "exemple", "titre": "Chantier exemple", "but": "Découvrir l'assistant."}],
 "tailscale": false, "appareils": [], "style": "court, tu"}
JSON
"$PY" -c 'import json,sys; a=json.load(open(sys.argv[1])); assert all(isinstance(v,str) and v.strip() for v in a.values()); json.load(open(sys.argv[2]))' "$ETAT/reponses.json" "$ETAT/choix.json"
echo "== step 0 ok ($VARIANTE)"

# --- step 2: the code ---------------------------------------------------------------------------------------------
mkdir -p "$(dirname "$HUB")"
cp -R "$KIT/ultron-hub" "$HUB"
"$PY" "$KIT/setup/remplir.py" "$ETAT/reponses.json" "$HUB" >/dev/null
cp -R "$KIT/gabarits" "$R" && "$PY" "$KIT/setup/remplir.py" "$ETAT/reponses.json" "$R" >/dev/null
cp "$R/settings.yaml" "$HUB/config/settings.yaml"
cp "$R/dashboard.yaml" "$HUB/config/dashboard.yaml"
"$PY" -m venv "$HUB/.venv"
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
VSITE="$("$HUB/.venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
printf '%s\n%s\n' "$HUB/src" "$SITE" > "$VSITE/ultron_blanc.pth"
printf '#!%s\nimport sys\nfrom ultron_hub.cli import main\nsys.exit(main())\n' "$HUB/.venv/bin/python" > "$CLI"
chmod +x "$CLI"
git -C "$HUB" init -q -b main && git -C "$HUB" add -A &&
    git -C "$HUB" -c user.name=essai -c user.email=essai@localhost commit -q -m "Installation du kit"
if [ "${ESSAI_PYTEST:-}" = 1 ]; then
    (cd "$HUB" && .venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider > "$WORK/pytest.log" 2>&1) || { tail -n 20 "$WORK/pytest.log"; echo "KO     pytest du code installé"; exit 1; }
    tail -n 1 "$WORK/pytest.log"
fi
"$CLI" --help >/dev/null
echo "== step 2 ok"

# --- step 3: prompts, esprit, instructions --------------------------------------------------------------------------
mkdir -p "$HOME/notes/prompt/demarrage" "$HOME/notes/prompt/reprises" "$HOME/ultron/esprit" "$HOME/.claude"
cp "$R/socle.md" "$HOME/notes/prompt/_noyau.md"
cp "$R/prompt/orchestrateur.md" "$HOME/notes/prompt/demarrage/orchestrateur-ultron.md"
cp "$R/prompt/reprise-orchestrateur.md" "$HOME/notes/prompt/reprise-orchestrateur.md"
cp "$R/prompt/_socle.md" "$HOME/notes/prompt/_socle.md"
cp "$R/charte.md" "$HOME/ultron/esprit/charte.md"
"$CLI" esprit init --profil-from "$R/profil.yaml" >/dev/null
ln -s "$HOME/ultron/esprit/charte.md" "$HOME/.claude/charte.md"
ln -s "$HOME/notes/prompt/_noyau.md" "$HOME/.claude/socle.md"
cp "$R/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
"$CLI" prompt mesure >/dev/null && "$CLI" esprit etat >/dev/null
echo "== step 3 ok"

# --- step 4: todos and chantiers ----------------------------------------------------------------------------------
"$CLI" todo list >/dev/null
printf '%s' "Découvrir l'assistant." | "$CLI" escouade nouveau exemple --titre "Chantier exemple" >/dev/null
"$CLI" chantier tableau | grep -qF '"sujet": "exemple"'
echo "== step 4 ok"

# --- step 5: Claude Code hooks (twice: the second run must add nothing) --------------------------------------------
"$PY" "$KIT/setup/hooks.py" "$HUB" --essai >/dev/null
"$PY" "$KIT/setup/hooks.py" "$HUB" >/dev/null
"$PY" "$KIT/setup/hooks.py" "$HUB" --essai | grep -qF '"added": []'
echo "== step 5 ok"

# --- step 6: the ultron command --------------------------------------------------------------------------------------
bash "$HUB/deploy/bin/poser.sh" >/dev/null
mkdir -p "$HOME/.local/bin"; ln -sfn "$CLI" "$HOME/.local/bin/ultron-hub"
env ULTRON_HUB_DIR="$HUB" ULTRON_PROMPTS_DIR="$HOME/notes/prompt" "$HOME/.local/bin/ultron" --essai >/dev/null
echo "== step 6 ok"

# --- steps 7 and 8: dashboard and health services (no certificate here; the service manager is the fake one) ------------
# The dashboard is installed in every case: verifier.sh checks it as soon as a tailscale command exists on the machine.
if [ "$VARIANTE" = mac ]; then
    for svc in dashboard sante; do
        "$HUB/.venv/bin/python" -m ultron_hub.common.plateforme installer "$svc" --gabarits "$R" >/dev/null
    done
    for f in "$HOME"/Library/LaunchAgents/*.plist; do "$PY" -c 'import plistlib,sys; plistlib.load(open(sys.argv[1],"rb"))' "$f"; done
else
    mkdir -p "$HOME/.config/systemd/user"
    for u in ultron-hub-dashboard.service ultron-hub-health.service ultron-hub-health.timer; do
        cp "$R/systemd/$u" "$HOME/.config/systemd/user/"
    done
    systemctl --user daemon-reload && systemctl --user enable --now ultron-hub-dashboard.service ultron-hub-health.timer
fi
echo "== steps 7-8 ok"

# --- step 9: final check --------------------------------------------------------------------------------------------------
bash "$KIT/setup/verifier.sh"
