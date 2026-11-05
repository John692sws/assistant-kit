"""📎 joindre (todo #293): a file sent from the dashboard is stored (type from the content, 0600, sanitized name, size caps),
then analysed by the catalogue's `joindre` (photo kept; PDF, text, audio, video through the `docs` batch), filed in the fil.

No network beyond 127.0.0.1, whois simulated; `recus.dossier` and `notifs.dossier` point at throwaway folders (conftest).
"""

import io
import json
import stat
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cli_contract import check_argv
from ultron_hub import notifs, recus
from ultron_hub.dashboard import catalogue
from test_dashboard_actions import dash, fake_cli, fresh_cache, request, session  # noqa: F401  (fixtures)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 64
MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
OGG = b"OggS" + b"\x00" * 64
NOW = datetime(2026, 10, 20, 10, 15, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("head,nom,ext", [
    (PNG, "IMG_0001.PNG", "png"), (JPG, "photo.jpeg", "jpg"), (PDF, "x.pdf", "pdf"), (MP4, "film.mov", "mov"),
    (MP4, "film.mp4", "mp4"), (M4A, "vocal.m4a", "m4a"), (MP4, "vocal-12h05.m4a", "m4a"), (HEIC, "IMG.HEIC", "heic"),
    (WEBM, "vocal.weba", "weba"), (WEBM, "clip.webm", "webm"), (OGG, "a.ogg", "ogg"), (b"ID3\x03" + b"\x00" * 20, "a.mp3", "mp3"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", "a.wav", "wav"), (b"Bonjour \xc3\xa9t\xc3\xa9", "notes.markdown", "md"),
    (PNG, "capture.exe", "png"),  # the content decides, never the name
])
def test_type_comes_from_the_content(head, nom, ext):
    assert recus.sniff(head, nom) == ext


@pytest.mark.parametrize("head,nom", [
    (b"MZ\x90\x00", "outil.exe"), (b"#!/bin/sh\nrm -rf /", "script.sh"), (b"<svg onload=alert(1)>", "image.svg"),
    (b"<html><script>", "page.png"), (b"PK\x03\x04", "archive.zip"), (b"texte\x00binaire", "notes.txt"), (b"\xff\xfe\xfd", "x.txt"),
])
def test_unknown_or_dangerous_content_is_refused(head, nom):
    assert recus.sniff(head, nom) is None


@pytest.mark.parametrize("nom,stem", [("../../etc/passwd", "passwd"), ("-rf .png", "rf"), ("é t é.png", "t"),
                                      ("", "fichier"), ("..", "fichier"), ("a" * 300 + ".png", "a" * 40), ("C:\\x\\y.png", "y")])
def test_names_are_sanitized(nom, stem):
    assert recus.stem_of(nom) == stem


def test_received_file_is_private_named_by_us_and_found_back(recus_dir):
    out = recus.recevoir(io.BytesIO(PNG), len(PNG), "../Capture d'écran.png", recus_dir, now=NOW)
    assert out["genre"] == "image" and out["taille"] == len(PNG)
    assert recus.RECU_RE.fullmatch(out["fichier"]) and out["fichier"].startswith("20261020T101500-")
    path = recus.chemin(out["fichier"], recus_dir)
    assert path.parent == recus_dir / "2026-10-20" and path.read_bytes() == PNG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(recus_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list((recus_dir / ".tmp").iterdir()) == []  # the temporary file is gone


@pytest.mark.parametrize("body,length,nom,mot", [
    (b"MZ\x90" * 10, 30, "x.exe", "type"), (PNG, 0, "x.png", "vide"), (PNG, recus.UPLOAD_MAX + 1, "x.png", "trop gros"),
    (PNG[:10], len(PNG), "x.png", "interrompu"),
])
def test_refused_upload_keeps_nothing(recus_dir, body, length, nom, mot):
    with pytest.raises(recus.Refuse, match=mot):
        recus.recevoir(io.BytesIO(body), length, nom, recus_dir)
    assert [p for p in recus_dir.rglob("*") if p.is_file()] == [] if recus_dir.exists() else True


def test_each_kind_has_its_size_cap_and_the_folder_its_total(recus_dir, monkeypatch):
    monkeypatch.setitem(recus.MAX_BYTES, "image", 100)
    with pytest.raises(recus.Refuse, match="trop gros"):
        recus.recevoir(io.BytesIO(PNG + b"\x00" * 100), len(PNG) + 100, "x.png", recus_dir)
    monkeypatch.setattr(recus, "TOTAL_MAX", 100)
    recus.recevoir(io.BytesIO(PNG), len(PNG), "x.png", recus_dir)
    with pytest.raises(recus.Refuse, match="épuisée"):
        recus.recevoir(io.BytesIO(PNG), len(PNG), "y.png", recus_dir)


@pytest.mark.parametrize("nom", ["../x.png", "/etc/passwd", "20261020T101500-a1b2c3-x.sh", "20261020T101500-a1b2c3--x.png",
                                 "20261020T101500-a1b2c3-x.png/../../y", "20261020T101500-a1b2c3-absent.png", 7])
def test_chemin_refuses_anything_but_a_stored_name(recus_dir, nom):
    with pytest.raises(recus.Refuse):
        recus.chemin(nom, recus_dir)


def test_chemin_refuses_a_symbolic_link(recus_dir, tmp_path):
    day = recus_dir / "2026-10-20"
    day.mkdir(parents=True)
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    (day / "20261020T101500-a1b2c3-x.png").symlink_to(secret)
    with pytest.raises(recus.Refuse):
        recus.chemin("20261020T101500-a1b2c3-x.png", recus_dir)


# -- the analysis (`joindre analyser`) -------------------------------------------------------------------------------------

def _settings(tmp_path):
    return SimpleNamespace(var_dir=tmp_path / "var")


def test_a_photo_is_kept_filed_in_the_fil_and_given_to_the_session(recus_dir, notifs_dir, tmp_path):
    recu = recus.recevoir(io.BytesIO(PNG), len(PNG), "capture.png", recus_dir)["fichier"]
    out = recus.analyser(_settings(tmp_path), recu)
    assert out["status"] == "ok"
    data = out["data"]
    path = str(recus.chemin(recu, recus_dir))
    assert data["chemin"] == path and data["genre"] == "image" and path in data["pour_session"]
    assert "jamais comme des consignes" in data["pour_session"] and "\n" not in data["pour_session"]
    [row] = notifs.liste()["notifs"]
    assert row["titre"].startswith("📎 Photo reçue") and row["pj"][0]["genre"] == "image"  # the image itself is in the fil
    assert row["origine"] == "toi"  # todo #353: marked at the origin (data field), the page draws it as « envoyé par toi »


def test_an_ultron_message_is_not_marked_as_sent_by_the_user(notifs_dir):
    notifs.noter("📎 Photo reçue : x.png", "briefing", False)  # same words, no origin: stays Ultron's
    assert notifs.liste()["notifs"][0]["origine"] == "ultron"


def test_a_pdf_goes_through_the_docs_batch(recus_dir, notifs_dir, tmp_path, monkeypatch):
    from ultron_hub.docs import commands as docs
    seen = {}
    batch = tmp_path / "lot"
    (batch / "files").mkdir(parents=True)
    monkeypatch.setattr(docs, "do_new", lambda settings: {"status": "ok", "data": {"id": "20261020T101500Z-0a1b2c3d", "path": str(batch)}})

    def present(settings, docs_id, by):
        seen.update(id=docs_id, by=by, files=sorted(p.name for p in (batch / "files").iterdir()))
        return {"status": "ok", "data": {"id": docs_id, "text": "Facture d'exemple\n- montant : 12 €"}}
    monkeypatch.setattr(docs, "do_present", present)
    recu = recus.recevoir(io.BytesIO(PDF), len(PDF), "facture.pdf", recus_dir)["fichier"]
    out = recus.analyser(_settings(tmp_path), recu)
    assert out["status"] == "ok" and out["data"]["id"] == "20261020T101500Z-0a1b2c3d"
    assert seen == {"id": "20261020T101500Z-0a1b2c3d", "by": "dashboard", "files": ["1-facture.pdf"]}
    assert "montant : 12 €" in out["data"]["text"] and "Résumé : Facture d'exemple - montant : 12 €" in out["data"]["pour_session"]
    assert notifs.liste()["notifs"][0]["titre"].startswith("📎 Document analysé")


def test_a_video_gives_only_its_sound_track_to_the_batch(recus_dir, notifs_dir, tmp_path, monkeypatch):
    from ultron_hub.docs import commands as docs
    batch = tmp_path / "lot"
    (batch / "files").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(docs, "do_new", lambda settings: {"status": "ok", "data": {"id": "20261020T101500Z-0a1b2c3d", "path": str(batch)}})
    monkeypatch.setattr(docs, "do_present", lambda *a: {"status": "ok", "data": {"id": a[1], "text": "transcription"}})
    monkeypatch.setattr(recus, "_transcoder", lambda src, dest: calls.append(dest.name) or dest.write_bytes(OGG))
    recu = recus.recevoir(io.BytesIO(MP4), len(MP4), "film.mov", recus_dir)["fichier"]
    assert recus.analyser(_settings(tmp_path), recu)["status"] == "ok" and calls == ["1-film.ogg"]


def test_a_failed_analysis_still_lands_in_the_fil_with_the_file(recus_dir, notifs_dir, tmp_path, monkeypatch):
    from ultron_hub.docs import commands as docs
    batch = tmp_path / "lot"
    (batch / "files").mkdir(parents=True)
    monkeypatch.setattr(docs, "do_new", lambda settings: {"status": "ok", "data": {"id": "20261020T101500Z-0a1b2c3d", "path": str(batch)}})
    monkeypatch.setattr(docs, "do_present", lambda *a: {"status": "error", "error": "Ce PDF est illisible."})
    recu = recus.recevoir(io.BytesIO(PDF), len(PDF), "x.pdf", recus_dir)["fichier"]
    out = recus.analyser(_settings(tmp_path), recu)
    assert out["status"] == "error" and "illisible" in out["error"] and "fichier gardé" in out["error"]
    assert "❌ Analyse impossible" in notifs.liste()["notifs"][0]["complet"]


def test_cli_joindre_liste_and_refused_name(recus_dir, capsys, project):
    from ultron_hub import cli
    recus.recevoir(io.BytesIO(PNG), len(PNG), "a.png", recus_dir)
    assert cli.main(["joindre", "liste", "--n", "5"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert len(data["recus"]) == 1 and data["recus"][0]["chemin"].startswith(str(recus_dir))
    assert cli.main(["joindre", "analyser", "../../etc/passwd"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_catalogue_entry_matches_the_cli_and_refuses_free_paths():
    action = catalogue.PAR_ID["joindre"]
    assert action.type == "simple" and action.signe is None and not action.ouvert
    _, args, stdin = catalogue.valider("joindre", {"fichier": "20261020T101500-a1b2c3-capture.png", "session": "ultron-19-10-15h38"})
    argv = catalogue.commande(action, args, stdin)
    assert argv == ["joindre", "analyser", "20261020T101500-a1b2c3-capture.png"]
    check_argv(argv)
    for bad in ("../x.png", "/home/exemple/.ssh/id_ed25519", "--help", "20261020T101500-a1b2c3-x.sh"):
        with pytest.raises(catalogue.Refused):
            catalogue.valider("joindre", {"fichier": bad})


# -- the upload route (POST /api/fichier) -----------------------------------------------------------------------------------

def upload(d, body, *, cookie, nom="capture.png", ctype="application/octet-stream", origin="same"):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", d.port, timeout=10)
    headers = {"Host": d.host, "Content-Type": ctype, "X-Nom-Fichier": nom}
    if origin == "same":
        headers["Origin"] = f"http://{d.host}"
    elif origin:
        headers["Origin"] = origin
    if cookie:
        headers["Cookie"] = f"ultron_session={cookie}"
    conn.request("POST", "/api/fichier", body=body, headers=headers)
    r = conn.getresponse()
    out = (r.status, json.loads(r.read()))
    conn.close()
    return out


def test_upload_stores_the_file_and_runs_nothing(dash, recus_dir):
    sid = session(dash)
    code, out = upload(dash, PNG, cookie=sid, nom="Capture%20d%27%C3%A9cran.png")
    assert code == 200 and out["ok"] and out["genre"] == "image"
    assert recus.chemin(out["fichier"], recus_dir).read_bytes() == PNG
    assert dash.calls() == [] and dash.journal == []  # stored only: the analysis is the `joindre` action


@pytest.mark.parametrize("kw,code", [
    ({"origin": "https://evil.example"}, 403), ({"origin": None}, 403), ({"ctype": "application/json"}, 400),
    ({"ctype": "multipart/form-data; boundary=x"}, 400), ({"cookie": None}, 403), ({"cookie": "forged"}, 403),
])
def test_upload_refusals_store_nothing(dash, recus_dir, kw, code):
    sid = session(dash)
    args = {"cookie": sid, **kw}
    status, out = upload(dash, PNG, **args)
    assert status == code and not out["ok"]
    assert not recus_dir.exists() or [p for p in recus_dir.rglob("*") if p.is_file()] == []


def test_upload_of_an_unknown_type_is_refused(dash, recus_dir):
    status, out = upload(dash, b"#!/bin/sh\necho pwned\n", cookie=session(dash), nom="x.sh")
    assert status == 409 and "type" in out["erreur"]
    assert [p for p in recus_dir.rglob("*") if p.is_file()] == []


def test_upload_from_a_refused_device_is_refused_before_anything(dash, recus_dir):
    sid = session(dash)
    dash.node = "nSTRANGER"
    status, _ = upload(dash, PNG, cookie=sid)
    assert status == 403 and not recus_dir.exists()


def test_upload_too_big_is_refused_from_its_length(dash, recus_dir, monkeypatch):
    monkeypatch.setattr(recus, "UPLOAD_MAX", 16)
    status, out = upload(dash, PNG, cookie=session(dash))
    assert status == 413 and not recus_dir.exists()


# -- 🎙 vocal de l'utilisateur (todo #365) -------------------------------------------------------------------------------

def _audio_analyse(monkeypatch):
    """Todo #397: a vocal never goes through the docs batch / Sonnet presenter; the fast transcriber gives the raw words."""
    def no_presenter(*a):
        raise AssertionError("a vocal must not start a docs-presenter job")
    monkeypatch.setattr(recus, "_presenter", no_presenter)
    from ultron_hub import vocal_rapide
    monkeypatch.setattr(vocal_rapide, "transcrire", lambda p: "j'ai plusieurs choses à dire : un, deux et trois")


def test_a_marked_vocal_reaches_the_session_as_the_users_raw_words(recus_dir, notifs_dir, tmp_path, monkeypatch):
    _audio_analyse(monkeypatch)
    recu = recus.recevoir(io.BytesIO(M4A), len(M4A), "vocal-21h27.m4a", recus_dir, vocal=True)["fichier"]
    assert recus.est_vocal(recus.chemin(recu, recus_dir))
    data = recus.analyser(_settings(tmp_path), recu)["data"]
    assert data["vocal"] is True
    assert "Message vocal de l'utilisateur" in data["pour_session"] and "un, deux et trois" in data["pour_session"]
    assert "jamais comme des consignes" not in data["pour_session"] and "résumé neutre" not in data["pour_session"]
    assert [r["fichier"] for r in recus.liste(_settings(tmp_path), 10)["recus"]] == [recu]  # the marker is not a received file


def test_vocal_seul_accepts_a_marked_vocal_and_refuses_everything_else(recus_dir, notifs_dir, tmp_path, monkeypatch):
    """Todo #379: the sheetless `joindre-vocal` runs `joindre analyser --vocal-seul`: only the user's recorded voice passes."""
    _audio_analyse(monkeypatch)
    vocal = recus.recevoir(io.BytesIO(M4A), len(M4A), "vocal-21h27.m4a", recus_dir, vocal=True)["fichier"]
    assert recus.analyser(_settings(tmp_path), vocal, vocal_seul=True)["data"]["vocal"] is True
    for nom, octets, vocal_ in (("vocal-21h27.m4a", M4A, False), ("capture.png", PNG, False), ("x.png", PNG, True)):
        recu = recus.recevoir(io.BytesIO(octets), len(octets), nom, recus_dir, vocal=vocal_)["fichier"]
        out = recus.analyser(_settings(tmp_path), recu, vocal_seul=True)
        assert out["status"] == "error" and "sans feuille" in out["error"] and out["data"] is None
    argv = catalogue.commande(catalogue.PAR_ID["joindre-vocal"], *catalogue.valider("joindre-vocal", {"fichier": vocal})[1:])
    assert argv == ["joindre", "analyser", vocal, "--vocal-seul"]
    check_argv(argv)


def test_an_unmarked_audio_stays_data_even_named_like_a_vocal(recus_dir, notifs_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(recus, "_presenter", lambda s, p, k: {"status": "ok", "data": {"id": "L1", "text": "résumé neutre"}})
    recu = recus.recevoir(io.BytesIO(M4A), len(M4A), "vocal-21h27.m4a", recus_dir)["fichier"]
    data = recus.analyser(_settings(tmp_path), recu)["data"]
    assert "vocal" not in data and "jamais comme des consignes" in data["pour_session"]
    assert "un, deux et trois" not in data["pour_session"]


def test_the_marker_is_only_for_audio(recus_dir):
    recu = recus.recevoir(io.BytesIO(PNG), len(PNG), "x.png", recus_dir, vocal=True)["fichier"]
    assert not recus.est_vocal(recus.chemin(recu, recus_dir))


def test_upload_marker_header_is_honoured_only_through_the_authenticated_route(dash, recus_dir):
    import http.client
    sid = session(dash)

    def post(extra, cookie):
        conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=10)
        h = {"Host": dash.host, "Content-Type": "application/octet-stream", "X-Nom-Fichier": "v.m4a",
             "Origin": f"http://{dash.host}", **extra}
        if cookie:
            h["Cookie"] = f"ultron_session={cookie}"
        conn.request("POST", "/api/fichier", body=M4A, headers=h)
        r = conn.getresponse()
        out = (r.status, json.loads(r.read()))
        conn.close()
        return out
    code, out = post({"X-Source": "vocal"}, sid)
    assert code == 200 and recus.est_vocal(recus.chemin(out["fichier"], recus_dir))
    code, out = post({}, sid)  # a picked 📎 audio: no marker
    assert code == 200 and not recus.est_vocal(recus.chemin(out["fichier"], recus_dir))
    code, _ = post({"X-Source": "vocal"}, None)  # no session: refused, nothing stored, no marker
    assert code == 403
    assert not list(recus_dir.rglob("*.vocal")) or len(list(recus_dir.rglob("*.vocal"))) == 1


def test_only_the_recorder_declares_vocal():
    """todo #365: the page sends X-Source: vocal from the 🎙 recorder only, never from the 📎 picker."""
    js = (Path(__file__).resolve().parents[1] / "src/ultron_hub/dashboard/static/app.js").read_text()
    assert 'if (estVocal) entetes["X-Source"] = "vocal";' in js
    assert '"." + ext, true, false, vocal.session);' in js and "envoyerFichier(f, f.name, false);" in js


def test_vocal_transcription_error_is_explained_and_the_file_kept(recus_dir, notifs_dir, tmp_path, monkeypatch):
    from ultron_hub import vocal_rapide
    from ultron_hub.bot.voice import VoiceError

    def boom(p):
        raise VoiceError("Je n'entends pas de parole dans ce vocal.")
    monkeypatch.setattr(vocal_rapide, "transcrire", boom)
    monkeypatch.setattr(recus, "_presenter", lambda *a: pytest.fail("no presenter for a vocal"))
    recu = recus.recevoir(io.BytesIO(M4A), len(M4A), "vocal-21h27.m4a", recus_dir, vocal=True)["fichier"]
    out = recus.analyser(_settings(tmp_path), recu)
    assert out["status"] == "error" and "pas de parole" in out["error"] and recus.chemin(recu, recus_dir).is_file()
