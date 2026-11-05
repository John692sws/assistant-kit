"""`groupe creer` (todo #246) : sujets du groupe depuis le dashboard, aucun réseau (`envoi._urlopen` simulé)."""
from tests.test_groupe_chantiers import BASE, groupe  # noqa: F401  (fixture)
from ultron_hub import envoi
from ultron_hub.groupe import commands, sujets


def prompts(tmp_path, *noms):
    d = tmp_path / "prompt"
    d.mkdir()
    for n in noms:
        (d / f"{n}.md").write_text("Domaine : business\n" if n != "_socle" else "x")
    return d




def test_creates_missing_domain_topics_then_chantiers_and_is_idempotent(groupe, tmp_path):  # noqa: F811
    path, calls, _, settings = groupe
    d = prompts(tmp_path, "alpha", "_socle")
    res = sujets.creer(settings, path=path, prompts=d)
    assert res["status"] == "ok" and res["summary"] == "6 sujet(s) créé(s)", res
    assert [c[1]["name"] for c in calls] == [t for k, t in sujets.TOPICS if k not in ("systeme", "missions")] + ["💼 alpha"]
    etat = envoi.lire_groupe(path)
    assert set(etat["sujets"]) == {k for k, _ in sujets.TOPICS} and set(etat["chantiers"]) == {"alpha"}
    n = len(calls)
    assert sujets.creer(settings, path=path, prompts=d)["summary"] == "tous les sujets existent déjà" and len(calls) == n


def test_telegram_refusal_keeps_what_was_created(groupe, tmp_path):  # noqa: F811
    path, calls, plan, settings = groupe
    plan["createForumTopic"] = "Not enough rights"
    res = sujets.creer(settings, path=path, prompts=prompts(tmp_path))
    assert res["status"] == "error" and "Not enough rights" in res["summary"]
    assert envoi.lire_groupe(path)["sujets"] == {"systeme": 11, "missions": 12}


def test_skipped_without_group_or_in_hermes_mode(groupe, tmp_path, monkeypatch):  # noqa: F811
    path, calls, _, settings = groupe
    path.write_text("")
    assert "groupe inconnu" in sujets.creer(settings, path=path, prompts=prompts(tmp_path))["summary"]
    monkeypatch.setenv(envoi.MODE_ENV, "hermes")
    res = sujets.creer(settings, path=path, prompts=tmp_path / "prompt")
    assert res["status"] == "skipped" and calls == []


def test_command_envelope_is_an_error_when_nothing_was_done(groupe, monkeypatch):  # noqa: F811
    _, _, _, settings = groupe
    monkeypatch.setattr(sujets, "creer", lambda s: {"status": "skipped", "summary": "mode hermes"})
    out = commands.handle(type("A", (), {"groupe_command": "creer"})(), settings)
    assert out["status"] == "error" and out["error"] == "mode hermes" and out["command"] == "groupe.creer"
