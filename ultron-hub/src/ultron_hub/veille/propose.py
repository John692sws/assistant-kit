"""A veille made from Telegram (todo #86): the user's request + a link → a script written by Claude, tried twice in
the sandbox, shown on a card, put in service by one signed ✅.

`propose`: one https link (fetch_pages rules), duplicate and ceiling checks, one download (outside the sandbox),
task `veille-script` (sonnet, no tools, request and page as untrusted data), then two dry runs in the sandbox: run 1
with `state: null` gives the starting state, run 2 with that state and the same page must say `new: false`. A failed
draft or run = ONE new try with the error; a second failure = nothing is created.
The proposal is kept in var/veilles-propositions/<id>/ (0700): `check`, `veille.json` (draft), `essais.json`,
`proposition.json`. Its fingerprint = 16 hex of SHA-256(check + NUL + canonical veille.json).

`creer`: the fingerprint is computed again (different = refused), the signed token (action `veille`, target = that
fingerprint, never replayed) or `--i-am-human` is required, then the veille is added like `veille add --url` with the
starting state of run 1. Scripts written by Claude get pages only, never `lecture` (symlink race noted by #87).
"""

import ast
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from ultron_hub.common.errors import InvalidRequestError, UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.core.job import JobRequest, new_job_id
from ultron_hub.core.runner import run_job
from ultron_hub.todo import closing
from ultron_hub.veille import core, sandbox

log = get_logger("veille")

TASK = "veille-script"
REQUEST_MAX = 1000
PAGE_PROMPT_MAX = 30_000
SCRIPT_MAX = 8 * 1024
EVERY_MIN, EVERY_DEFAULT = 15, 30
ACTIVE_MAX = 10
TRIAL_TIMEOUT_S = 30
PURGE_S = 24 * 3600
TRIGGER_MAX, EXAMPLE_MAX = 200, 300
SHEBANG = "#!/usr/bin/python3"
ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}")
URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
URL_TRAIL = ".,;:!?)]}»\"'›"
SCRIPT_FILE, DRAFT, TRIALS, PROPOSAL, PROOFS = "check", "veille.json", "essais.json", "proposition.json", "preuves.json"
PROOFS_KEPT = 500
STRIP_RE = re.compile(r"<(script|style|svg|noscript)\b.*?</\1\s*>|<!--.*?-->", re.I | re.S)


class Refused(UltronHubError):
    """The request cannot become a veille: nothing was created."""


class ScriptRefused(UltronHubError):
    """Claude's draft or one of its dry runs failed (the reason goes back to Claude once)."""


def root(settings: Settings) -> Path:
    return settings.var_dir / "veilles-propositions"


def folder(settings: Settings, pid: Any) -> Path:
    if not isinstance(pid, str) or ID_RE.fullmatch(pid) is None:
        raise Refused("identifiant de proposition invalide")
    return root(settings) / pid


def neutral(text: Any) -> str:
    """Placed between the tags of the prompt: it cannot close them."""
    return str(text or "").replace("<", "‹").replace(">", "›")


def extract_url(text: str) -> str:
    found = []
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(URL_TRAIL)
        if url not in found:
            found.append(url)
    if not found:
        raise Refused("il me faut le lien de la page à surveiller (https://…)")
    if len(found) > 1:
        raise Refused("un seul lien par veille, s'il te plaît")
    try:
        return sandbox.check_url(found[0])
    except InvalidRequestError as exc:
        raise Refused(f"lien refusé : {exc}") from None


def page_for_prompt(text: str) -> str:
    """The page as Claude reads it: no scripts, styles or comments, spaces folded, 30 000 characters, < > neutralised."""
    cleaned = re.sub(r"\s+", " ", STRIP_RE.sub(" ", text or "")).strip()
    return neutral(cleaned[:PAGE_PROMPT_MAX])


def canonical(draft: dict[str, Any]) -> bytes:
    return json.dumps(draft, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def fingerprint(script: bytes, draft: dict[str, Any]) -> str:
    return hashlib.sha256(script + b"\0" + canonical(draft)).hexdigest()[:16]


def active_veilles() -> list[dict[str, Any]]:
    return [row for row in core.entries() if not row["paused"]]


def check_ceiling() -> None:
    active = active_veilles()
    if len(active) >= ACTIVE_MAX:
        names = ", ".join(f"{r['nom']} ({r['description']})" for r in active)
        raise Refused(f"déjà {len(active)} veilles actives (plafond {ACTIVE_MAX}) : {names}. "
                      "Mets-en une en pause ou retire-la avant d'en créer une autre.")


def duplicate(url: str) -> dict[str, Any] | None:
    for name in core.names():
        try:
            config = core.load(name)
        except InvalidRequestError:
            continue
        if url in (config.get("urls") or []):
            return {"nom": name, "description": config.get("description", "")}
    return None


def _free_name(name: str) -> str:
    taken = set(core.names())
    candidate, n = name, 2
    while candidate in taken or core.folder(candidate).exists():
        suffix = f"-{n}"
        candidate = name[:40 - len(suffix)].rstrip("-") + suffix
        n += 1
    return candidate


def _one_line(value: Any, limit: int) -> str:
    line = " ".join(str(value or "").split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def check_script(text: Any) -> bytes:
    """Python 3, standard library only, 8 Kio at most; the first line is always our shebang."""
    if not isinstance(text, str) or not text.strip():
        raise ScriptRefused("script vide")
    lines = text.replace("\r\n", "\n").strip("\n").split("\n")
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    source = "\n".join([SHEBANG, *lines]) + "\n"
    data = source.encode("utf-8")
    if len(data) > SCRIPT_MAX:
        raise ScriptRefused(f"script trop long ({len(data)} octets, {SCRIPT_MAX // 1024} Kio au plus)")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ScriptRefused(f"script invalide (erreur de syntaxe ligne {exc.lineno})") from None
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""] if not node.level else ["(import relatif)"]
        for module in modules:
            if module.split(".")[0] not in sys.stdlib_module_names:
                raise ScriptRefused(f"module hors bibliothèque standard : {module}")
    return data


def check_draft(raw: Any, url: str) -> tuple[dict[str, Any], bytes, dict[str, str]]:
    """Claude's answer → (draft veille.json without the starting state, script bytes, card texts)."""
    if not isinstance(raw, dict):
        raise ScriptRefused("réponse sans les champs attendus")
    script = check_script(raw.get("script"))
    name = raw.get("nom")
    if not isinstance(name, str) or core.NAME_RE.fullmatch(name) is None:
        name = "veille-web"
    description = _one_line(raw.get("description"), core.DESCRIPTION_MAX)
    if not description:
        raise ScriptRefused("description vide")
    every = raw.get("every_min")
    if isinstance(every, bool) or not isinstance(every, int):
        every = EVERY_DEFAULT
    every = min(max(every, EVERY_MIN), core.EVERY_MAX)  # politeness: never more than once every 15 minutes
    draft = {"nom": _free_name(name), "description": description, "every_min": every, "timeout_s": TRIAL_TIMEOUT_S,
             "urls": [url], "lecture": []}
    card = {"declencheur": _one_line(raw.get("declencheur"), TRIGGER_MAX) or "la cible change",
            "exemple_message": _one_line(raw.get("exemple_message"), EXAMPLE_MAX)}
    return draft, script, card


def trial(path: Path, script: bytes, page: dict[str, Any]) -> list[dict[str, Any]]:
    """Two dry runs in the sandbox (no `lecture`). Run 2 on the same page with run 1's state must say new: false."""
    target = path / SCRIPT_FILE
    target.unlink(missing_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, "wb") as fh:
        fh.write(script)
    runs = []
    state = None
    for n in (1, 2):
        try:
            result = core.execute(target, path, [], f"essai-{n}", {"state": state, "pages": [page]}, TRIAL_TIMEOUT_S)
        except core.CheckFailed as exc:
            raise ScriptRefused(f"essai {n} échoué : {exc}") from None
        if len(json.dumps(result["state"], ensure_ascii=False)) > 2000:
            raise ScriptRefused(f"essai {n} : état trop gros (2 000 caractères au plus)")
        runs.append(result)
        state = result["state"]
    if runs[1]["new"]:
        raise ScriptRefused("essai 2 : new vaut true alors que la page n'a pas changé (fausse alerte)")
    return runs


def ask_claude(settings: Settings, metrics: dict[str, Any], requested_by: str | None) -> Any:
    """The task veille-script; raises Refused when Claude itself fails (quota, login): no new try then."""
    request = JobRequest(action=TASK, params={"metrics": metrics}, job_id=new_job_id(), requested_by=requested_by)
    result = run_job(request, settings)
    if not result.ok:
        raise Refused(f"je n'ai pas pu écrire le script : {result.error or result.summary or 'erreur inconnue'}")
    return result.data


def _write(path: Path, data: Any) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def _read(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise Refused("proposition incomplète : refais 🔔 Veille")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused("proposition illisible : refais 🔔 Veille") from None


def purge(settings: Settings, now: float | None = None) -> list[str]:
    base = root(settings)
    if not base.is_dir():
        return []
    now = time.time() if now is None else now
    removed = []
    for path in base.iterdir():
        if ID_RE.fullmatch(path.name) is None or path.is_symlink() or not path.is_dir():
            continue
        if now - path.stat().st_mtime > PURGE_S:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path.name)
    return removed


AskClaude = Callable[[Settings, dict[str, Any], str | None], Any]


def propose(settings: Settings, text: str, requested_by: str | None = None, depuis: str | None = None,
            ask: AskClaude | None = None) -> dict[str, Any]:
    """`text` = the request (stdin). `depuis` = a proposal to make precise (✏️ Préciser, once): `text` is then the
    precision, added to that proposal's request."""
    ask = ask or ask_claude
    purge(settings)
    text = (text or "").strip()
    if not text or len(text) > REQUEST_MAX:
        raise Refused(f"demande de 1 à {REQUEST_MAX} caractères, s'il te plaît")
    if depuis is not None:
        previous = _read(folder(settings, depuis) / PROPOSAL)
        if previous.get("precisee"):
            raise Refused("cette proposition a déjà été précisée une fois : refais 🔔 Veille")
        text = f"{previous.get('demande', '')}\nPrécision : {text}"[:REQUEST_MAX * 2 + 20]
    url = extract_url(text)
    found = duplicate(url)
    if found:
        return {"doublon": found, "url": url}
    check_ceiling()
    page = sandbox.fetch_page(url)
    if "erreur" in page:
        raise Refused(f"page illisible : {page['erreur']}")
    pid = new_job_id()
    path = folder(settings, pid)
    root(settings).mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    try:
        draft, script, card, runs, attempts = _write_and_try(settings, path, text, url, page, requested_by, ask)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)  # nothing is kept from a request that failed
        raise
    draft["etat_initial"] = runs[0]["state"]
    empreinte = fingerprint(script, draft)
    host = urllib.parse.urlsplit(url).hostname or ""
    lines = script.decode("utf-8").count("\n")
    proposal = {"id": pid, "empreinte": empreinte, "nom": draft["nom"], "description": draft["description"],
                "every_min": draft["every_min"], "url": url, "domaine": host, **card, "lignes": lines,
                "script": script.decode("utf-8"), "essais": 2, "relance": attempts == 2,
                "precisee": depuis is not None, "demande": text, "requested_by": requested_by}
    _write(path / DRAFT, draft)
    _write(path / TRIALS, [{"essai": n, **r} for n, r in enumerate(runs, 1)])
    _write(path / PROPOSAL, proposal)
    return proposal


def _write_and_try(settings: Settings, path: Path, text: str, url: str, page: dict[str, Any], requested_by: str | None,
                   ask: AskClaude) -> tuple[dict[str, Any], bytes, dict[str, str], list[dict[str, Any]], int]:
    metrics: dict[str, Any] = {"demande": neutral(text), "url": url, "page": page_for_prompt(page.get("texte", ""))}
    error = ""
    for attempt in (1, 2):
        raw = ask(settings, metrics, requested_by)
        try:
            draft, script, card = check_draft(raw, url)
            return draft, script, card, trial(path, script, page), attempt
        except ScriptRefused as exc:
            error = str(exc)
            log.info("veille proposal %s: try %d refused (%s)", path.name, attempt, error)
            script_text = raw.get("script") if isinstance(raw, dict) else ""
            metrics["essai_precedent"] = {"erreur": neutral(error), "script": neutral(str(script_text or "")[:SCRIPT_MAX])}
    raise Refused(f"je n'ai pas réussi à écrire une veille fiable (deux essais) : {error}")


def _proofs(settings: Settings) -> list[str]:
    path = root(settings) / PROOFS
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() and not path.is_symlink() else []
    except (OSError, ValueError):
        data = []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


def _spend(settings: Settings, proof: str) -> None:
    root(settings).mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(root(settings) / PROOFS, [*_proofs(settings), proof][-PROOFS_KEPT:])


def creer(settings: Settings, pid: str, empreinte: str, human: bool = False) -> dict[str, Any]:
    if not isinstance(empreinte, str) or FINGERPRINT_RE.fullmatch(empreinte) is None:
        raise Refused("empreinte invalide (16 caractères hexadécimaux)")
    path = folder(settings, pid)
    if not path.is_dir() or path.is_symlink():
        raise Refused("proposition introuvable (expirée après 24 h ?) : refais 🔔 Veille")
    script_path = path / SCRIPT_FILE
    if script_path.is_symlink() or not script_path.is_file():
        raise Refused("proposition incomplète : refais 🔔 Veille")
    script = script_path.read_bytes()   # read once: these very bytes are checked, then installed
    draft = _read(path / DRAFT)
    if not isinstance(draft, dict) or fingerprint(script, draft) != empreinte:
        raise Refused("la proposition a changé depuis la carte (empreinte différente) : rien n'est créé")
    proposal = _read(path / PROPOSAL)
    by = closing.authorize("veille", empreinte, human=human,
                           what=f"Mettre en service la veille « {draft.get('description', '')} » ?",
                           used=lambda proof: proof in _proofs(settings))
    if by.get("proof"):
        _spend(settings, by["proof"])
    if proposal.get("creee"):
        raise Refused(f"cette veille est déjà en service : {proposal['creee']}")
    urls = draft.get("urls") or []
    if len(urls) != 1:
        raise Refused("proposition invalide : une seule page attendue")
    found = duplicate(urls[0])
    if found:
        raise Refused(f"une veille existe déjà sur ce lien : {found['nom']} ({found['description']})")
    check_ceiling()
    every = draft.get("every_min")
    if isinstance(every, bool) or not isinstance(every, int) or every < EVERY_MIN:
        raise Refused(f"fréquence invalide ({EVERY_MIN} min au moins)")
    # Pages only, never `lecture`, whatever the draft says.
    config = core.add(draft.get("nom"), script, every, draft.get("description", ""), draft.get("timeout_s", TRIAL_TIMEOUT_S),
                      lecture=[], urls=urls)
    core.seed_state(config["nom"], draft.get("etat_initial"))
    proposal.update(creee=config["nom"], par=by.get("by"))
    _write(path / PROPOSAL, proposal)
    return {**config, "par": by.get("by")}
