#!/bin/bash
# Pose la commande `ultron` du dépôt (deploy/bin/ultron) dans ~/.local/bin/ultron (todo #284 : `ultron <chantier>`).
# À lancer UNE fois après la fusion ; relançable sans effet (idempotent). Ni sudo ni réseau. Tout part de $HOME (testable).
#   - sauvegarde ~/.local/bin/ultron.avant-284 (la première fois seulement), puis copie atomique, mode 755
#   - vérifie la copie posée par un essai à sec (`ultron --essai`), qui n'ouvre aucune session
# Usage : deploy/bin/poser.sh [--essai]     (--essai : dit ce qui serait fait, n'écrit rien)
set -eu

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ultron"
BIN="$HOME/.local/bin/ultron"
ESSAI=""
[ "${1:-}" = "--essai" ] && ESSAI=1

[ -f "$SRC" ] || { echo "poser: source absente : $SRC" >&2; exit 1; }
sh -n "$SRC" || { echo "poser: $SRC n'est pas un script sh valide (rien modifié)" >&2; exit 1; }
if [ -f "$BIN" ] && cmp -s "$SRC" "$BIN"; then
    echo "poser: déjà à jour : $BIN"
    exit 0
fi
if [ -n "$ESSAI" ]; then
    echo "poser: remplacerait $BIN par $SRC"
    exit 0
fi
mkdir -p "$(dirname "$BIN")"
if [ -f "$BIN" ] && [ ! -e "$BIN.avant-284" ]; then cp -p "$BIN" "$BIN.avant-284"; fi
cp "$SRC" "$BIN.tmp-284"
chmod 755 "$BIN.tmp-284"
mv "$BIN.tmp-284" "$BIN"
echo "poser: posé : $BIN (ancienne version : $BIN.avant-284)"
"$BIN" --essai >/dev/null || { echo "poser: l'essai à sec de $BIN a échoué ; remettre $BIN.avant-284" >&2; exit 1; }
echo "poser: essai à sec réussi"
