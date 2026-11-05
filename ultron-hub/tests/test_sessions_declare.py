"""Declared state of a session (todo #431): `session etat` writes it, `session liste` shows it as `declare`."""

import json
import stat
import time
from argparse import Namespace

import pytest

from ultron_hub.sessions import commands, declare, journal

NAME = "ultron-demo-26-10-10h00"


@pytest.fixture(autouse=True)
def sessions_etat_dir(tmp_path, monkeypatch):
    """Never write the real var/sessions-etat: the declared states land in a throwaway folder (kept out of conftest.py,
    a guard file that would block the automatic merge)."""
    path = tmp_path / "sessions-etat"
    monkeypatch.setattr(declare, "dossier", lambda settings=None: path)
    return path


def etat(sessions_etat_dir, **kw):
    args = {"session": NAME, "efface": False, **{f: None for f in declare.FIELDS}, **kw}
    return commands.handle(Namespace(session_command="etat", **args), None)


def test_write_permissions_and_partial_update(sessions_etat_dir):
    out = etat(sessions_etat_dir, maintenant="tests", ensuite="fusion")
    assert out["status"] == "ok" and out["data"]["maintenant"] == "tests" and out["data"]["at"]
    path = sessions_etat_dir / f"{NAME}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(sessions_etat_dir.stat().st_mode) == 0o700
    out = etat(sessions_etat_dir, attend="ta réponse")
    assert (out["data"]["maintenant"], out["data"]["ensuite"], out["data"]["attend"]) == ("tests", "fusion", "ta réponse")
    assert json.loads(path.read_text())["bloque"] is None


def test_read_without_fields_and_clear(sessions_etat_dir):
    assert etat(sessions_etat_dir)["data"]["maintenant"] is None
    assert not sessions_etat_dir.exists()  # reading writes nothing
    etat(sessions_etat_dir, maintenant="a")
    assert etat(sessions_etat_dir)["data"]["maintenant"] == "a"
    out = etat(sessions_etat_dir, efface=True)
    assert all(out["data"][f] is None for f in declare.FIELDS)


def test_text_is_one_line_and_cut(sessions_etat_dir):
    out = etat(sessions_etat_dir, maintenant="a\nb\x00" + "x" * 300)
    assert "\n" not in out["data"]["maintenant"] and len(out["data"]["maintenant"]) == declare.TEXT_MAX


def test_invalid_name_refused(sessions_etat_dir):
    out = etat(sessions_etat_dir, session="../evil", maintenant="x")
    assert out["status"] == "error" and "refusé" in out["error"]
    assert not sessions_etat_dir.exists()


def test_liste_with_and_without_state_and_stale(sessions_etat_dir, monkeypatch):
    now = int(time.time())
    sessions = [{"nom": NAME, "cree": now - 600, "ouverte": True, "chantier": "demo"},
                {"nom": "ultron-main-26-10-11h32", "cree": now - 600, "ouverte": False, "chantier": None},
                {"nom": "ultron-old-26-10-09h00", "cree": now + 3600, "ouverte": False, "chantier": "old"}]
    monkeypatch.setattr(journal, "list_sessions", lambda: sessions)
    monkeypatch.setattr(commands.etat, "digest", lambda nom: {})
    etat(sessions_etat_dir, maintenant="en cours")
    etat(sessions_etat_dir, session="ultron-old-26-10-09h00", maintenant="périmé")
    (sessions_etat_dir / "ultron-main-26-10-11h32.json").write_text("{pas du json")
    out = commands.handle(Namespace(session_command="liste"), None)["data"]["sessions"]
    by = {s["nom"]: s["declare"] for s in out}
    assert by[NAME]["maintenant"] == "en cours" and set(by[NAME]) == {*declare.FIELDS, "at"}
    assert by["ultron-main-26-10-11h32"] is None and by["ultron-old-26-10-09h00"] is None


def test_outside_tmux_without_session_is_refused(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    with pytest.raises(journal.SessionError):
        declare.current_session()
