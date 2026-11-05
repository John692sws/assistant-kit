"""Automatic gesture of the tidy-up (todo #231): a Reprise file over its size budget is archived by `esprit reprise-archiver`.

No archiving code here: it calls the existing, loss-proof `esprit.reprise.archive` (every line ends up once in the file or in the
archive, else nothing is written). Only the files listed under `budgets.archiver_seul` in config/menage.yaml are concerned
(glob patterns allowed, todo #284: `~/notes/prompt/reprises/*.md`, one Reprise per chantier, archived under its own name).
"""

import fnmatch
import glob
import logging
import os
from pathlib import Path
from typing import Any

from ultron_hub.common.settings import Settings
from ultron_hub.esprit import reprise
from ultron_hub.esprit import store as journal
from ultron_hub.menage import rules

log = logging.getLogger("ultron_hub.menage")


def archiver_reprises(settings: Settings, cfg: dict[str, Any], home: str | None = None) -> list[dict[str, Any]]:
    """Archive each listed file whose size exceeds its `budgets.files` limit; one journal line per file archived.

    Never raises: a failure becomes an `erreur` item, the scan then still reports the file as too big.
    """
    home = home or rules.user_home()
    budgets = cfg.get("budgets") or {}
    limits = {os.path.normpath(rules.expand(str(k), home)): float(v) for k, v in (budgets.get("files") or {}).items()}
    chantiers = os.path.normpath(os.path.join(home, "notes", "prompt", "reprises"))
    paths: list[str] = []
    for raw in budgets.get("archiver_seul") or []:
        pattern = os.path.normpath(rules.expand(str(raw), home))
        found = sorted(glob.glob(pattern)) if glob.has_magic(pattern) else [pattern]
        paths += [p for p in found if p not in paths]
    done: list[dict[str, Any]] = []
    for path in paths:
        # as check 19: a file matched by several budget patterns keeps the smallest one
        matching = [v for k, v in limits.items() if k == path or glob.has_magic(k) and fnmatch.fnmatchcase(path, k)]
        limit = min(matching) if matching else None
        own = os.path.dirname(path) == chantiers  # a chantier's Reprise: archives/reprises-<chantier>-AAAA-MM.md
        try:
            if limit is None or os.path.islink(path) or not os.path.isfile(path) or os.lstat(path).st_size / 1024 <= limit:
                continue
            result = reprise.archive(Path(path), Path(home) / "notes" / "prompt" / "archives",
                                     nom=f"reprises-{Path(path).stem}" if own else "reprises")
            done.append({"path": path, **result})
            if result["ecrit"]:
                journal.record(settings, "ultron-hub", "menage",
                               f"Ménage : Reprise au-delà de {limit:g} Ko, {result['archivees']} entrée(s) archivée(s) "
                               f"dans {result['archive']}")
        except Exception as exc:  # noqa: BLE001 - the report must go out
            log.warning("menage: archivage de %s impossible", path, exc_info=True)
            done.append({"path": path, "ecrit": False, "erreur": f"{type(exc).__name__}: {exc}"})
    return done
