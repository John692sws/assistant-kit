#!/usr/bin/env bash
# Final check of the assistant install. Read-only: writes nothing, starts nothing.
# One line per point: "OK", "KO" (with what to do) or "SAUTÉ" (with the reason). Exit 0 when no point is KO.
# Works on Linux and macOS. Overrides (tests): ULTRON_HUB_DIR, ULTRON_PROMPTS_DIR, ULTRON_ESPRIT_DIR, KIT_OS.
set -u

HUB="${ULTRON_HUB_DIR:-$HOME/workspace/projects/active/ultron-hub}"
PROMPTS="${ULTRON_PROMPTS_DIR:-$HOME/notes/prompt}"
ESPRIT="${ULTRON_ESPRIT_DIR:-$HOME/ultron/esprit}"
CLI="$HUB/.venv/bin/ultron-hub"
OS="${KIT_OS:-$(uname -s)}"
KO=0

ok() { printf 'OK     %s\n' "$1"; }
ko() { printf 'KO     %s : %s\n' "$1" "$2"; KO=$((KO + 1)); }
saute() { printf 'SAUTÉ  %s : %s\n' "$1" "$2"; }
check() { # check <label> <hint> <command...>
    local label="$1" hint="$2"; shift 2
    if "$@" >/dev/null 2>&1; then ok "$label"; else ko "$label" "$hint"; fi
}

PY="$(for p in python3.14 python3.13 python3.12 python3; do q="$(command -v "$p")" && "$q" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null && { echo "$q"; break; }; done; true)"
py_ok() { [ -n "$PY" ] && "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; }
check "python 3.12 ou plus" "étape 1" py_ok
for tool in git tmux claude; do check "commande $tool" "étape 1" command -v "$tool"; done

check "code installé ($HUB)" "étape 2" test -f "$HUB/pyproject.toml"
check "ultron-hub répond" "étape 2" "$CLI" --help
check "config/settings.yaml" "étape 2" test -f "$HUB/config/settings.yaml"
no_marker() { [ -d "$HUB" ] && ! grep -rIlq --exclude-dir=.venv --exclude-dir=.git '{{[A-Z]' "$HUB"; }
check "aucun marqueur {{…}} restant" "étape 2 (remplir.py)" no_marker

check "noyau du prompt" "étape 3" test -f "$PROMPTS/_noyau.md"
check "orchestrateur" "étape 3" test -f "$PROMPTS/demarrage/orchestrateur-ultron.md"
check "Reprise" "étape 3" test -f "$PROMPTS/reprise-orchestrateur.md"
check "socle des chantiers" "étape 3" test -f "$PROMPTS/_socle.md"
check "prompt assemblé" "étape 3" "$CLI" prompt mesure
for f in charte.md profil.md journal.jsonl; do check "esprit : $f" "étape 3" test -f "$ESPRIT/$f"; done
check "~/.claude/CLAUDE.md" "étape 3" test -f "$HOME/.claude/CLAUDE.md"

check "base de todos" "étape 4" test -f "$HUB/var/ultron.db"
check "chantiers" "étape 4" "$CLI" chantier tableau

hooks_ok() {
    [ -n "$PY" ] && "$PY" - "$HOME/.claude/settings.json" "$CLI" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
cli = sys.argv[2]
want = {"SessionStart": "esprit context --claude-hook", "SessionEnd": "journal summarize --claude-hook --detach",
        "PostToolUse": "esprit contexte --claude-hook"}
hooks = data.get("hooks", {})
cmds = {e: [h.get("command") for g in hooks.get(e, []) for h in g.get("hooks", [])] for e in want}
sys.exit(0 if all(f"{cli} {a}" in cmds[e] for e, a in want.items()) else 1)
EOF
}
check "hooks Claude Code" "étape 5 (hooks.py)" hooks_ok

check "commande ultron" "étape 6 (poser.sh)" test -x "$HOME/.local/bin/ultron"
check "ultron se lance (--help)" "étape 6" env ULTRON_HUB_DIR="$HUB" ULTRON_PROMPTS_DIR="$PROMPTS" "$HOME/.local/bin/ultron" --help

if [ "$OS" = "Darwin" ]; then
    AGENTS="$HOME/Library/LaunchAgents"
    DOM="gui/$(id -u)"
    running() { launchctl print "$DOM/$1" 2>/dev/null | grep -q 'state = running'; }
    if command -v tailscale >/dev/null 2>&1; then
        check "agent launchd dashboard posé" "étape 7" test -f "$AGENTS/ultron-hub.dashboard.plist"
        check "dashboard actif" "étape 7" running ultron-hub.dashboard
    else
        saute "dashboard" "Tailscale absent (étape manuelle M1)"
    fi
    check "agent launchd santé posé" "étape 8" test -f "$AGENTS/ultron-hub.health.plist"
    check "agent santé chargé" "étape 8" launchctl print "$DOM/ultron-hub.health"
elif ! command -v systemctl >/dev/null 2>&1; then
    ko "systemd utilisateur" "systemctl introuvable"
else
    if command -v tailscale >/dev/null 2>&1; then
        check "unité dashboard posée" "étape 7" test -f "$HOME/.config/systemd/user/ultron-hub-dashboard.service"
        check "dashboard actif" "étape 7" systemctl --user is-active ultron-hub-dashboard.service
    else
        saute "dashboard" "Tailscale absent (étape manuelle M1)"
    fi
    check "unité santé posée" "étape 8" test -f "$HOME/.config/systemd/user/ultron-hub-health.timer"
    check "timer santé" "étape 8" systemctl --user is-enabled ultron-hub-health.timer
fi

if [ "$KO" -eq 0 ]; then echo "Bilan : tout est OK."; else echo "Bilan : $KO point(s) KO."; fi
[ "$KO" -eq 0 ]
