"""`ultron-hub dashboard captures` (todo #241): screenshots of the preview, taken by the BROWSER venv's Python.

Playwright is never imported here: `deploy/navigateur/capturer.py` runs as a subprocess with
`~/.local/share/ultron-hub/navigateur/bin/python` (made by `deploy/navigateur/installer.sh`).
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from ultron_hub.dashboard import apercu

NAVIGATEUR_DIR = Path.home() / ".local" / "share" / "ultron-hub" / "navigateur"
TIMEOUT_S = 420  # 230 s measured on 28/10 with 8 stale screens waiting 30 s each (todo #453)
Runner = Callable[..., subprocess.CompletedProcess]


class CapturesError(Exception):
    pass


def navigateur_dir() -> Path:
    return Path(os.environ.get("ULTRON_NAVIGATEUR_DIR") or NAVIGATEUR_DIR)


def browser_env(base: Path) -> dict[str, str]:
    # TMPDIR: in an escouade's sandbox /tmp is read-only and Chromium's mkdtemp fails there
    env = {k: v for k, v in os.environ.items() if k in ("HOME", "PATH", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "TMPDIR")}
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(base / "navigateurs")
    libs = base / "libs" / "usr" / "lib" / "x86_64-linux-gnu"
    if libs.is_dir():  # libraries extracted without sudo by installer.sh
        env["LD_LIBRARY_PATH"] = str(libs)
    return env


def capturer_path(root: Path) -> Path:
    return root / "deploy" / "navigateur" / "capturer.py"


def rapport(controles: list[dict[str, Any]], ignores: list[str] = ()) -> dict[str, Any]:
    """Design checks summary (todo #252): a failure is reported, never raised.

    A screen the capturer could not reach (`ignores` entry ending in "Error)") is a defect too (todo #453):
    a stale selector waits its full timeout and silently drops the screen."""
    controles = list(controles) + [{"type": "ecran", "ecran": i.split(" (")[0], "theme": "", "element": i}
                                   for i in ignores if i.endswith("Error)")]
    par_type: dict[str, int] = {}
    for c in controles:
        par_type[c.get("type", "?")] = par_type.get(c.get("type", "?"), 0) + 1
    noms = {"contraste": "contraste WCAG AA", "cible": "cibles tactiles >= 44 px", "debordement": "débordement horizontal à 375 px",
            "ecran": "écrans capturés (sélecteur introuvable)"}
    lignes = []
    for type_, nom in noms.items():
        n = par_type.get(type_, 0)
        lignes.append(f"{'OK  ' if n == 0 else 'ECHEC'} {nom}" + (f" : {n} défaut(s)" if n else ""))
        for c in (c for c in controles if c.get("type") == type_):
            detail = {k: v for k, v in c.items() if k not in ("type", "ecran", "theme", "element")}
            lignes.append(f"    [{c.get('ecran')}/{c.get('theme')}] {c.get('element')} {json.dumps(detail, ensure_ascii=False)}")
    return {"ok": not controles, "total": len(controles), "par_type": par_type, "defauts": controles, "resume": "\n".join(lignes)}


def ecrire_rapport(sortie: Path, rap: dict[str, Any]) -> list[str]:
    sortie.mkdir(parents=True, exist_ok=True)
    fj, ft = sortie / "rapport.json", sortie / "rapport.txt"
    fj.write_text(json.dumps(rap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ft.write_text(rap["resume"] + "\n", encoding="utf-8")
    return [str(fj), str(ft)]


def capture(root: Path, sortie: Path, runner: Runner = subprocess.run) -> dict[str, Any]:
    base = navigateur_dir()
    python = base / "bin" / "python"
    script = capturer_path(root)
    if not python.exists():
        raise CapturesError(f"navigateur absent : lancer {root / 'deploy' / 'navigateur' / 'installer.sh'}")
    if not script.exists():
        raise CapturesError("capturer.py introuvable")
    with apercu.running() as url:
        proc = runner([str(python), "-I", str(script), "--url", url, "--sortie", str(sortie)], capture_output=True, text=True,
                      timeout=TIMEOUT_S, check=False, stdin=subprocess.DEVNULL, env=browser_env(base))
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        raise CapturesError("capture échouée" + (" : " + lines[-1][:300] if lines else ""))
    try:
        data = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise CapturesError("réponse du capteur illisible") from None
    try:  # the checks never fail the command
        rap = rapport([c for c in data.pop("controles", []) if isinstance(c, dict)],
                      [i for i in data.get("ignores", []) if isinstance(i, str)])
        rap["fichiers"] = ecrire_rapport(sortie, rap)
    except (OSError, TypeError, ValueError) as exc:
        rap = {"ok": False, "total": 0, "defauts": [], "resume": f"rapport non écrit : {type(exc).__name__}", "fichiers": []}
    data["rapport"] = rap
    return data
