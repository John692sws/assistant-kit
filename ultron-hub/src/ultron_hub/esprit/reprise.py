"""Archive old Reprise entries of the orchestrator prompt (todo #169). Script only, nothing lost.

The section « ## 8. Reprise » holds one entry per hand-over: a top-level bullet `- **…**` and its indented lines, most recent first.
`archive` keeps the FOCUS bullet and the `keep` most recent entries, and moves the others, unchanged, to the top of
`~/notes/prompt/archives/reprises-AAAA-MM.md`. Anti-loss: every line of the prompt ends up exactly once in the new prompt or in
the archive, else nothing is written.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path

from ultron_hub.common.errors import InvalidRequestError

PROMPT = Path.home() / "notes" / "prompt" / "demarrage" / "orchestrateur-ultron.md"
REPRISE = Path.home() / "notes" / "prompt" / "reprise-orchestrateur.md"  # todo #230: the Reprise has its own file
ARCHIVES = Path.home() / "notes" / "prompt" / "archives"
SECTIONS = ("## 8. Reprise", "## Reprise")
KEEP = 3
ENTRY_STARTS = ("- **", "### ")  # an entry is a top-level bullet `- **…` or a heading `### …`
CHANTIERS = Path.home() / "notes" / "prompt" / "reprises"  # todo #284: one Reprise per chantier session


def of_chantier(chantier: str, folder: Path = CHANTIERS) -> tuple[Path, str]:
    """(Reprise file of a chantier session, archive name prefix): reprises/<chantier>.md -> archives/reprises-<chantier>-AAAA-MM.md."""
    from ultron_hub.escouade.core import check_name  # lazy: the orchestrator's archiving needs nothing from the squads

    check_name(chantier)
    return folder / f"{chantier}.md", f"reprises-{chantier}"


def current() -> Path:
    """The Reprise file when it exists (new layout), else the old orchestrator prompt (transition without breakage)."""
    return REPRISE if REPRISE.is_file() else PROMPT


def split(text: str) -> tuple[list[str], list[str], list[list[str]], list[str]]:
    """(lines before the entries, heading + free lines, entries, lines after the section)."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(SECTIONS))
    except StopIteration:
        raise InvalidRequestError(f"section « {SECTIONS[0]} » absente") from None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    head, entries = [lines[start]], []
    for line in lines[start + 1:end]:
        if line.startswith(ENTRY_STARTS):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
        else:
            head.append(line)
    return lines[:start], head, entries, lines[end:]


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def archive(prompt: Path = PROMPT, archives: Path = ARCHIVES, keep: int = KEEP, now: float | None = None,
            dry_run: bool = False, nom: str = "reprises") -> dict:
    if not 1 <= keep <= 20:
        raise InvalidRequestError("--garder : entre 1 et 20")
    if prompt.is_symlink() or not prompt.is_file():
        raise InvalidRequestError(f"{prompt} n'est pas un fichier ordinaire")
    source = prompt.read_text(encoding="utf-8")
    before, head, entries, after = split(source)
    focus = [e for e in entries if e[0].startswith("- **FOCUS")]
    dated = [e for e in entries if not e[0].startswith("- **FOCUS")]
    kept, moved = dated[:keep], dated[keep:]
    month = time.strftime("%Y-%m", time.localtime(time.time() if now is None else now))
    target = archives / f"{nom}-{month}.md"
    result = {"gardees": len(kept), "archivees": len(moved), "archive": str(target), "ecrit": False}
    if not moved:
        return result
    new_prompt = "\n".join(before + head + [x for e in focus + kept for x in e] + after) + "\n"
    old_archive = target.read_text(encoding="utf-8") if target.is_file() else ""
    origin = "du prompt orchestrateur (todo #169)" if nom == "reprises" else f"de {prompt.name} (todo #284)"
    title = f"# Reprises archivées ({month})\n\nEntrées sorties {origin}, les plus récentes en haut.\n"
    body = old_archive.split("\n", 3)[3] if old_archive.startswith("# Reprises archivées") else old_archive
    new_archive = title + "\n" + "\n".join(x for e in moved for x in e) + "\n" + (body if body.strip() else "")
    lost = Counter(l for l in source.splitlines() if l.strip()) - \
        Counter(l for l in new_prompt.splitlines() + [x for e in moved for x in e] if l.strip())
    if lost:
        raise InvalidRequestError(f"{sum(lost.values())} ligne(s) perdue(s) : rien n'a été écrit")
    if dry_run:
        return result
    if target.is_symlink():
        raise InvalidRequestError(f"{target} est un lien : rien n'a été écrit")
    archives.mkdir(parents=True, exist_ok=True)
    _write(target, new_archive)  # the archive first: a crash in between duplicates, never loses
    _write(prompt, new_prompt)
    result["ecrit"] = True
    return result
