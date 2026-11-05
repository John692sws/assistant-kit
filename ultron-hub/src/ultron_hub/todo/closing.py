"""Who may close a todo or approve an action: the user's Telegram button, or the user at a real terminal.

The Hermes plugin (gateway process) holds the secret `~/.config/ultron-hub/close.key` and signs each
button press: token = "<ts>.<hmac>" with hmac = HMAC-SHA256(key, "ultron-hub-close:v1:<action>:<id>:<ts>").
`<id>` is a todo number, "todo-<id>:<n>" (`user`: "✅ Exécuter", a server action without sudo declared for after 🔀), a lot's
fingerprint (`fusion-lot`: 🔀 Tout fusionner, see branch_lot.py), a proposed veille's fingerprint (`veille`: ✅ of
🔔 Veille, see veille/propose.py) or "<reflex>:<fingerprint>" (`reflexe`: the button of
a health diagnosis, see health/reflexes.py) or "<todo id>-<sha12>" (`revert-fusion`: ↩️ Annuler la dernière fusion
automatique, see auto_merge.py) or a tidy-up proposal's fingerprint (`menage-lot`: 🧹 Ménage proposé, see
menage_lot.py) or the Sunday reports' fingerprint (`rapports-lot`: ✅ of 📋 Rapports du dimanche, see rapports.py)
or "<draft folder>:<fingerprint of its text>" (`habitude`: ✅ of 💡 Nouvelle habitude proposée, see habitudes.py)
or "<video id>:<fingerprint>" (`video-lot`: ✅ Tout ranger of a video's proposals, see video/lot.py)
or "<name>:<fingerprint>" (`routine`: ✅ of 🔁 Routine proposée, see routines.py).
The phone dashboard (todo #131) signs its own proof, after checking the device (tailscale whois) and the user's
confirmation: token = "dashboard.<device>.<ts>.<hmac>" with hmac over "ultron-hub-close:v1:dashboard:<device>:<action>:
<id>:<ts>" (same key, same TTL, same replay check); the actor recorded is "dashboard:<device>". Only DASHBOARD_ACTIONS.
It reaches the CLI through the TOKEN_ENV variable (argv is visible to every user in `ps`).
`hermes_plugin/close_key.py` mirrors the signing code (the plugin cannot import ultron_hub).

`ultron-hub todo close-auto <id>` (todo #528, `verify_auto_close`) is the only unsigned close: the todo was merged
alone by `branch auto-merge` (whose tests passed, or nothing was merged), the merge is still in main, and its diff
touches nothing in `auto_merge.SECURITY`; the actor recorded is "auto:verifie" with the merge sha as proof.

`--i-am-human` is the terminal path: stdin must be a TTY, no Hermes/Claude process among the ancestors
(Hermes' terminal tool can open a pty), and a random code shown on /dev/tty must be typed back.

Everything runs as the same Unix user as the agents: this stops closing by mistake or by habit and
records who closed, it is not a barrier against an agent determined to forge (see CLAUDE.md).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import pwd
import re
import secrets
import stat
import sys
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from ultron_hub.common.errors import InvalidRequestError

# Not derived from $HOME: `HOME=/tmp/x ultron-hub ...` must not point the CLI at a key an agent wrote.
KEY_PATH = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "ultron-hub" / "close.key"
TOKEN_ENV = "ULTRON_HUB_CLOSE_TOKEN"
TOKEN_TTL_S = 120
FUTURE_SKEW_S = 10
KEY_MIN_BYTES = 32
ACTIONS = ("done", "cancel", "reopen", "merge", "profil", "user", "savoir", "escouade", "reponse",
           "fusion-lot", "veille", "reflexe", "revert-fusion", "compte", "session", "menage-lot", "rapports-lot", "habitude",
           "video-lot", "autorisation", "routine")
# Actor recorded in the status event, by signed action.
SIGNED_ACTORS = {"done": "telegram-button", "cancel": "telegram-button", "reopen": "telegram-button",
                 "merge": "telegram-merge",
                 "profil": "telegram-button", "user": "telegram-button", "savoir": "telegram-button",
                 "escouade": "telegram-button", "reponse": "telegram-button",
                 "fusion-lot": "telegram-merge", "veille": "telegram-button",
                 "reflexe": "telegram-button", "revert-fusion": "telegram-button",
                 "compte": "telegram-button", "session": "telegram-button",
                 "menage-lot": "telegram-button", "rapports-lot": "telegram-button",
                 "habitude": "telegram-button", "video-lot": "telegram-button",
                 "autorisation": "telegram-button", "routine": "telegram-button"}
HUMAN_ACTOR = "cli-human"
# Todo #529: mail sending and money, on top of auto_merge.SECURITY (which must stay untouched: it carries the user's advance
# authorisation). Nothing of the sort exists in ultron-hub today; these name patterns (fnmatch on the lowercase path)
# keep close-auto refusing the day such a module appears, here or in another repository.
AUTO_CLOSE_EXTRA = ("*smtp*", "*sendmail*", "*gmail*", "*mail_send*", "*send_mail*", "*envoi_mail*",
                    "*paiement*", "*payment*", "*stripe*", "*facture*", "*invoice*", "*virement*", "*billing*")
AUTO_VERIFIED_ACTOR = "auto:verifie"  # todo #528: closed alone after a verified automatic merge (verify_auto_close)
# Actions the dashboard may sign (todo #131: ▶️ Lancer une escouade; todo #132: ✅ done, 🔀 merge, ❓ reponse, which also
# seals a ➕ remark like Telegram; todo #242: ✖ cancel, 🔀 fusion-lot on the lot's fingerprint, ✅ Exécuter after a merge =
# `user` on "todo-<id>:<n>", the only target its catalogue entry can build; todo #249: ✅ profil on the proposal's number, 📚 savoir on the draft's fingerprint;
# todo #247: `reflexe` on the "<reflex>:<fingerprint>" of the diagnosis shown; todo #254: ↩️ `revert-fusion` on "<id>-<sha12>"; todo #263: `compte`, 🔁 Basculer on the destination slot "a"|"b" (todo #336: also 🔑 "login:<slot>", "code:<sha16 of the code>"); todo #265: `session` = write into an ultron session
# ("<name>:<sha16 of the text>") or press a choice key ("<name>:<key>")); todo #362: `menage-lot` = ✅ of 🧹 Ménage proposé on
# the proposal's fingerprint (menage_lot.py); todo #417: `rapports-lot` = ✅ of 📋 Rapports du dimanche on the lot's
# fingerprint (rapports.py); todo #429: `habitude` = ✅ of 💡 Nouvelle habitude proposée on "<folder>:<fingerprint>"
# (habitudes.py); todo #442: `video-lot` = ✅ Tout ranger of a video's proposals on "<video id>:<fingerprint>" (video/lot.py);
# todo #495: `autorisation` = the advance authorisation of automatic merges on "<perimeter>:<days>" (autorisation.py);
# todo #503: `routine` = ✅ of 🔁 Routine proposée on "<name>:<fingerprint>" (routines.py);
# "dashboard" is never an action (no clash).
DASHBOARD_ACTIONS = ("done", "merge", "cancel", "fusion-lot", "user", "profil", "savoir",
                     "reflexe", "revert-fusion", "compte", "session", "menage-lot", "rapports-lot", "habitude", "video-lot",
                     "autorisation", "routine")
DASHBOARD_ACTOR = "dashboard:"
DEVICE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
AGENT_PROCESS_RE = re.compile(r"(?:^|[/\s])(?:hermes|claude)(?:[\s.:-]|$)", re.I)
# Todo number, "todo-<id>:<n>" or another action's target; validated with fullmatch before it enters a signed message.
_TARGET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}")
_TOKEN_RE = re.compile(r"([0-9]{1,12})\.([0-9a-f]{64})")
_DASHBOARD_TOKEN_RE = re.compile(r"dashboard\.([a-z0-9][a-z0-9-]{0,39})\.([0-9]{1,12})\.([0-9a-f]{64})")
MAX_ANCESTORS = 64


class CloseRefused(InvalidRequestError):
    """The request to close does not come from the user."""


def message(action: str, target: int | str, ts: int) -> bytes:
    target = str(target)
    if _TARGET_RE.fullmatch(target) is None:
        raise CloseRefused("identifiant invalide pour une signature")
    return f"ultron-hub-close:v1:{action}:{target}:{ts}".encode()


def sign(key: bytes, action: str, target: int | str, ts: int) -> str:
    return f"{ts}.{hmac.new(key, message(action, target, ts), hashlib.sha256).hexdigest()}"


def dashboard_message(device: str, action: str, target: int | str, ts: int) -> bytes:
    if action not in DASHBOARD_ACTIONS:
        raise CloseRefused(f"action non signable depuis le dashboard : {action}")
    if DEVICE_RE.fullmatch(device or "") is None:
        raise CloseRefused("appareil invalide pour une signature")
    target = str(target)
    if _TARGET_RE.fullmatch(target) is None:
        raise CloseRefused("identifiant invalide pour une signature")
    return f"ultron-hub-close:v1:dashboard:{device}:{action}:{target}:{ts}".encode()


def sign_dashboard(key: bytes, device: str, action: str, target: int | str, ts: int) -> str:
    """The dashboard's proof; the caller has already checked the device and the user's confirmation."""
    return f"dashboard.{device}.{ts}.{hmac.new(key, dashboard_message(device, action, target, ts), hashlib.sha256).hexdigest()}"


def load_key(path: Path | None = None) -> bytes:
    """The key, only if it is a regular file owned by us and readable by nobody else."""
    path = path or KEY_PATH
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise CloseRefused("clé de clôture absente : le plugin Telegram la crée au premier appui sur un bouton") from None
    except OSError as exc:
        raise CloseRefused(f"clé de clôture illisible ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise CloseRefused("clé de clôture refusée : doit être un fichier à nous, en mode 0600")
        key = fh.read().strip()
    if len(key) < KEY_MIN_BYTES:
        raise CloseRefused("clé de clôture trop courte")
    return key


def check(token: str, action: str, todo_id: int | str, key: bytes | None = None,
          now: float | None = None) -> tuple[str, str]:
    """Check a Telegram or dashboard token; returns (short proof id, actor). The proof is recorded, refused if used again."""
    m = _TOKEN_RE.fullmatch(token or "")
    device = None
    if m is None:
        d = _DASHBOARD_TOKEN_RE.fullmatch(token or "")
        if d is None:
            raise CloseRefused("jeton de clôture mal formé")
        if action not in DASHBOARD_ACTIONS:
            raise CloseRefused(f"jeton du dashboard refusé pour l'action {action}")
        device, ts_text, digest = d[1], d[2], d[3]
    else:
        ts_text, digest = m[1], m[2]
    ts = int(ts_text)
    now = time.time() if now is None else now
    if not (now - TOKEN_TTL_S <= ts <= now + FUTURE_SKEW_S):
        raise CloseRefused("jeton de clôture expiré")
    key = key if key is not None else load_key()
    expected = sign(key, action, todo_id, ts) if device is None else sign_dashboard(key, device, action, todo_id, ts)
    if not hmac.compare_digest(expected, token):
        raise CloseRefused("jeton de clôture invalide")
    return digest[:16], (SIGNED_ACTORS[action] if device is None else DASHBOARD_ACTOR + device)


def verify(token: str, action: str, todo_id: int | str, key: bytes | None = None, now: float | None = None) -> str:
    """Check a button token; returns its short proof id (recorded, and refused if used again)."""
    return check(token, action, todo_id, key, now)[0]


# -- terminal path -------------------------------------------------------------------------

def _ancestors() -> list[str]:
    """Command lines of our parent processes, nearest first."""
    lines, pid = [], os.getppid()
    for _ in range(MAX_ANCESTORS):
        if pid <= 1:
            break
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        lines.append(raw.replace(b"\0", b" ").decode("utf-8", "replace").strip())
        pid = ppid
    return lines


def _ask(prompt: str) -> str:
    with open("/dev/tty", "r+", encoding="utf-8") as tty:
        tty.write(prompt)
        tty.flush()
        return tty.readline().strip()


def confirm_human(what: str) -> dict:
    """Interactive confirmation at a real terminal; returns the event details."""
    if not sys.stdin.isatty():
        raise CloseRefused("--i-am-human exige un terminal interactif (stdin n'est pas un TTY)")
    agent = next((line for line in _ancestors() if AGENT_PROCESS_RE.search(line)), None)
    if agent is not None:
        raise CloseRefused("--i-am-human refusé : lancé depuis un agent (Hermes/Claude), utilise le bouton Telegram")
    code = f"{secrets.randbelow(10000):04d}"
    try:
        answer = _ask(f"{what}\nTape {code} pour confirmer : ")
    except OSError as exc:
        raise CloseRefused(f"--i-am-human : terminal inaccessible ({exc.strerror})") from None
    if answer != code:
        raise CloseRefused("confirmation incorrecte : rien n'a changé")
    try:
        tty = os.ttyname(0)
    except OSError:
        tty = None
    return {"by": HUMAN_ACTOR, "tty": tty}


def refusal_hint(action: str, target: int | str) -> str:
    if action == "user":
        owner, _, n = str(target).partition(":")
        todo_id = owner.removeprefix("todo-")
        return (f"bouton « ✅ Exécuter » Telegram (après 🔀 de la tâche #{todo_id}), "
                f"ou en terminal `ultron-hub branch after-run {todo_id} {n} --i-am-human`")
    if action == "profil":
        return f"bouton « ✅ Ajouter » de /profil sur Telegram, ou en terminal `ultron-hub profil accept {target} --i-am-human`"
    if action == "menage-lot":
        return (f"bouton « ✅ Tout appliquer » de 🧹 Ménage proposé sur le dashboard, "
                f"ou en terminal `ultron-hub menage appliquer --empreinte {target} --i-am-human`")
    if action == "rapports-lot":
        return (f"bouton « ✅ Appliquer les actions conseillées » des Rapports du dimanche (onglet Moi du dashboard), "
                f"ou en terminal `ultron-hub rapports appliquer --empreinte {target} --i-am-human`")
    if action == "video-lot":
        vid, _, emp = str(target).partition(":")
        return (f"bouton « ✅ Tout ranger » des propositions de la vidéo sur le dashboard, "
                f"ou en terminal `ultron-hub video ranger-lot {vid} --empreinte {emp} --i-am-human`")
    if action == "habitude":
        dossier, _, emp = str(target).partition(":")
        return (f"bouton ✅ de la carte « Nouvelle habitude proposée » (onglet Moi du dashboard), "
                f"ou en terminal `ultron-hub habitudes installer --dossier {dossier} --empreinte {emp} --i-am-human`")
    if action == "routine":
        nom, _, emp = str(target).partition(":")
        return (f"bouton ✅ de la carte « Routine proposée » (onglet À toi du dashboard), "
                f"ou en terminal `ultron-hub routine activer --nom {nom} --empreinte {emp} --i-am-human`")
    if action == "savoir":
        return ("bouton « ✅ Ranger » de la fiche sur Telegram, "
                "ou en terminal `ultron-hub savoir ranger <brouillon.md> --i-am-human`")
    if action == "escouade":
        return (f"bouton ▶️ Lancer de /escouade sur Telegram, "
                f"ou en terminal `ultron-hub escouade run {target} --duree 3h --i-am-human`")
    if action == "reponse":
        q = str(target).partition(":")[0]
        return (f"bouton de la question {q} sur Telegram, "
                f"ou en terminal `ultron-hub escouade repondre {q} --type texte --i-am-human`")
    if action == "fusion-lot":
        return ("bouton ✅ de « 🔀 Tout fusionner » (📥 À toi) sur Telegram, ou en terminal "
                f"`ultron-hub branch merge-lot --ids <ids> --empreinte {target} --i-am-human`")
    if action == "veille":
        return ("bouton ✅ de la carte 🔔 Veille sur Telegram, ou en terminal "
                f"`ultron-hub veille creer <proposition> --empreinte {target} --i-am-human`")
    if action == "reflexe":
        return (f"bouton du diagnostic dans /sante sur Telegram, "
                f"ou en terminal `ultron-hub health reflexe-agir {target} --i-am-human`")
    if action == "session" and str(target).startswith("nouvelle:"):
        return ("dashboard du téléphone (💬 Sessions ultron → ➕ Nouvelle session), ou en terminal "
                "`ultron-hub session nouvelle --consigne-stdin --i-am-human`")
    if action == "session":
        nom = str(target).partition(":")[0]
        return (f"dashboard du téléphone (💬 Sessions ultron → {nom}), ou en terminal "
                f"`ultron-hub session ecrire|choisir {nom} … --i-am-human`")
    if action == "revert-fusion":
        return ("bouton ↩️ Annuler la dernière fusion du dashboard, ou en terminal "
                f"`ultron-hub branch revert-last --cible {target} --i-am-human`")
    if action == "autorisation":
        perim, _, jours = str(target).partition(":")
        return ("bouton « Autorisation d'avance » de 🌿 État des fusions sur le dashboard, ou en terminal "
                f"`ultron-hub branch autorisation donner --jours {jours or 'N'} --perimetre {perim} --i-am-human`")
    if action == "compte" and str(target).startswith(("login:", "code:")):  # todo #336
        return ("bouton 🔑 Se reconnecter du dashboard (carte Comptes), ou en terminal "
                "`ultron-hub compte connexion demarrer|code … --i-am-human`")
    if action == "compte":
        return f"bouton 🔁 Basculer du dashboard (carte Comptes), ou en terminal `ultron-hub compte basculer --vers {target} --i-am-human`"
    cmd = "branch merge" if action == "merge" else "todo " + action
    return f"bouton Telegram (/todo → fiche #{target}), ou en terminal `ultron-hub {cmd} {target} --i-am-human`"


def authorize(action: str, todo_id: int | str, *, human: bool, what: str, used: Callable[[str], bool],
              required: bool = True, env: dict | None = None) -> dict:
    """Event details naming who asked, or CloseRefused.

    A token in the environment is always checked; without one, `--i-am-human` asks at the terminal;
    otherwise the action is refused if `required` (everything but reopen) or recorded as plain "cli".
    `used(proof)` says whether this token was already spent on this task (replay).
    """
    if action not in ACTIONS:
        raise ValueError(action)
    token = (os.environ if env is None else env).get(TOKEN_ENV)
    if token:
        proof, by = check(token, action, todo_id)
        if used(proof):
            raise CloseRefused("jeton de clôture déjà utilisé")
        return {"by": by, "proof": proof}
    if human:
        return confirm_human(what)
    if required:
        raise CloseRefused(f"réservé à l'utilisateur : {refusal_hint(action, todo_id)}")
    return {"by": "cli"}


# -- verified automatic close (todo #528) -----------------------------------------------------------------------------

AUTO_MERGE_SUBJECTS = ("Fusion automatique de todo-{id}/", "Fusion de todo-{id}/")


def _refuse_auto(settings, todo_id: int, step: str, reason: str) -> None:
    """WARNING + journal like the other refusals, then CloseRefused; the todo stays open."""
    from ultron_hub.common.logging import get_logger
    from ultron_hub.esprit import store as journal
    get_logger("closing").warning("todo=%d step=%s reason=%s", todo_id, step, reason)
    journal.record(settings, "ultron-hub", "todo", f"Clôture auto refusée : {reason}", todo_id)
    raise CloseRefused(f"❌ Clôture auto refusée : {reason}. La tâche #{todo_id} reste ouverte (✅ de l'utilisateur).")


def verify_auto_close(settings, store, todo_id: int, env: dict | None = None) -> dict:
    """Event details {"by": "auto:verifie", "proof": sha12, ...} if the todo may be closed without the user, else CloseRefused.

    Proofs, all required: called outside a job or a squad (like auto_merge); the todo is open; its last automatic merge
    (var/fusions-auto.jsonl, written by auto_merge after the tests passed on the merged result) is not reverted and
    is the one its last `livree` event (by auto:orchestrateur) names; each merge commit is still in main, has two
    parents and the subject "Fusion [automatique ]de todo-<id>/…"; no new todo-<id>/… branch is waiting; the merged
    diff touches nothing in auto_merge.SECURITY (secrets, signing, sending, deletion, root: the user's ✅).
    """
    from ultron_hub import auto_merge, branches, repos  # late: auto_merge imports the todo store

    job = auto_merge.check_caller(env)
    if job:
        _refuse_auto(settings, todo_id, "close-auto-mission", f"clôture auto refusée depuis un job ou une escouade ({job})")
    task = store.get(todo_id)
    if task.status != "todo":
        _refuse_auto(settings, todo_id, "close-auto-status", f"la tâche #{todo_id} n'est pas ouverte ({task.status})")
    entries = auto_merge._entries(settings)
    fusion = next((e for e in reversed(entries) if e.get("type") == "fusion" and e.get("todo_id") == todo_id
                   and e.get("by") == auto_merge.ACTOR and e.get("repos")), None)
    if fusion is None:
        _refuse_auto(settings, todo_id, "close-auto-unmerged",
                     f"aucune fusion automatique enregistrée pour la tâche #{todo_id} (branche non fusionnée)")
    cible = auto_merge.target(fusion)
    if any(e.get("type") == "revert" and e.get("cible") == cible for e in entries):
        _refuse_auto(settings, todo_id, "close-auto-reverted", f"la fusion automatique {cible} a été annulée")
    shas = [str(x.get("sha") or "") for x in fusion["repos"]]
    history = store.history(todo_id)
    last = next((i for i in range(len(history) - 1, -1, -1) if history[i]["action"] == "livree"), None)
    ev = history[last]["details"] if last is not None else {}
    if (last is None or ev.get("by") != auto_merge.ACTOR
            or not all(s and s[:auto_merge.SHA12_LEN] in str(ev.get("note", "")) for s in shas)):
        _refuse_auto(settings, todo_id, "close-auto-no-proof",
                     "pas de preuve de vérification : la dernière livraison n'est pas la fusion automatique testée")
    if any(h["action"] in ("status", "fusion-annulee") for h in history[last + 1:]):
        _refuse_auto(settings, todo_id, "close-auto-no-proof", "la tâche a changé depuis la fusion automatique")
    known = repos.list_repos(settings)
    subjects = tuple(s.format(id=todo_id) for s in AUTO_MERGE_SUBJECTS)
    touched: list[str] = []
    for x, sha in zip(fusion["repos"], shas):
        root = known.get(x.get("repo"))
        if root is None:
            _refuse_auto(settings, todo_id, "close-auto-unmerged", f"dépôt {x.get('repo')} introuvable")
        in_main = branches.subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", sha, branches.MAIN],
                                          capture_output=True, text=True, timeout=branches.GIT_TIMEOUT_S,
                                          env=branches._env(), check=False).returncode == 0
        line = branches.git(root, "log", "-1", "--format=%P%x00%s", sha, check=False) if in_main else ""
        parents, _, subject = line.partition("\0")
        if not in_main or len(parents.split()) != 2 or not subject.startswith(subjects):
            _refuse_auto(settings, todo_id, "close-auto-unmerged",
                         f"la fusion {sha[:auto_merge.SHA12_LEN]} de todo-{todo_id}/… n'est pas dans main de {x.get('repo')}")
        files = branches.git(root, "diff", "--name-only", "--no-renames", f"{sha}^1", sha).splitlines()
        covered = auto_merge.security(files)
        risky = covered + [f for f in files if f not in covered and any(fnmatch(f.lower(), pat) for pat in AUTO_CLOSE_EXTRA)]
        touched += [f if root == settings.root else f"{f} ({x.get('repo')})" for f in risky]
    waiting = [b for t in branches.targets(settings, todo_id) for b in t["branches"]]
    if waiting:
        _refuse_auto(settings, todo_id, "close-auto-branch", f"une branche attend encore : {', '.join(waiting)}")
    if touched:
        _refuse_auto(settings, todo_id, "close-auto-security",
                     "la fusion touche un fichier de sécurité (✅ de l'utilisateur) : " + ", ".join(touched[:10]))
    return {"by": AUTO_VERIFIED_ACTOR, "proof": shas[0][:auto_merge.SHA12_LEN], "via": "close-auto", "cible": cible,
            "fusion": [{"repo": x.get("repo"), "sha": sha} for x, sha in zip(fusion["repos"], shas)]}
