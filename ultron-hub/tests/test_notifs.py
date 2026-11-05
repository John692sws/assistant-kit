"""Todo #276: history of the notifications sent (never the real var/notifs: the `notifs_dir` fixture)."""

import json
import stat
from pathlib import Path

import pytest

from cli_contract import check_argv
from ultron_hub import cli, envoi, notifs
from ultron_hub.dashboard import catalogue


def at(n):
    return f"2026-01-01T10:{n // 60:02d}:{n % 60:02d}+00:00"


def test_noter_then_liste_newest_first_unread(notifs_dir):
    assert notifs.noter("Fusion prête\nLa todo #1 attend", "travail", True, now=at(1))
    assert notifs.noter("Question\ndétail", "briefing", False, now=at(2))
    data = notifs.liste()
    assert [n["titre"] for n in data["notifs"]] == ["Question", "Fusion prête"]
    assert data["non_lues"] == 2 and not any(n["lue"] for n in data["notifs"])
    assert data["notifs"][1] == {"at": at(1), "titre": "Fusion prête", "corps": "La todo #1 attend", "complet": "La todo #1 attend", "details": [],
                                  "pj": [], "onglet": "travail", "lue": False, "etiquettes": [], "genre": "feu-vert", "genre_fil": "fait", "origine": "ultron", "resume": "La todo #1 attend", "todo": "1",
                                   "cible": {"genre": "todo", "ref": "1", "libelle": "Voir la todo #1"}}
    row = json.loads((notifs_dir / notifs.HISTORY_FILE).read_text().splitlines()[1])
    assert row["telegram"] is False and row["onglet"] == "briefing"


def test_files_are_private(notifs_dir):
    notifs.noter("a\nb", "briefing", True)
    assert stat.S_IMODE(notifs_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((notifs_dir / notifs.HISTORY_FILE).stat().st_mode) == 0o600
    notifs.vu()
    assert stat.S_IMODE((notifs_dir / notifs.SEEN_FILE).stat().st_mode) == 0o600


def test_text_is_cleaned(notifs_dir):
    notifs.noter("Titre\nvoir /home/exemple/.ssh/id_rsa\nMEDIA:/tmp/x.png", "briefing", True)
    row = notifs.liste()["notifs"][0]
    assert "MEDIA" not in row["corps"] and "x.png" not in row["corps"]


def test_vu_marks_read_and_new_ones_stay_unread(notifs_dir):
    notifs.noter("a\nb", "briefing", True, now=at(1))
    notifs.noter("c\nd", "briefing", True, now=at(2))
    assert notifs.vu() == {"vu_jusqu_a": at(2)}
    data = notifs.liste()
    assert data["non_lues"] == 0 and all(n["lue"] for n in data["notifs"])
    notifs.noter("e\nf", "travail", True, now=at(3))
    data = notifs.liste()
    assert data["non_lues"] == 1 and [n["lue"] for n in data["notifs"]] == [False, True, True]


def test_liste_limits_n_but_counts_all_unread(notifs_dir):
    for i in range(5):
        notifs.noter(f"t{i}\nb", "briefing", True, now=at(i))
    data = notifs.liste(2)
    assert [n["titre"] for n in data["notifs"]] == ["t4", "t3"] and data["non_lues"] == 5


def test_unreadable_lines_are_ignored(notifs_dir):
    notifs.noter("ok\nb", "briefing", True, now=at(1))
    with open(notifs_dir / notifs.HISTORY_FILE, "a") as fh:
        fh.write("pas du json\n[1, 2]\n{\"sans\": \"date\"}\n")
    (notifs_dir / notifs.SEEN_FILE).write_text("{cassé")
    data = notifs.liste()
    assert [n["titre"] for n in data["notifs"]] == ["ok"] and data["non_lues"] == 1


def test_rotation_keeps_the_last_300_past_400(notifs_dir):
    for i in range(notifs.ROTATE_AT):
        notifs.noter(f"t{i}\nb", "briefing", True, now=at(i))
    assert len((notifs_dir / notifs.HISTORY_FILE).read_text().splitlines()) == notifs.ROTATE_AT
    notifs.noter("dernière\nb", "briefing", True, now=at(500))
    lines = (notifs_dir / notifs.HISTORY_FILE).read_text().splitlines()
    assert len(lines) == notifs.KEEP and json.loads(lines[-1])["titre"] == "dernière"
    assert not (notifs_dir / "historique.tmp").exists()


def test_noter_never_raises(notifs_dir, monkeypatch):
    (notifs_dir.parent / "notifs-etat").write_text("un fichier, pas un dossier")  # mkdir fails
    assert notifs.noter("a\nb", "briefing", True) is False
    monkeypatch.setattr(notifs, "dossier", lambda settings=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert notifs.noter("a\nb", "briefing", True) is False


def test_envoyer_notes_even_when_telegram_fails(monkeypatch):
    failed = envoi._erreur("telegram en panne")
    monkeypatch.setattr(envoi, "_envoyer_telegram", lambda *a, **k: failed)
    res = envoi.envoyer("🔀 Fusion à faire\ntodo #5")
    assert res is failed and not res.ok
    row = notifs.liste()["notifs"][0]
    assert "Fusion" in row["titre"] and row["onglet"] == "travail"
    assert json.loads((notifs.dossier() / notifs.HISTORY_FILE).read_text())["telegram"] is False


def test_envoyer_result_unchanged_when_history_breaks(monkeypatch):
    ok = envoi.Resultat(envoyes=1)
    monkeypatch.setattr(envoi, "_envoyer_telegram", lambda *a, **k: ok)
    monkeypatch.setattr(notifs, "dossier", lambda settings=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert envoi.envoyer("bonjour") is ok


def test_cli_liste_and_vu(capsys):
    notifs.noter("Salut\ncorps", "moi", True, now=at(1))
    assert cli.main(["notifs", "liste", "--n", "10"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["data"]["non_lues"] == 1 and out["data"]["notifs"][0]["titre"] == "Salut"
    assert cli.main(["notifs", "vu"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["vu_jusqu_a"] == at(1)
    assert notifs.liste()["non_lues"] == 0


@pytest.mark.parametrize("n", ["0", "201", "x"])
def test_cli_refuses_bad_n(n, capsys):
    assert cli.main(["notifs", "liste", "--n", n]) != 0


def test_catalogue_entries_match_the_cli():
    lecture, vu = catalogue.PAR_ID["notifs"], catalogue.PAR_ID["notifs-vu"]
    assert lecture.type == "lecture" and vu.type == "simple" and vu.risque == "low"
    check_argv(list(lecture.argv))
    check_argv(list(vu.argv))


# -- todo #289: the fil (attachments kept and served by name only) ---------------------------------------------------------
def _png(tmp_path, name="capture.png", data=b"\x89PNG fake"):
    f = tmp_path / name
    f.write_bytes(data)
    return f


def test_attachment_is_copied_and_listed(notifs_dir, tmp_path):
    src = _png(tmp_path)
    assert notifs.noter(f"Capture\nvoilà\nMEDIA:{src}", "briefing", True)
    row = notifs.liste()["notifs"][0]
    [pj] = row["pj"]
    assert pj["orig"] == "capture.png" and pj["genre"] == "image" and pj["url"] == "/api/pj/" + pj["nom"]
    assert pj["taille"] == len(b"\x89PNG fake")  # todo #350: the card « nom · taille · ouvrir » needs it
    assert notifs.PJ_RE.fullmatch(pj["nom"]) and "MEDIA" not in row["complet"] and "voilà" in row["complet"]
    kept = notifs.pj_path(pj["nom"])
    assert kept.read_bytes() == src.read_bytes() and kept.parent == notifs_dir / "pj"
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600 and stat.S_IMODE((notifs_dir / "pj").stat().st_mode) == 0o700
    src.unlink()  # survives the scratchpad cleanup
    assert notifs.pj_path(pj["nom"]).exists()


def test_refused_attachments_are_not_kept(notifs_dir, tmp_path, monkeypatch):
    big = _png(tmp_path, "gros.bin", b"x" * 10)
    monkeypatch.setattr(notifs, "PJ_MAX_BYTES", 5)
    secret = tmp_path / ".ssh"
    secret.mkdir()
    (secret / "id_rsa").write_text("clé")
    other = tmp_path / "dir"
    other.mkdir()
    text = "\n".join(["Titre", f"MEDIA:{big}", f"MEDIA:{secret / 'id_rsa'}", f"MEDIA:{other}", f"MEDIA:{tmp_path / 'absent.png'}"])
    assert notifs.noter(text, "briefing", True)
    assert notifs.liste()["notifs"][0]["pj"] == []
    assert not list((notifs_dir / "pj").glob("*")) if (notifs_dir / "pj").exists() else True


@pytest.mark.parametrize("nom", ["../historique.jsonl", "historique.jsonl", "..", "/etc/passwd", "a/b", "20260101T100500-abc123-x.png/..",
                                 "20260101T100500-ABC123-x.png", "", "20260101T100500-abc123-"])
def test_pj_path_refuses_anything_but_a_stored_name(notifs_dir, tmp_path, nom):
    notifs.noter("a\nb", "briefing", True)  # the folder exists, and so does historique.jsonl
    assert notifs.pj_path(nom) is None


def test_pj_path_refuses_a_symlink(notifs_dir, tmp_path):
    notifs.noter(f"a\nMEDIA:{_png(tmp_path)}", "briefing", True)
    (notifs_dir / "pj" / "20260101T100500-abc123-lien.png").symlink_to(notifs_dir / notifs.HISTORY_FILE)
    assert notifs.pj_path("20260101T100500-abc123-lien.png") is None


def test_old_history_without_attachments_is_readable(notifs_dir):
    notifs_dir.mkdir(parents=True)
    (notifs_dir / notifs.HISTORY_FILE).write_text(json.dumps({"at": at(1), "titre": "Ancien", "corps": "corps ancien", "onglet": "moi"}) + "\n")
    [row] = notifs.liste()["notifs"]
    assert row["pj"] == [] and row["complet"] == "corps ancien" and row["titre"] == "Ancien"


def test_forged_pj_names_in_history_are_dropped(notifs_dir):
    notifs_dir.mkdir(parents=True)
    bad = {"at": at(1), "titre": "t", "corps": "c", "pj": [{"nom": "../vu.json"}, "x", {"nom": "20260101T100500-abc123-ok.png"}]}
    (notifs_dir / notifs.HISTORY_FILE).write_text(json.dumps(bad) + "\n")
    assert [p["nom"] for p in notifs.liste()["notifs"][0]["pj"]] == ["20260101T100500-abc123-ok.png"]


def test_rotation_removes_orphan_attachments(notifs_dir, tmp_path):
    notifs.noter(f"vieux\nMEDIA:{_png(tmp_path)}", "briefing", True, now=at(0))
    [old] = list((notifs_dir / "pj").iterdir())
    for i in range(1, notifs.ROTATE_AT + 1):
        notifs.noter(f"t{i}\nb", "briefing", True, now=at(i))
    assert not old.exists()


def test_envoyer_keeps_the_attachment(monkeypatch, tmp_path):
    monkeypatch.setattr(envoi, "_envoyer_telegram", lambda *a, **k: envoi.Resultat(envoyes=1))
    envoi.envoyer(f"Bilan\nvoir\nMEDIA:{_png(tmp_path, 'bilan.pdf')}")
    [pj] = notifs.liste()["notifs"][0]["pj"]
    assert pj["genre"] == "fichier" and notifs.pj_path(pj["nom"]).read_bytes() == b"\x89PNG fake"


def test_the_dashboard_serves_only_stored_attachments(notifs_dir, tmp_path):
    import urllib.error
    import urllib.request
    from ultron_hub.dashboard import apercu
    with apercu.running() as url:
        resp = urllib.request.urlopen(url + "/api/pj/20260101T100500-abc123-capture.png", timeout=5)
        assert resp.headers["Content-Type"] == "image/png" and resp.read()[:4] == b"\x89PNG"
        assert resp.headers["Content-Disposition"].startswith("inline")
        pdf = urllib.request.urlopen(url + "/api/pj/20260101T094000-def456-bilan.pdf", timeout=5)
        assert pdf.headers["Content-Type"] == "application/octet-stream" and "attachment" in pdf.headers["Content-Disposition"]
        md = urllib.request.urlopen(url + "/api/pj/20260101T094001-aaa111-notes.md", timeout=5)
        assert md.headers["Content-Type"].startswith("text/plain") and md.headers["Content-Disposition"].startswith("inline")
        page = urllib.request.urlopen(url + "/api/pj/20260101T094002-bbb222-verset-2-255.html", timeout=5)  # todo #373
        assert page.headers["Content-Type"].startswith("text/html") and page.headers["Content-Disposition"].startswith("inline")
        assert page.headers["Content-Security-Policy"] == "sandbox allow-scripts" and page.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "allow-same-origin" not in page.headers["Content-Security-Policy"]
        audio = urllib.request.urlopen(url + "/api/pj/20260101T094003-ccc333-memo.mp3", timeout=5)
        assert audio.headers["Content-Type"] == "audio/mpeg" and audio.headers["Accept-Ranges"] == "bytes" and audio.headers["Content-Disposition"].startswith("inline")
        part = urllib.request.urlopen(urllib.request.Request(url + "/api/pj/20260101T094003-ccc333-memo.mp3", headers={"Range": "bytes=3-10"}), timeout=5)
        assert part.status == 206 and part.read() == bytes(range(0, 8)) and part.headers["Content-Range"] == "bytes 3-10/259"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(urllib.request.Request(url + "/api/pj/20260101T094003-ccc333-memo.mp3", headers={"Range": "bytes=999-"}), timeout=5)
        assert exc.value.code == 416
        index = urllib.request.urlopen(url + "/", timeout=5)  # the page itself keeps its strict policy
        assert "frame-ancestors 'none'" in index.headers["Content-Security-Policy"] and index.headers["X-Frame-Options"] == "DENY"
        for bad in ("../historique.jsonl", "%2e%2e/historique.jsonl", "..%2Fvu.json", "inconnu", "20260101T100500-abc123-absent.png"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(url + "/api/pj/" + bad, timeout=5)
            assert exc.value.code == 404
        data = json.load(urllib.request.urlopen(url + "/api/state", timeout=5))
        assert data  # the rest of the page is untouched


def test_catalogue_entry_is_the_fil():
    assert catalogue.PAR_ID["notifs"].bouton == "📜 Fil"
    cols = catalogue.projeter(catalogue.PAR_ID["notifs"].donnees, notifs.liste() | {"notifs": [{"at": "x", "titre": "t", "corps": "c", "complet": "cc", "onglet": "moi", "lue": True, "secret": 1,
                                                                                               "pj": [{"nom": "n", "orig": "o", "genre": "image", "url": "u", "chemin": "/x"}]}]})
    assert cols["notifs"][0]["pj"] == [{"nom": "n", "orig": "o", "genre": "image", "url": "u"}] and "secret" not in cols["notifs"][0]


def test_lire_one_marks_only_that_one_and_is_purged(notifs_dir):
    for i in (1, 2, 3):
        notifs.noter(f"t{i}\nb", "briefing", True, now=at(i))
    assert notifs.lire(at(2))["lue"] is True
    assert [r["lue"] for r in notifs.liste()["notifs"]] == [False, True, False]
    assert notifs.liste()["non_lues"] == 2
    notifs.lire("2030-01-01T00:00:00+00:00")  # unknown date: not kept
    assert json.loads((notifs_dir / notifs.SEEN_FILE).read_text())["lues"] == [at(2)]
    notifs.vu()
    assert json.loads((notifs_dir / notifs.SEEN_FILE).read_text())["lues"] == []
    with pytest.raises(ValueError):
        notifs.lire("2026-01-01T10:00:01+00:00; rm")


def test_cible_derived_from_text_or_given_and_absent_otherwise(notifs_dir):
    notifs.noter("Fusion prête\nLa todo #299 attend", "travail", True, now=at(1))
    notifs.noter("Salut\nultron-20-10-13h32 a fini", "briefing", True, now=at(2))
    notifs.noter("Rien\nde précis", "briefing", True, now=at(3))
    notifs.noter("Autre\nsans réf", "briefing", True, now=at(4), cible={"genre": "todo", "ref": "7"})
    c = [r["cible"] for r in notifs.liste()["notifs"]][::-1]
    assert c[0] == {"genre": "todo", "ref": "299", "libelle": "Voir la todo #299"}
    assert c[1]["libelle"] == "Ouvrir la session ultron-20-10-13h32" and c[2] is None and c[3]["ref"] == "7"
    assert notifs.cible_depuis("version v#1x", "a#5") is None


def test_todo_key_for_grouping_is_server_side(notifs_dir):  # todo #347
    notifs.noter("Étape\nLa todo #299 avance", "travail", True, now=at(1))
    notifs.noter("Salut\nultron-20-10-13h32 a fini", "briefing", True, now=at(2))
    notifs.noter("Autre\nsans réf", "briefing", True, now=at(3), cible={"genre": "todo", "ref": "7"})
    notifs.noter("Rien\nde précis", "briefing", True, now=at(4))
    assert [r["todo"] for r in notifs.liste()["notifs"]][::-1] == ["299", None, "7", None]


def test_cli_lire(capsys, notifs_dir):
    notifs.noter("a\nb", "briefing", True, now=at(1))
    assert cli.main(["notifs", "lire", at(1)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["lue"] is True
    assert cli.main(["notifs", "lire", "x"]) != 0


@pytest.mark.parametrize("texte,attendu", [
    ("Fusion prête\nla todo #1", "feu-vert"), ("🔀 Branche à fusionner", "feu-vert"),
    ("Choix\ntu décides quoi ?", "question"), ("Titre\nOn continue ?", "question"),
    ("🚨 Service tombé", "alerte"), ("Titre\n❌ échec", "alerte"),
    ("Reçu\nMEDIA:/tmp/x.png", "fichier"), ("Bilan du jour\nok", "bilan"), ("Titre\nEn clair : ça va", "bilan"),
    ("Tests passés\nrien à dire", "fait"), ("", "fait"),
    ("Photo ?\nMEDIA:/tmp/x.png", "question"),
])
def test_deduire_genre(texte, attendu):
    assert notifs.deduire_genre(texte) == attendu


def test_noter_keeps_given_genre_and_deduces_otherwise(notifs_dir):
    notifs.noter("Tests passés\nok", "briefing", True, now=at(1), genre="bilan")
    notifs.noter("Tests passés\nok", "briefing", True, now=at(2))
    notifs.noter("Tests passés\nok", "briefing", True, now=at(3), genre="inconnu")
    assert [n["genre"] for n in reversed(notifs.liste()["notifs"])] == ["bilan", "fait", "fait"]


def test_old_rows_get_a_genre_when_read(notifs_dir):
    notifs_dir.mkdir()
    (notifs_dir / notifs.HISTORY_FILE).write_text(json.dumps({"at": at(1), "titre": "🚨 Panne", "corps": "x"}) + "\n")
    assert notifs.liste()["notifs"][0]["genre"] == "alerte"


def test_send_genre_reaches_the_fil(notifs_dir, monkeypatch, capsys):
    monkeypatch.setattr(envoi, "_envoyer_telegram", lambda *a, **k: envoi.Resultat(envoyes=1))
    assert cli.main(["send", "--genre", "bilan", "Salut\ncorps"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert notifs.liste()["notifs"][0]["genre"] == "bilan"


def test_send_refuses_unknown_genre(notifs_dir, monkeypatch, capsys):
    monkeypatch.setattr(envoi, "_envoyer_telegram", lambda *a, **k: pytest.fail("must not send"))
    cli.main(["send", "--genre", "bidule", "Salut"])
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error" and "genre" in out["error"] and notifs.liste()["notifs"] == []


# -- Todo #344: titles that say something ------------------------------------------------------------------------------
def test_titre_sens_first_sentence_without_head_noise():
    texte = "🤖 #340 Le correctif est fusionné. Le reste suit demain\nMEDIA:/tmp/x.png\nDeuxième ligne"
    assert notifs.titre_sens("Ultron", texte) == "Le correctif est fusionné"
    assert notifs.resume_sens("Ultron", texte) == "Le reste suit demain Deuxième ligne"
    assert notifs.titre_sens("", "v1.2 sortie ! ok") == "v1.2 sortie"
    assert notifs.titre_sens("Ultron", "Ultron : session orchestrateur ouverte. Suite") == "session orchestrateur ouverte"


def test_titre_sens_keeps_a_real_title_and_cuts_long_ones():
    assert notifs.titre_sens("Fusion prête", "autre chose") == "Fusion prête"
    long = "mot " * 40
    titre = notifs.titre_sens("Ultron", long)
    assert len(titre) <= notifs.TITRE_MAX and titre.endswith("…") and not titre.endswith(" …")
    assert len(notifs.resume_sens("Ultron", "Court.\n" + long)) <= notifs.RESUME_MAX


def test_liste_derives_title_and_summary_for_ultron_rows(notifs_dir):
    assert notifs.noter("Ultron\n✅ La todo est finie. Détail suivant", "briefing", True, now=at(1))
    n = notifs.liste()["notifs"][0]
    assert n["titre"] == "La todo est finie" and n["resume"] == "Détail suivant"


def test_titre_sens_is_masked_before(notifs_dir):
    row = {"at": at(5), "titre": "Ultron", "corps": "", "onglet": "briefing", "telegram": True,
           "complet": "Voir /home/exemple/.ssh/id_rsa avec sk-abcdefghijklmnopqrstuvwxyz123456 pour la suite"}
    notifs_dir.mkdir()
    (notifs_dir / notifs.HISTORY_FILE).write_text(json.dumps(row) + "\n")
    n = notifs.liste()["notifs"][0]
    assert ".ssh" not in n["titre"] + n["resume"] and "sk-abcdef" not in n["titre"] + n["resume"]


def test_redemarrage_todos_pure():
    assert notifs.redemarrage_todos({"corps": "✅ Le bot redémarré (fusion de #243, #284)"}) == ["243", "284"]
    assert notifs.redemarrage_todos({"corps": "❌ Le bot redémarré (fusion de #5) : échec"}) is None
    assert notifs.redemarrage_todos({"corps": "🔀 #5 fusionnée"}) is None


def test_liste_folds_restart_into_its_merge_row(notifs_dir):
    notifs.noter("🤖 #7 fusionnée auto (abc)\nok", "travail", True, now=at(1))
    notifs.noter("🤖 #8 fusionnée auto (def)\nok", "travail", True, now=at(2))
    notifs.noter("Ultron\n✅ Le bot redémarré (fusion de #7)", "travail", True, now=at(3))
    out = notifs.liste()
    assert [x["at"] for x in out["notifs"]] == [at(2), at(1)]
    assert {x["at"]: x["etiquettes"] for x in out["notifs"]} == {at(1): ["bot redémarré"], at(2): []}
    assert out["non_lues"] == 2


def test_liste_keeps_orphan_and_failed_restarts(notifs_dir):
    notifs.noter("Ultron\n✅ Le bot redémarré (fusion de #9)", "travail", True, now=at(1))
    notifs.noter("🤖 #9 fusionnée auto (abc)\nok", "travail", True, now=at(2))
    notifs.noter("Ultron\n❌ Le bot redémarré (fusion de #9) : échec", "travail", True, now=at(3))
    out = notifs.liste()
    assert len(out["notifs"]) == 3 and all(x["etiquettes"] == [] for x in out["notifs"])


def test_folded_restart_is_read_with_its_merge_row(notifs_dir):
    notifs.noter("🤖 #7 fusionnée auto (abc)\nok", "travail", True, now=at(1))
    notifs.noter("Ultron\n✅ Le bot redémarré (fusion de #7)", "travail", True, now=at(2))
    assert notifs.liste()["non_lues"] == 1
    notifs.lire(at(1))
    assert notifs.liste()["non_lues"] == 0


@pytest.mark.parametrize("titre,pj,attendu", [
    ("Capture prête", [{"nom": "x"}], "fichier"),
    ("⚠ Disque presque plein", [], "alerte"),
    ("🚨 Bot arrêté", [], "alerte"),
    ("Panne du bot", [], "alerte"),
    ("Échec de la sauvegarde", [], "alerte"),
    ("Bilan de la journée", [], "bilan"),
    ("Point de situation", [], "bilan"),
    ("Passation de session", [], "bilan"),
    ("Pointeur mis à jour", [], "fait"),
    ("Fusion terminée", [], "fait"),
])
def test_genre_fil(titre, pj, attendu):
    assert notifs.genre_fil(titre, pj) == attendu


def test_liste_gives_genre_fil_and_catalogue_keeps_it(notifs_dir):
    notifs.noter("Ultron\nPanne du bot, relancé", "briefing", True, now=at(1))
    notifs.noter("Bilan du jour\nvoilà", "briefing", True, now=at(2))
    notifs.noter("Fusion faite\nok", "briefing", True, now=at(3))
    got = {n["titre"]: n["genre_fil"] for n in notifs.liste()["notifs"]}
    assert got["Bilan du jour"] == "bilan" and got["Fusion faite"] == "fait"
    assert "alerte" in got.values()  # raw title « Ultron » is replaced by the shown one (the sentence), which carries « Panne »
    kept = catalogue.projeter(catalogue.NOTIFS, {"notifs": notifs.liste()["notifs"], "non_lues": 3})
    assert all("genre_fil" in n for n in kept["notifs"])


@pytest.mark.parametrize("nom,genre", [("a.png", "image"), ("a.MD", "texte"), ("a.txt", "texte"), ("a.html", "html"),
                                       ("a.mp3", "audio"), ("a.m4a", "audio"), ("a.pdf", "fichier"), ("a.svg", "fichier")])
def test_attachment_genres_for_the_reader(nom, genre):
    assert notifs.genre(nom) == genre


def test_the_dashboard_refuses_an_unknown_device_for_attachments(notifs_dir, monkeypatch):
    import urllib.error
    import urllib.request
    from ultron_hub.dashboard import apercu
    monkeypatch.setattr(apercu._Caller, "caller", lambda self, ip: None)
    with apercu.running() as url:
        for nom in ("20260101T094002-bbb222-verset-2-255.html", "20260101T100500-abc123-capture.png"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(url + "/api/pj/" + nom, timeout=5)
            assert exc.value.code == 403


def test_no_attachment_link_opens_a_new_tab():
    """Anti-breakdown (todo #373): in the installed iOS app a target=_blank attachment leaves the page with no way back."""
    from ultron_hub.dashboard import server
    js = (Path(server.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    start = js.index("function ouvrirLecteur"), js.index("var filDonnees")
    bloc = js[start[0]:start[1]]
    assert "_blank" not in bloc and "popstate" in js and "pushState" in js
    assert 'setAttribute("sandbox", "allow-scripts")' in bloc


def _var_src(notifs_dir, mode, name="r.png"):
    var = notifs_dir.parent
    (var / "recus").mkdir(exist_ok=True)
    f = var / "recus" / name
    f.write_bytes(b"\x89PNG" + b"1" * 20)
    f.chmod(mode)
    return f


def test_private_var_source_is_hard_linked(notifs_dir):
    src = _var_src(notifs_dir, 0o600)
    assert notifs.noter(f"Titre\nMEDIA:{src}", "briefing", True)
    kept = notifs.pj_path(notifs.liste()["notifs"][0]["pj"][0]["nom"])
    assert kept.stat().st_ino == src.stat().st_ino and src.stat().st_nlink == 2
    assert stat.S_IMODE(src.stat().st_mode) == 0o600
    src.unlink()
    assert kept.read_bytes().startswith(b"\x89PNG")


def test_shared_mode_var_source_is_copied_and_untouched(notifs_dir):
    src = _var_src(notifs_dir, 0o664)
    assert notifs.noter(f"Titre\nMEDIA:{src}", "briefing", True)
    kept = notifs.pj_path(notifs.liste()["notifs"][0]["pj"][0]["nom"])
    assert kept.stat().st_ino != src.stat().st_ino and stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert stat.S_IMODE(src.stat().st_mode) == 0o664 and kept.read_bytes() == src.read_bytes()


def test_link_failure_falls_back_to_copy(notifs_dir, monkeypatch):
    src = _var_src(notifs_dir, 0o600)

    def boom(*a, **k):
        raise OSError("EXDEV")
    monkeypatch.setattr(notifs.os, "link", boom)
    assert notifs.noter(f"Titre\nMEDIA:{src}", "briefing", True)
    kept = notifs.pj_path(notifs.liste()["notifs"][0]["pj"][0]["nom"])
    assert kept.stat().st_ino != src.stat().st_ino and kept.read_bytes() == src.read_bytes()


def test_purge_of_a_linked_attachment_keeps_the_source(notifs_dir):
    src = _var_src(notifs_dir, 0o600)
    assert notifs.noter(f"Titre\nMEDIA:{src}", "briefing", True)
    nom = notifs.liste()["notifs"][0]["pj"][0]["nom"]
    notifs._menage_pj(notifs_dir, [])
    assert not (notifs_dir / "pj" / nom).exists() and src.read_bytes().startswith(b"\x89PNG")


def test_separer_technique_todo_351():
    t = "Branche prête.\nCommit 3f9a1c2b sur todo-351/details\n3306 passed\nrc=0\nVoir {{HOME}}/x.md\nMerci."
    principal, tech = notifs.separer_technique(t)
    assert principal == "Branche prête.\nMerci."
    assert len(tech) == 4 and "3306 passed" in tech
    assert notifs.separer_technique("Tout est fini, décidé.") == ("Tout est fini, décidé.", [])
    tout = "rc=0\n3306 passed"
    assert notifs.separer_technique(tout) == (tout, [])
