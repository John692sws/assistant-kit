"""MCP stdio server `ultron` of a squad (todo #106): tools `demander` and `signaler`, nothing to answer.

Standard library only, never `import ultron_hub`: launched with `python -I -S` from a frozen copy, like hooks.py.
Box (one per subject): <boites>/<sujet>/questions/Q<n>.json, reponses/Q<n>.json; <boites>/compteur (flock).
An answer counts only with a valid seal: HMAC-SHA256 of "ultron-hub-close:v1:reponse:<Q>:<sha256(valeur)[:16]>:<ts>"
with close.key (same message as todo/closing.py), ts not older than the question.
"""

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import pwd
import queue
import re
import stat
import sys
import threading
import time
from pathlib import Path

KEY_PATH = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "ultron-hub" / "close.key"
TYPES = ("choix", "libre", "action")
FIXED_VALUES = {"fait": "fait", "refus": "refus", "decide": "decide"}  # signed value of the button answers
MAX_BLOCKING = 3
BEAT_FILE = "attente.battement"  # touched every 30 s while `demander` waits (todo #118)
QID_RE = re.compile(r"Q[0-9]{1,9}")
REMARK_RE = re.compile(r"R-[0-9]{8}T[0-9]{6}-[0-9a-f]{4}")
SUBJECT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_SEAL_RE = re.compile(r"([0-9]{1,12})\.([0-9a-f]{64})")


def value_hash(valeur: str) -> str:
    return hashlib.sha256(valeur.encode()).hexdigest()[:16]


def seal_message(qid: str, valeur: str, ts: int) -> bytes:
    return f"ultron-hub-close:v1:reponse:{qid}:{value_hash(valeur)}:{ts}".encode()


def load_key(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return None
        key = fh.read().strip()
    return key if len(key) >= 32 else None


def seal_ok(key: bytes | None, qid: str, answer: dict, not_before: float) -> bool:
    if key is None or not isinstance(answer.get("valeur"), str):
        return False
    m = _SEAL_RE.fullmatch(str(answer.get("sceau", "")))
    if m is None or int(m[1]) < not_before - 10:
        return False
    expected = hmac.new(key, seal_message(qid, answer["valeur"], int(m[1])), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, m[2])


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Box:
    def __init__(self, folder: Path):
        self.folder = folder
        self.root = folder.parent

    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        fh = (self.root / ".lock").open("a")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def next_id(self) -> str:
        with self._lock():
            counter = self.root / "compteur"
            try:
                n = int(counter.read_text().strip() or 0) + 1
            except (OSError, ValueError):
                n = 1
            counter.write_text(f"{n}\n")
        return f"Q{n}"

    def question_path(self, qid: str) -> Path:
        return self.folder / "questions" / f"{qid}.json"

    def question(self, qid: str) -> dict | None:
        return _read_json(self.question_path(qid)) if QID_RE.fullmatch(qid or "") else None

    def update(self, qid: str, **fields) -> dict | None:
        with self._lock():
            q = self.question(qid)
            if q is not None:
                q.update(fields)
                _write_json(self.question_path(qid), q)
        return q

    def add(self, q: dict) -> None:
        _write_json(self.question_path(q["id"]), q)
        (self.root / ".a-envoyer").touch()

    def questions(self) -> list[dict]:
        out = [_read_json(p) for p in sorted((self.folder / "questions").glob("Q*.json"))]
        return [q for q in out if q]

    def answer(self, qid: str) -> dict | None:
        return _read_json(self.folder / "reponses" / f"{qid}.json")

    def take_mail(self, key: bytes | None, squad_id: str, reader: str) -> list[str]:
        """Sealed remarks and answers to `signaler` questions not read yet, marked read under the lock (once)."""
        out: list[str] = []
        with self._lock():
            for path in sorted((self.folder / "remarques").glob("R-*.json")):
                r = _read_json(path)
                if not (REMARK_RE.fullmatch(path.stem) and r and isinstance(r.get("texte"), str)
                        and seal_ok(key, remark_seal_id(self.folder.name), dict(r, valeur=r["texte"]), 0)):
                    continue
                read_dir = self.folder / "lues"
                read_dir.mkdir(parents=True, exist_ok=True)
                _write_json(read_dir / path.name, dict(r, lue_at=time.time(), lue_par=reader))
                path.unlink()
                out.append(f"Remarque ({time.strftime('%H:%M', time.localtime(r.get('at') or 0))}) : {r['texte']}")
            for q in self.questions():
                if q.get("bloquante") or q.get("statut") != "ouverte" or q.get("escouade") != squad_id:
                    continue
                a = self.answer(q["id"])
                if a is None or not seal_ok(key, q["id"], a, q.get("cree") or 0):
                    continue
                q.update(statut="repondue", reponse_lue=time.time())
                _write_json(self.question_path(q["id"]), q)
                value = "décide toi-même, note à confirmer" if a.get("type") == "decide" else a["valeur"]
                out.append(f"Réponse à {q['id']} ({str(q.get('texte', ''))[:120]}) : {value}")
        return out


def remark_seal_id(chantier: str) -> str:
    """Seal target prefix of a remark: the signed message is that of an answer to "R:<chantier>"."""
    return f"R:{chantier}"


def frame(jeton: str, items: list[str]) -> str:
    """User messages in the fixed frame of the session token (socle, section 6); nothing without a token."""
    if not items or not re.fullmatch(r"[0-9a-f]{8}", jeton or ""):
        return ""
    return "\n".join(f"[MESSAGE DE L'UTILISATEUR {jeton}] {t} [/MESSAGE]" for t in items)


class Server:
    def __init__(self, args):
        self.args = args
        self.box = Box(Path(args.boite))
        self.squad = Path(args.escouade_dir)
        self.key_path = Path(args.cle) if args.cle else KEY_PATH
        self.inbox: "queue.Queue[dict | None]" = queue.Queue()
        self.closed = False
        self.clock, self.sleep = time.time, time.sleep  # injectable: tests drive a fake clock

    # -- I/O --
    def send(self, msg: dict) -> None:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def reader(self) -> None:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.inbox.put(msg)
        self.inbox.put(None)

    def session(self) -> tuple[str, int]:
        etat = _read_json(self.squad / "etat.json") or {}
        return str(etat.get("id", self.squad.name)), int(etat.get("numero") or 1)

    def mail(self) -> str:
        """Remarks and `signaler` answers waiting, framed with the session token (marked read only if framed)."""
        etat = _read_json(self.squad / "etat.json") or {}
        if not re.fullmatch(r"[0-9a-f]{8}", str(etat.get("jeton") or "")):
            return ""
        squad_id, _ = self.session()
        return frame(etat["jeton"], self.box.take_mail(load_key(self.key_path), squad_id, "demander"))

    # -- tools --
    def tools(self) -> list[dict]:
        props = {"question": {"type": "string", "maxLength": 800},
                 "choix": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 4},
                 "type": {"type": "string", "enum": list(TYPES)},
                 "commande": {"type": "string", "maxLength": 300}}
        ask = dict(props, attente_max_min={"type": "number"}, reprendre={"type": "string"})
        return [
            {"name": "demander", "description": "Pose une question bloquante à l'utilisateur et attend sa réponse "
             "(ou reprend une question posée par signaler : reprendre = \"Q…\").",
             "inputSchema": {"type": "object", "properties": ask}},
            {"name": "signaler", "description": "Pose une question sans attendre ; la réponse arrivera au fil du travail.",
             "inputSchema": {"type": "object", "properties": props, "required": ["question"]}},
        ]

    def _new_question(self, a: dict, bloquante: bool) -> dict:
        texte = a.get("question")
        if not isinstance(texte, str) or not texte.strip() or len(texte) > 800:
            raise ValueError("question vide ou trop longue (800 caractères au plus)")
        choix = a.get("choix") or []
        if not isinstance(choix, list) or len(choix) > 4 or any(not isinstance(c, str) or not c.strip()
                                                                or len(c) > 40 for c in choix):
            raise ValueError("choix : 4 au plus, 40 caractères chacun")
        kind = a.get("type") or ("choix" if choix else "libre")
        if kind not in TYPES:
            raise ValueError(f"type inconnu : {kind}")
        squad_id, n = self.session()
        q = {"id": self.box.next_id(), "chantier": self.args.sujet, "escouade": squad_id, "session": n, "type": kind,
             "bloquante": bloquante, "texte": texte.strip(), "choix": choix, "conseille": choix[0] if choix else None,
             "commande": str(a.get("commande") or "")[:300] or None, "cree": time.time(), "echeance": None,
             "statut": "ouverte", "envoi": None, "rappel": None}
        return q

    def signaler(self, a: dict) -> str:
        q = self._new_question(a, False)
        self.box.add(q)
        return f"{q['id']} posée, continue ; la réponse arrivera au fil du travail."

    def demander(self, a: dict, token) -> str:
        squad_id, n = self.session()
        mine = [q for q in self.box.questions() if q.get("escouade") == squad_id and q.get("session") == n]
        if any(q.get("bloquante") and q.get("statut") == "ouverte" for q in mine):
            raise ValueError("une question bloquante est déjà ouverte : une seule à la fois")
        if a.get("reprendre"):
            q = self.box.question(str(a["reprendre"]))
            if q is None or q.get("escouade") != squad_id or q.get("statut") != "ouverte":
                raise ValueError(f"question inconnue ou close : {a['reprendre']}")
        else:
            if sum(1 for q in mine if q.get("bloquante")) >= MAX_BLOCKING:
                raise ValueError(f"{MAX_BLOCKING} questions bloquantes déjà posées dans cette session : "
                                 "décide la solution la plus prudente et réversible, notée à confirmer")
            q = self._new_question(a, True)
        wait = self.args.attente_defaut
        if isinstance(a.get("attente_max_min"), (int, float)) and a["attente_max_min"] > 0:
            wait = a["attente_max_min"] * 60
        wait = min(wait, self.args.attente_max)
        q["echeance"] = self.clock() + wait
        if a.get("reprendre"):
            q = self.box.update(q["id"], bloquante=True, echeance=q["echeance"])
        else:
            self.box.add(q)
        return self.wait(q, token)

    def wait(self, q: dict, token) -> str:
        """Wait for the answer; the heartbeat file tells the watchdog that this is an attended wait, not idleness."""
        try:
            return self._wait(q, token)
        finally:
            (self.squad / BEAT_FILE).unlink(missing_ok=True)

    def _wait(self, q: dict, token) -> str:
        qid, start, last_progress, warned = q["id"], self.clock(), self.clock(), False
        last_beat = None
        while self.clock() < q["echeance"]:
            if last_beat is None or self.clock() - last_beat >= self.args.battement:
                last_beat = self.clock()
                self.beat(last_beat)
            answer = self.box.answer(qid)
            if answer is not None:
                if seal_ok(load_key(self.key_path), qid, answer, q["cree"]):
                    self.box.update(qid, statut="repondue", reponse_lue=self.clock())
                    if answer.get("type") == "decide":
                        text = f"{qid} : l'utilisateur te laisse décider. Décide toi-même, note à confirmer."
                    else:
                        text = f"{qid} : réponse de l'utilisateur ({answer.get('type')}) : {answer['valeur']}"
                    return "\n".join(filter(None, [text, self.mail()]))
                if not warned:
                    sys.stderr.write(f"mcp_ultron: answer to {qid} ignored (no valid seal)\n")
                    warned = True
            if (self.squad / "drapeau-passation").exists():
                return (f"{qid} : limite atteinte, fais la passation ; la question reste ouverte dans À toi.")
            if self.drain_closed():
                return f"{qid} : session arrêtée."
            if token is not None and self.clock() - last_progress >= self.args.progress:
                last_progress = self.clock()
                self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                           "params": {"progressToken": token, "progress": int(last_progress - start),
                                      "message": f"{qid} : en attente de l'utilisateur"}})
            self.sleep(self.args.tick)
        self.box.update(qid, statut="decidee_seule")
        return f"{qid} : pas de réponse. Décision prudente et réversible, notée « à confirmer » dans la fiche."

    def beat(self, ts: float) -> None:
        path = self.squad / BEAT_FILE
        try:
            path.touch()
            os.utime(path, (ts, ts))
        except OSError:
            pass  # a missing heartbeat only costs the watchdog's idleness filet, never the wait

    def drain_closed(self) -> bool:
        """Stdin closed while waiting: the session is gone (other messages wait for the main loop)."""
        pending = []
        while True:
            try:
                msg = self.inbox.get_nowait()
            except queue.Empty:
                break
            if msg is None:
                self.closed = True
            else:
                pending.append(msg)
        for msg in pending:
            self.inbox.put(msg)
        if self.closed:
            self.inbox.put(None)
        return self.closed

    # -- dispatch --
    def handle(self, msg: dict) -> None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return  # notifications
        if method == "initialize":
            version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
            result = {"protocolVersion": version, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "ultron", "version": "1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.tools()}
        elif method == "tools/call":
            params = msg.get("params") or {}
            args = params.get("arguments") or {}
            token = (params.get("_meta") or {}).get("progressToken")
            try:
                if params.get("name") == "demander":
                    text = self.demander(args, token)
                elif params.get("name") == "signaler":
                    text = self.signaler(args)
                else:
                    raise ValueError(f"outil inconnu : {params.get('name')}")
                result = {"content": [{"type": "text", "text": text}]}
            except (ValueError, OSError) as exc:
                result = {"content": [{"type": "text", "text": f"Refusé : {exc}"}], "isError": True}
        else:
            self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})
            return
        self.send({"jsonrpc": "2.0", "id": mid, "result": result})

    def run(self) -> int:
        threading.Thread(target=self.reader, daemon=True).start()
        while True:
            msg = self.inbox.get()
            if msg is None:
                return 0
            self.handle(msg)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="mcp_ultron")
    p.add_argument("--boite", required=True)
    p.add_argument("--escouade-dir", required=True)
    p.add_argument("--sujet", required=True)
    p.add_argument("--cle")  # tests only; the real launch uses the key path from pwd
    p.add_argument("--attente-defaut", type=float, default=20 * 60)
    p.add_argument("--attente-max", type=float, default=60 * 60)  # todo #118: 60 min (was 25)
    p.add_argument("--tick", type=float, default=1.0)
    p.add_argument("--battement", type=float, default=30.0)
    p.add_argument("--progress", type=float, default=30.0)
    return Server(p.parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
