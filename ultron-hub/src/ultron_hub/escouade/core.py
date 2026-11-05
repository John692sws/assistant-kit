"""Assemblage des prompts d'escouade (todo #95) : socle commun + mode escouade + fichier sujet. Fonctions sans effet de bord."""

import re
import secrets
from pathlib import Path

from ultron_hub.common.errors import InvalidRequestError

PROMPTS_ENV = "ULTRON_PROMPTS_DIR"
DEFAULT_DIR = Path("~/notes/prompt")
SOCLE_NAME = "_socle"
MODE_NAME = "_mode-escouade"
MODE_INTERACTIF_NAME = "_mode-interactif"
ORCHESTRATEUR_PATH = Path("demarrage/orchestrateur-ultron.md")  # under the prompts folder
MAIN_NAME = "main"  # the session without chantier (`ultron-main-JJ-MM-HHhMM`), never a chantier name
REPRISE_NAME = "reprise-orchestrateur"
REPRISES_DIR = "reprises"  # todo #284: one Reprise per chantier, <prompts>/reprises/<chantier>.md
INTERACTIF_NUMBERS = (2, 6, 7, 8)  # sections of the interactive mode file
SOCLE_NUMBERS = (2, 3, 5, 6, 7)  # sections found in socle + mode together (socle: 3, 5; mode: 2, 6, 7)
NOYAU_NAME = "_noyau"
NOYAU_MAX = 5000  # bytes: the core of the interactive prompt (todo #358)
FICHE_MAX = 4000  # bytes: one sheet of <prompts>/fiches/, read on demand
FICHES_DIR = "fiches"
_ENTREE = re.compile(r"^- \*\*\d{1,2}/\d{1,2}\b", re.MULTILINE)  # a dated Reprise entry, most recent first
DEFAULT_DRAPEAU = "aucun (session lancée à la main)"
DEFAULT_PROJET = Path("~/notes")
_PROJET = re.compile(r"^Projet\s*:\s*(.+?)\s*$", re.MULTILINE)
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")  # nouveaux sujets (Telegram : callbacks de 64 octets)
TITRE_MAX = 120
OBJECTIF_MAX = 2000
_HEADING = re.compile(r"^## (\d+)\.", re.MULTILINE)


def split_sections(text: str) -> tuple[str, dict[int, str]]:
    """(texte avant le premier « ## N. », {N: section avec son titre})."""
    marks = list(_HEADING.finditer(text))
    if not marks:
        return text, {}
    head = text[:marks[0].start()]
    sections: dict[int, str] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        sections.setdefault(int(mark.group(1)), text[mark.start():end])
    return head, sections


def _block(text: str) -> str:
    return text.strip("\n") + "\n"


def new_jeton() -> str:
    """Session token: 8 hex characters, drawn fresh for every session."""
    return secrets.token_hex(4)


def sujet_sections(sujet_texte: str, sujet: str) -> tuple[str, str]:
    """(header + section 1, section 4) of a subject file; its old 2, 3, 5, 6, 7 are ignored in favour of the socle."""
    _, own = split_sections(sujet_texte)
    if 4 not in own:
        raise InvalidRequestError(f"le sujet {sujet} n'a pas de section « ## 4. »")
    pos4 = re.search(r"^## 4\.", sujet_texte, re.MULTILINE).start()
    before = sujet_texte[:pos4]
    for n in SOCLE_NUMBERS:
        if n in own and n < 4:
            before = before.replace(own[n], "")
    return before, own[4]


def _fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    left = sorted({m for m in re.findall(r"\{(sujet|limite|fiche|drapeau|dossier|jeton|reprise_fichier|reprise_archiver)\}", text)})
    if left:  # une valeur contenant elle-même « {limite} » par exemple
        raise InvalidRequestError("paramètre non rempli : " + ", ".join("{" + k + "}" for k in left))
    return text


def assemble(socle: str, mode: str, sujet_texte: str, *, sujet: str, limite: str, fiche: str, drapeau: str,
             dossier: str = str(DEFAULT_DIR.expanduser()), jeton: str | None = None) -> str:
    """Three pieces: common base (rules), escouade mode (stop, questions, handover), subject file (1 and 4).

    Order (todo #229, spec A1): hard rules first (stop, security), then the way of working, then the
    subject sheet, then the escouade reporting and the handover last. A legacy single-file socle that still
    holds every section works with an empty mode.
    """
    jeton = jeton or new_jeton()
    _, common = split_sections(socle)
    _, own_mode = split_sections(mode)
    common = {**common, **own_mode}
    missing = [n for n in SOCLE_NUMBERS if n not in common]
    if missing:
        raise InvalidRequestError("socle incomplet, sections manquantes : " + ", ".join(f"## {n}." for n in missing))
    before, own4 = sujet_sections(sujet_texte, sujet)
    parts = [common[2], common[5], common[3], before, own4, common[6], common[7]]
    text = "\n".join(_block(p) for p in parts if p.strip())
    return _fill(text, {"sujet": sujet, "limite": limite, "fiche": fiche, "drapeau": drapeau, "dossier": dossier,
                        "jeton": jeton})


def assemble_interactif(socle: str, mode: str, orchestrateur: str, reprise: str, sujet_texte: str | None = None, *,
                        sujet: str | None = None, dossier: str = str(DEFAULT_DIR.expanduser())) -> str:
    """Interactive prompt (todo #230): socle + interactive mode + orchestrator (+ subject sheet) + Reprise last.

    Order (spec A1): stop conditions and hard rules first, then the way of working, the mission and order of work,
    the subject sheet, reporting and handover, and the Reprise (state of the day) at the very end.
    """
    _, common = split_sections(socle)
    _, own = split_sections(mode)
    orch_head, orch = split_sections(orchestrateur)
    missing = [f"## {n}." for n in (3, 5) if n not in common] + [f"## {n}." for n in INTERACTIF_NUMBERS if n not in own]
    missing += [f"orchestrateur ## {n}." for n in (1, 4) if n not in orch]
    if missing:
        raise InvalidRequestError("prompt interactif incomplet, sections manquantes : " + ", ".join(missing))
    fiche_parts = list(sujet_sections(sujet_texte, sujet or "?")) if sujet_texte is not None else []
    if sujet_texte is not None and sujet:  # todo #287: the orchestrator part, aimed at one chantier
        orch_head, mission, ordre = chantier_orchestrateur(sujet)
        orch = {**orch, 1: mission, 4: ordre}
        reprise_fichier = str(Path(dossier) / REPRISES_DIR / f"{sujet}.md")
        reprise_archiver = f"ultron-hub esprit reprise-archiver --chantier {sujet}"
    else:
        reprise_fichier = "~/notes/prompt/reprise-orchestrateur.md"
        reprise_archiver = "ultron-hub esprit reprise-archiver"
    parts = [own[2], common[5], common[3], own[8], orch_head, orch[1], orch[4], *fiche_parts, own[6], own[7]]
    text = "\n".join(_block(p) for p in parts if p.strip())
    if sujet_texte is not None and sujet:  # a mode file posed before #287 still names the orchestrator's Reprise
        text = text.replace("`~/notes/prompt/reprise-orchestrateur.md`), puis `ultron-hub esprit reprise-archiver`",
                            "`{reprise_fichier}`), puis `{reprise_archiver}`")
    text = _fill(text, {"sujet": sujet or "choisi parmi `ultron-hub chantier tableau`",
                        "fiche": f"~/notes/chantier-{sujet}.md" if sujet else "la fiche du chantier concerné (`~/notes/chantier-<nom>.md`)",
                        "limite": "aucun seuil de quota par défaut (cible : 100 % de chaque compte à sa remise à zéro)", "drapeau": "aucun", "dossier": dossier,
                        "jeton": "-", "reprise_fichier": reprise_fichier, "reprise_archiver": reprise_archiver})
    if sujet_texte is not None and sujet:
        text += "\n" + _block(reprise_consigne(sujet, dossier))
    return text + "\n" + _block(reprise)  # the Reprise is data of the day: never filled, always last


def chantier_orchestrateur(sujet: str) -> tuple[str, str, str]:
    """(header, section 1, section 4) of the orchestrator aimed at ONE chantier (todo #287): the delegation, branches,
    checks and handover rules stay (they live in the socle and the interactive mode); the mission, the order of work and
    the Reprise are the chantier's only."""
    head = (f"# Orchestrateur du chantier {sujet}\n\n"
            f"**Session de chantier** (`ultron {sujet}`) : tu orchestres CE chantier et rien d'autre. Ce prompt est assemblé par\n"
            f"`ultron-hub prompt interactif {sujet}` : socle commun, mode interactif, ce chantier, sa fiche, puis sa Reprise (en dernier).\n"
            "`ultron \"consigne\"` ajoute une consigne du jour, prioritaire sur la section 4.\n")
    mission = (f"## 1. Mission\n"
               f"Faire avancer le chantier **{sujet}** : sa mission est celle de sa fiche `~/notes/chantier-{sujet}.md`\n"
               f"(`ultron-hub chantier fiche {sujet}`) et de la fiche de sujet ci-dessous.\n"
               f"- **Pas de travail hors chantier** : une panne ou une idée d'un autre chantier se note (`ultron-hub todo add`, avec son\n"
               f"  chantier) et se signale à l'utilisateur, tu ne la traites pas ici.\n"
               "- Tu avances **partout où tu es autonome** dans ce chantier ; tu n'attends l'utilisateur que pour ce que lui seul peut faire.\n"
               "- Les escouades sont en pause (décision du 20/10) : la section « ## REPRISE » de la fiche est gelée, ta Reprise est\n"
               "  le fichier indiqué en bas de ce prompt.\n")
    ordre = ("## 4. Ordre du travail (à chaque ouverture)\n"
             "1. Lire la Reprise du chantier (en bas de ce prompt, entrée la plus récente) : branches prêtes, décisions attendues,\n"
             "   travail interrompu.\n"
             "2. Vérifier l'état réel : `git worktree list`, `git branch --list 'todo-*'` (celles de ce chantier), journal commun,\n"
             "   puis `git merge-tree --write-tree main <branche>` pour chaque branche prête.\n"
             "3. Rappeler à l'utilisateur ce qui n'attend que lui dans ce chantier (✅, 🔀, questions), en 5 lignes.\n"
             f"4. Travailler dans l'ordre : panne ou faille du chantier d'abord ; consigne du jour ; suite de la Reprise ; puis les todos\n"
             f"   de ce chantier seulement (`ultron-hub todo list --chantier {sujet}`, `ultron-hub chantier fiche {sujet}`), en commençant par celles\n"
             "   qui n'attendent rien de l'utilisateur. Plus de todo à faire dans le chantier : dis-le, propose la suite, ne sors pas du chantier.\n"
             "5. Après chaque fusion annoncée par l'utilisateur : refaire le point 2 avant d'annoncer une autre branche prête.\n")
    return head, mission, ordre


def reprise_path(sujet: str, dossier: str | None = None) -> Path:
    """The Reprise of a chantier session (todo #284): <prompts>/reprises/<chantier>.md."""
    return prompts_dir(dossier) / REPRISES_DIR / f"{check_name(sujet)}.md"


def reprise_vide(sujet: str) -> str:
    """First content of a chantier Reprise: the heading `esprit reprise-archiver` needs, and no entry yet."""
    return (f"## Reprise du chantier {sujet} (état du jour, mise à jour à chaque passation, la plus récente en haut)\n"
            "Aucune passation encore pour ce chantier.\n")


def reprise_consigne(sujet: str, dossier: str) -> str:
    """Where a chantier session writes its Reprise: its own file, never the orchestrator's (todo #284)."""
    path = Path(dossier) / REPRISES_DIR / f"{sujet}.md"
    return (f"## Reprise de cette session de chantier ({sujet})\n"
            f"- Cette session travaille sur le chantier {sujet} seulement. Sa Reprise (ci-dessous) est le fichier `{path}` :\n"
            f"  à la passation, c'est lui que tu mets à jour (jamais `reprise-orchestrateur.md`, qui est celle de la session\n"
            f"  système), puis `ultron-hub esprit reprise-archiver --chantier {sujet}`. La « ## REPRISE » de la fiche est gelée\n"
            f"  (escouades en pause) : n'y écris rien.\n"
            f"- Pour continuer à neuf : `! ultron relance` rouvre une session sur ce même chantier.")


def derniere_entree(reprise: str) -> str:
    """Header of a Reprise file + its most recent dated entry; a file without dated entry is returned as is."""
    marks = list(_ENTREE.finditer(reprise))
    if not marks:
        return reprise
    end = marks[1].start() if len(marks) > 1 else len(reprise)
    return reprise[:marks[0].start()] + reprise[marks[0].start():end]


def assemble_noyau(noyau: str, orchestrateur: str, reprise: str, sujet_texte: str | None = None, *,
                   sujet: str | None = None, dossier: str = str(DEFAULT_DIR.expanduser())) -> dict[str, str]:
    """Core mode (todo #358): {block: text} = noyau, mission (header + section 1 + subject parts), reprise.

    The core replaces the socle, the interactive mode and the orchestrator's section 4; only the latest Reprise entry
    is injected. The Reprise is data of the day: never filled, always last.
    """
    size = len(noyau.encode())
    if size > NOYAU_MAX:
        raise InvalidRequestError(f"noyau trop gros : {size} octets (maximum {NOYAU_MAX})")
    orch_head, orch = split_sections(orchestrateur)
    if 1 not in orch:
        raise InvalidRequestError("orchestrateur incomplet, section manquante : ## 1.")
    mission_parts = [orch_head, orch[1]]
    consigne = ""
    if sujet_texte is not None and sujet:
        orch_head, mission, _ = chantier_orchestrateur(sujet)
        mission_parts = [orch_head, mission, *sujet_sections(sujet_texte, sujet)]
        reprise_fichier = str(Path(dossier) / REPRISES_DIR / f"{sujet}.md")
        reprise_archiver = f"ultron-hub esprit reprise-archiver --chantier {sujet}"
        consigne = reprise_consigne(sujet, dossier)
    else:
        if sujet_texte is not None:
            mission_parts += list(sujet_sections(sujet_texte, "?"))
        reprise_fichier = "~/notes/prompt/reprise-orchestrateur.md"
        reprise_archiver = "ultron-hub esprit reprise-archiver"
    values = {"sujet": sujet or "choisi parmi `ultron-hub chantier tableau`",
              "fiche": f"~/notes/chantier-{sujet}.md" if sujet else "la fiche du chantier concerné (`~/notes/chantier-<nom>.md`)",
              "limite": "aucun seuil de quota par défaut (cible : 100 % de chaque compte à sa remise à zéro)", "drapeau": "aucun", "dossier": dossier, "jeton": "-",
              "reprise_fichier": reprise_fichier, "reprise_archiver": reprise_archiver}
    mission_text = "\n".join(_block(p) for p in [*mission_parts, consigne] if p.strip())
    return {"noyau": _block(_fill(noyau, values)), "mission": _fill(mission_text, values) if mission_text else "",
            "reprise": _block(derniere_entree(reprise))}


def read_pieces(sujet: str | None, folder: Path, *, noyau: bool) -> tuple[dict[str, str], str | None]:
    paths = {"orchestrateur": folder / ORCHESTRATEUR_PATH}
    if noyau:
        paths["noyau"] = folder / f"{NOYAU_NAME}.md"
    else:
        paths.update({"socle": folder / f"{SOCLE_NAME}.md", "mode": folder / f"{MODE_INTERACTIF_NAME}.md"})
    if not sujet:
        paths["reprise"] = folder / f"{REPRISE_NAME}.md"
    texts = {}
    for key, path in paths.items():
        if not path.is_file():
            raise InvalidRequestError(f"fichier introuvable : {path}")
        texts[key] = path.read_text(encoding="utf-8")
    fiche = None
    if sujet:
        check_name(sujet)
        subject_file = folder / f"{sujet}.md"
        if not subject_file.is_file():
            raise InvalidRequestError(f"fichier introuvable : {subject_file}")
        fiche = subject_file.read_text(encoding="utf-8")
        own = reprise_path(sujet, str(folder))  # a chantier session reads its own Reprise, never the orchestrator's
        texts["reprise"] = own.read_text(encoding="utf-8") if own.is_file() else reprise_vide(sujet)
    return texts, fiche


def noyau_blocks(sujet: str | None, folder: Path) -> dict[str, str] | None:
    """Blocks of the core-mode prompt, or None when <prompts>/_noyau.md is absent (legacy path)."""
    if not (folder / f"{NOYAU_NAME}.md").is_file():
        return None
    texts, fiche = read_pieces(sujet, folder, noyau=True)
    return assemble_noyau(texts["noyau"], texts["orchestrateur"], texts["reprise"], fiche, sujet=sujet, dossier=str(folder))


def build_interactif(sujet: str | None = None, *, dossier: str | None = None) -> str:
    """Read the pieces from the prompts folder and assemble the interactive prompt (script only, no AI).

    With <prompts>/_noyau.md: core mode (todo #358). Without: the legacy socle + mode + orchestrator assembly.
    """
    folder = prompts_dir(dossier)
    blocks = noyau_blocks(sujet, folder)
    if blocks is not None:
        return "\n".join(b for b in (blocks["noyau"], blocks["mission"], blocks["reprise"]) if b.strip())
    texts, fiche = read_pieces(sujet, folder, noyau=False)
    return assemble_interactif(texts["socle"], texts["mode"], texts["orchestrateur"], texts["reprise"], fiche,
                               sujet=sujet, dossier=str(folder))


_FICHE_REF = re.compile(r"fiches/[A-Za-z0-9_.-]+\.md")
_SKILL_REF = re.compile(r"(?:^[ \t]*(?:-[ \t]*)?|;[ \t]*)`([a-z0-9][a-z0-9-]*)`", re.MULTILINE)


def noyau_renvois(text: str) -> dict[str, list[str]]:
    """Sheets (fiches/x.md) and skills cited by the core text; skills = backticked slugs leading an item of a "skill" section."""
    fiches = sorted(set(_FICHE_REF.findall(text)))
    skills: list[str] = []
    in_skills = False
    section: list[str] = []
    for line in text.splitlines() + ["## "]:
        if line.startswith("## "):
            if in_skills:
                skills += _SKILL_REF.findall("\n".join(section))
            in_skills, section = "skill" in line.lower(), []
        else:
            section.append(line)
    return {"fiches": fiches, "skills": sorted(set(skills))}


def mesure(sujet: str | None = None, *, dossier: str | None = None, skills_dir: Path | None = None) -> dict:
    """Read-only sizes in bytes of each block of the prompt and of each sheet in fiches/, plus the core's cross-references (todos #358, #449)."""
    folder = prompts_dir(dossier)
    blocks = noyau_blocks(sujet, folder)
    raisons: list[str] = []
    if blocks is None:
        text = build_interactif(sujet, dossier=str(folder))
        sizes = {"mode": "ancien (pas de _noyau.md)", "total": len(text.encode())}
    else:
        sizes = {"mode": "noyau", **{k: len(v.encode()) for k, v in blocks.items()}}
        sizes["total"] = len(build_interactif(sujet, dossier=str(folder)).encode())
    fdir = folder / FICHES_DIR
    fiches = {f.name: f.stat().st_size for f in sorted(fdir.glob("*.md"))} if fdir.is_dir() else {}
    raisons += [f"fiche {n} {v} > {FICHE_MAX}" for n, v in fiches.items() if v > FICHE_MAX]
    renvois: dict[str, list[str]] = {"fiches": [], "skills": []}
    if blocks is not None:
        renvois = noyau_renvois((folder / f"{NOYAU_NAME}.md").read_text())
        sdir = skills_dir if skills_dir is not None else Path.home() / ".claude/skills"
        raisons += [f"fiche citée absente : {f}" for f in renvois["fiches"] if not (folder / f).is_file()]
        raisons += [f"skill citée absente : {k}" for k in renvois["skills"] if not (sdir / k / "SKILL.md").is_file()]
    return {"ok": not raisons, "raison": "; ".join(raisons) or None, "octets": sizes, "fiches": fiches, "renvois": renvois}


def prompts_dir(override: str | None = None) -> Path:
    import os
    return Path(override or os.environ.get(PROMPTS_ENV) or DEFAULT_DIR).expanduser()


def check_name(sujet: str) -> str:
    if not sujet or "/" in sujet or "\\" in sujet or ".." in sujet or sujet.startswith("."):
        raise InvalidRequestError(f"nom de sujet refusé : {sujet!r}")
    if sujet == MAIN_NAME:
        raise InvalidRequestError("« main » est le nom de la session Main (sans chantier) : il ne peut pas être un chantier")
    if sujet.startswith("_") or sujet == REPRISE_NAME:
        raise InvalidRequestError(f"« {sujet} » n'est pas un sujet (fichier de socle ou de mode)")
    return sujet


def projet(texte: str) -> Path | None:
    """Dossier de travail annoncé par la ligne « Projet : <chemin> » de l'en-tête (avant « ## 1. »), ou None."""
    head, _ = split_sections(texte)
    found = _PROJET.search(head)
    if found is None:
        return None
    path = Path(found.group(1).strip().strip("`")).expanduser()
    if not path.is_absolute():
        raise InvalidRequestError(f"« Projet : {found.group(1)} » doit être un chemin absolu (ou commencer par ~)")
    return path


def projet_of(sujet: str, dossier: str | None = None) -> Path:
    """Dossier de travail d'un sujet : sa ligne « Projet : », sinon ~/notes. Doit exister."""
    check_name(sujet)
    fichier = prompts_dir(dossier) / f"{sujet}.md"
    work = DEFAULT_PROJET.expanduser()
    if fichier.is_file():
        work = projet(fichier.read_text(encoding="utf-8")) or work
    if not work.is_dir():
        raise InvalidRequestError(f"dossier de travail introuvable ou pas un dossier : {work}")
    return work


def nouveau(sujet: str, titre: str, objectif: str, *, fiche_existe: bool) -> str:
    """Texte d'un sujet minimal (titre, objectif, ## 1., ## 4.) ; la validation des entrées est ici."""
    if NAME_RE.fullmatch(sujet or "") is None:
        raise InvalidRequestError(f"nom de sujet refusé : {sujet!r} (a-z, 0-9 et tirets, 40 caractères au plus)")
    check_name(sujet)
    titre, objectif = " ".join((titre or "").split()), (objectif or "").strip()
    if not titre or len(titre) > TITRE_MAX:
        raise InvalidRequestError(f"titre vide ou trop long ({TITRE_MAX} caractères au plus)")
    if not objectif or len(objectif) > OBJECTIF_MAX:
        raise InvalidRequestError(f"objectif vide ou trop long ({OBJECTIF_MAX} caractères au plus)")
    if re.search(r"^## \d+\.", objectif, re.MULTILINE):
        raise InvalidRequestError("l'objectif ne doit pas contenir de titre « ## N. »")
    fiche = f"~/notes/chantier-{sujet}.md"
    lire = f"- `{fiche}` : fiche du chantier." if fiche_existe else "- Rien d'écrit pour l'instant : c'est un nouveau sujet."
    return (f"# {titre}\n\nSession précédente : aucune.\nProjet : {DEFAULT_PROJET.expanduser()}\n\n{objectif}\n\n"
            f"## 1. Lire d'abord\n{lire}\n\n## 4. Pistes de travail\n\n### A. Cadrer le chantier\n"
            f"- Écrire la fiche `{fiche}` (objectif, état des lieux, décisions), puis proposer les pistes de travail.\n")


def list_subjects(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.md")
                  if p.is_file() and not p.stem.startswith("_") and p.stem != REPRISE_NAME)


def build(sujet: str, limite: str, *, fiche: str | None = None, drapeau: str | None = None,
          dossier: str | None = None, jeton: str | None = None) -> str:
    check_name(sujet)
    folder = prompts_dir(dossier)
    subject_file, socle_file = folder / f"{sujet}.md", folder / f"{SOCLE_NAME}.md"
    mode_file = folder / f"{MODE_NAME}.md"
    for path in (socle_file, subject_file):
        if not path.is_file():
            raise InvalidRequestError(f"fichier introuvable : {path}")
    mode = mode_file.read_text(encoding="utf-8") if mode_file.is_file() else ""
    return assemble(socle_file.read_text(encoding="utf-8"), mode, subject_file.read_text(encoding="utf-8"),
                    sujet=sujet, limite=limite, fiche=fiche or f"~/notes/chantier-{sujet}.md",
                    drapeau=drapeau or DEFAULT_DRAPEAU, dossier=str(folder), jeton=jeton)


def incomplets(folder: Path) -> dict[str, str]:
    """{subject: error} for the subjects that cannot be assembled (todo #216). Writes nothing."""
    out = {}
    for name in list_subjects(folder):
        try:
            build(name, "1h", dossier=str(folder), jeton="00000000")
        except (InvalidRequestError, OSError, UnicodeError) as exc:
            out[name] = str(exc)
    return out
