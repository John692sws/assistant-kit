"""MCP server `ultron` of a squad (todo #106): protocol, waiting, seals, caps. Short timings, no real key."""

import hashlib
import hmac
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from ultron_hub.escouade import hooks, mcp_ultron
from ultron_hub.todo import closing

SERVER = Path(mcp_ultron.__file__)
KEY = b"k" * 40


class Client:
    def __init__(self, tmp: Path, attente: float = 3.0):
        self.squad = tmp / "squad"
        self.squad.mkdir(exist_ok=True)
        (self.squad / "etat.json").write_text(json.dumps({"id": "fba-1", "numero": 1}))
        self.key = tmp / "close.key"
        self.key.write_bytes(KEY)
        self.key.chmod(0o600)
        self.box = tmp / "boites" / "fba"
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-S", str(SERVER), "--boite", str(self.box), "--escouade-dir", str(self.squad),
             "--sujet", "fba", "--cle", str(self.key), "--attente-defaut", str(attente), "--attente-max", "5",
             "--tick", "0.05", "--progress", "0.2"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.n = 0

    def call(self, method, params=None, read=True):
        self.n += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        return self.read_until(self.n) if read else None

    def read_until(self, mid):
        self.notes = []
        while True:
            msg = json.loads(self.proc.stdout.readline())
            if msg.get("id") == mid:
                return msg
            self.notes.append(msg)

    def tool(self, name, args, token=None):
        params = {"name": name, "arguments": args}
        if token:
            params["_meta"] = {"progressToken": token}
        return self.call("tools/call", params)["result"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(5)


def answer(box: Path, qid: str, valeur: str, kind="choix", good=True):
    ts = int(time.time())
    mac = hmac.new(KEY if good else b"x" * 40, mcp_ultron.seal_message(qid, valeur, ts), hashlib.sha256).hexdigest()
    mcp_ultron._write_json(box / "reponses" / f"{qid}.json",
                           {"id": qid, "type": kind, "valeur": valeur, "sceau": f"{ts}.{mac}"})


def later(delay, fn, *args, **kw):
    t = threading.Timer(delay, fn, args, kw)
    t.start()
    return t


def test_protocol_and_signaler(tmp_path):
    c = Client(tmp_path)
    init = c.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert init["result"]["serverInfo"]["name"] == "ultron"
    names = [t["name"] for t in c.call("tools/list")["result"]["tools"]]
    assert names == ["demander", "signaler"]  # nothing to answer
    r = c.tool("signaler", {"question": "Quel compte ?", "choix": ["pro", "perso"]})
    assert r["content"][0]["text"].startswith("Q1 posée")
    q = json.loads((c.box / "questions" / "Q1.json").read_text())
    assert q["statut"] == "ouverte" and not q["bloquante"] and q["escouade"] == "fba-1"
    assert (c.box.parent / ".a-envoyer").exists()
    assert c.tool("signaler", {"question": "Et ensuite ?"})["content"][0]["text"].startswith("Q2")
    c.close()


def test_demander_waits_for_sealed_answer_with_progress(tmp_path):
    c = Client(tmp_path)
    t = later(0.5, answer, c.box, "Q1", "pro")
    r = c.tool("demander", {"question": "Quel compte ?", "choix": ["pro", "perso"]}, token="tok")
    t.join()
    assert "réponse de l'utilisateur (choix) : pro" in r["content"][0]["text"]
    assert any(n.get("method") == "notifications/progress" and n["params"]["progressToken"] == "tok" for n in c.notes)
    assert json.loads((c.box / "questions" / "Q1.json").read_text())["statut"] == "repondue"
    c.close()


def test_unsealed_answer_is_ignored_then_deadline(tmp_path):
    c = Client(tmp_path, attente=1.0)
    later(0.1, answer, c.box, "Q1", "pro", good=False).join()
    r = c.tool("demander", {"question": "Quel compte ?"})
    assert "pas de réponse" in r["content"][0]["text"]
    assert json.loads((c.box / "questions" / "Q1.json").read_text())["statut"] == "decidee_seule"
    c.close()


def test_skip_and_handover_flag(tmp_path):
    c = Client(tmp_path)
    later(0.3, answer, c.box, "Q1", "decide", kind="decide")
    assert "Décide toi-même" in c.tool("demander", {"question": "A ?"})["content"][0]["text"]
    later(0.3, (c.squad / "drapeau-passation").touch)
    r = c.tool("demander", {"question": "B ?"})
    assert "passation" in r["content"][0]["text"]
    # Q2 stays open: one blocking question at a time.
    r = c.tool("demander", {"question": "C ?"})
    assert r["isError"] and "une seule à la fois" in r["content"][0]["text"]
    c.close()


def test_three_blocking_per_session_and_reprendre(tmp_path):
    c = Client(tmp_path, attente=0.2)
    for _ in range(3):
        assert "pas de réponse" in c.tool("demander", {"question": "?"})["content"][0]["text"]
    assert c.tool("demander", {"question": "4e ?"})["isError"]
    c.close()
    (tmp_path / "squad" / "etat.json").write_text(json.dumps({"id": "fba-1", "numero": 2}))
    c2 = Client(tmp_path, attente=2.0)
    (c2.squad / "etat.json").write_text(json.dumps({"id": "fba-1", "numero": 2}))
    qid = c2.tool("signaler", {"question": "Plus tard ?"})["content"][0]["text"].split()[0]
    assert qid == "Q4"  # counter shared by the box root
    later(0.3, answer, c2.box, qid, "oui", kind="texte")
    assert ": oui" in c2.tool("demander", {"reprendre": qid})["content"][0]["text"]
    c2.close()


def test_seal_matches_closing_signature():
    target = f"Q7:{mcp_ultron.value_hash('pro')}"
    token = closing.sign(KEY, "reponse", target, 1000)
    assert mcp_ultron.seal_ok(KEY, "Q7", {"valeur": "pro", "sceau": token}, 1000)
    assert not mcp_ultron.seal_ok(KEY, "Q7", {"valeur": "perso", "sceau": token}, 1000)
    assert not mcp_ultron.seal_ok(KEY, "Q7", {"valeur": "pro", "sceau": token}, 2000)  # older than the question


def test_server_imports_no_ultron_hub():
    import ast
    tree = ast.parse(SERVER.read_text())
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert names and not any(m.startswith("ultron_hub") for m in names)


def test_actions_and_pretooluse():
    assert "reponse" in closing.ACTIONS
    for sub in ("repondre", "remarque", "question-envoyee"):
        assert hooks.check_command(f"ultron-hub escouade {sub} Q1 --type texte")
