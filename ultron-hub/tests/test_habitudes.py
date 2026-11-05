"""Todo #429: 💡 Nouvelle habitude proposée, a skill draft of fin-de-chantier installed by a ✅ signed on its exact text."""

import json
import os
import time

import pytest

from hermes_plugin import close_key
from test_dashboard_actions import confirm, dash, fake_cli, prepare, session  # noqa: F401  (fixtures)
from ultron_hub import cli, habitudes
from ultron_hub.dashboard import apercu, catalogue
from ultron_hub.todo import closing

DOSSIER = "2026-10-25-relire-diff"
SKILL = "---\nname: relire-diff\ndescription: \"Avant 🔀, relire le diff.\"\n---\n\n# relire-diff\n\n## Étapes\n1. git diff\n"
RAISON = ("# Raison\n\n## Gestes répétés\nfusion ×5, diff ×4, tests ×3, code ×2\n\n## Corrections de l'utilisateur\n- 10:50 a\n"
          "- 11:00 b\n\n## Pièges rencontrés\n- (aucun)\n")


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def brouillon(base, dossier=DOSSIER, mode="nouvelle", nom="relire-diff", texte=SKILL):
    d = base / dossier
    d.mkdir(parents=True)
    (d / "BROUILLON.md").write_text(f"# Brouillon {mode} : {nom}\n\nJour : 2026-10-25. Statut : EN ATTENTE du ✅ de l'utilisateur.\n")
    (d / "RAISON.md").write_text(RAISON)
    if mode == "nouvelle":
        (d / "SKILL.md").write_text(texte)
    elif mode == "remplace":
        (d / "REMPLACE.md").write_text(texte)
    else:
        (d / "PATCH.md").write_text(f"# Patch proposé pour ~/.claude/skills/{nom}/SKILL.md\n\nÀ ajouter (après ✅) :\n\n{texte}")
    return d


@pytest.fixture
def lieux(project, tmp_path, monkeypatch):
    props, skills = tmp_path / "propositions", tmp_path / "skills"
    props.mkdir()
    skills.mkdir()
    monkeypatch.setenv(habitudes.PROPOSITIONS_ENV, str(props))
    monkeypatch.setenv(habitudes.SKILLS_ENV, str(skills))
    return props, skills


def liste(capsys):
    code, out = run_cli(capsys, "habitudes", "liste")
    assert code == 0, out
    return out["data"]["habitudes"]


def installer(capsys, monkeypatch, dossier, emp, token_target=None):
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("habitude", token_target or f"{dossier}:{emp}"))
    return run_cli(capsys, "habitudes", "installer", "--dossier", dossier, "--empreinte", emp)


def test_liste_gives_the_exact_text_and_its_fingerprint(lieux, capsys):
    props, _ = lieux
    brouillon(props)
    (props / "jetees").mkdir()
    [h] = liste(capsys)
    assert h["dossier"] == DOSSIER and h["nom"] == "relire-diff" and h["mode"] == "nouvelle" and h["texte"] == SKILL
    assert h["empreinte"] == habitudes.empreinte("nouvelle", "relire-diff", SKILL) and len(h["empreinte"]) == 16
    assert h["complet"] is True and h["trop_long"] is False
    assert h["resume"].count(".") >= 2 and "2 correction(s)" in h["resume"] and "fusion ×5" in h["resume"]


def test_a_new_skill_is_installed_then_the_draft_is_filed(lieux, capsys, monkeypatch):
    props, skills = lieux
    brouillon(props)
    emp = liste(capsys)[0]["empreinte"]
    code, out = run_cli(capsys, "habitudes", "installer", "--dossier", DOSSIER, "--empreinte", emp)  # no token
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and not (skills / "relire-diff").exists()
    code, out = installer(capsys, monkeypatch, DOSSIER, emp)
    assert code == 0, out
    f = skills / "relire-diff" / "SKILL.md"
    assert f.read_text() == SKILL and oct(f.stat().st_mode & 0o777) == "0o644"
    assert oct(f.parent.stat().st_mode & 0o777) == "0o755"
    rangé = props / "appliquees" / DOSSIER
    assert "Statut : APPLIQUÉ" in (rangé / "BROUILLON.md").read_text() and not (props / DOSSIER).exists()
    assert liste(capsys) == []


def test_a_patch_is_appended_under_a_dated_title(lieux, capsys, monkeypatch):
    props, skills = lieux
    (skills / "ultron-retex").mkdir()
    (skills / "ultron-retex" / "SKILL.md").write_text("# ultron-retex\n")
    brouillon(props, mode="patch", nom="ultron-retex", texte="- Règle : relire le diff\n")
    [h] = liste(capsys)
    assert h["mode"] == "patch" and h["texte"] == "- Règle : relire le diff\n"
    code, out = installer(capsys, monkeypatch, DOSSIER, h["empreinte"])
    assert code == 0, out
    text = (skills / "ultron-retex" / "SKILL.md").read_text()
    assert text.startswith("# ultron-retex\n\n## Ajout du ") and f"(brouillon {DOSSIER})" in text and text.endswith("relire le diff\n")


def test_a_changed_text_is_refused_and_nothing_is_installed(lieux, capsys, monkeypatch):
    props, skills = lieux
    d = brouillon(props)
    emp = liste(capsys)[0]["empreinte"]
    (d / "SKILL.md").write_text(SKILL + "- ajout glissé\n")
    code, out = installer(capsys, monkeypatch, DOSSIER, emp)
    assert code == 1 and "a changé" in out["error"] and not (skills / "relire-diff").exists()
    new = liste(capsys)[0]["empreinte"]
    code, out = installer(capsys, monkeypatch, DOSSIER, new, token_target=f"{DOSSIER}:{emp}")  # token of the old text
    assert code == 1 and "invalide" in out["error"] and not (skills / "relire-diff").exists()


@pytest.mark.parametrize("nom", ["Relire", "relire_diff", "synced", "fin-de-chantier"])
def test_a_bad_or_forbidden_name_is_refused(lieux, capsys, monkeypatch, nom):
    props, skills = lieux
    brouillon(props, nom=nom)
    emp = liste(capsys)[0]["empreinte"]
    code, out = installer(capsys, monkeypatch, DOSSIER, emp)
    assert code == 1 and ("nom de skill invalide" in out["error"] or "jamais" in out["error"]), out
    assert list(skills.iterdir()) == [] and (props / DOSSIER).exists()


def test_an_existing_skill_is_never_replaced(lieux, capsys, monkeypatch):
    props, skills = lieux
    (skills / "relire-diff").mkdir()
    (skills / "relire-diff" / "SKILL.md").write_text("garde-moi\n")
    brouillon(props)
    code, out = installer(capsys, monkeypatch, DOSSIER, liste(capsys)[0]["empreinte"])
    assert code == 1 and "existe déjà" in out["error"] and (skills / "relire-diff" / "SKILL.md").read_text() == "garde-moi\n"


def test_an_incomplete_draft_is_refused(lieux, capsys, monkeypatch):
    props, skills = lieux
    brouillon(props, texte=SKILL + "\n## Étapes\nA COMPLÉTER (gestes répétés : x)\n")
    [h] = liste(capsys)
    assert h["complet"] is False
    code, out = installer(capsys, monkeypatch, DOSSIER, h["empreinte"])
    assert code == 1 and "à compléter" in out["error"] and list(skills.iterdir()) == []


def test_symbolic_links_and_outside_folders_are_refused(lieux, capsys, monkeypatch, tmp_path):
    props, skills = lieux
    secret = tmp_path / "secret.md"
    secret.write_text("secret\n")
    d = brouillon(props)
    (d / "SKILL.md").unlink()
    os.symlink(secret, d / "SKILL.md")
    assert liste(capsys) == []  # a link inside: never shown
    emp = habitudes.empreinte("nouvelle", "relire-diff", "secret\n")
    code, out = installer(capsys, monkeypatch, DOSSIER, emp)
    assert code == 1 and "lien symbolique" in out["error"] and list(skills.iterdir()) == []
    ailleurs = brouillon(tmp_path / "ailleurs", dossier="2026-10-26-autre")
    os.symlink(ailleurs, props / "2026-10-26-autre")
    assert liste(capsys) == []
    code, out = installer(capsys, monkeypatch, "2026-10-26-autre", habitudes.empreinte("nouvelle", "relire-diff", SKILL))
    assert code == 1 and "lien symbolique" in out["error"]
    for bad in ("../ailleurs", "x"):
        code, out = run_cli(capsys, "habitudes", "installer", "--dossier", bad, "--empreinte", "0123456789abcdef", "--i-am-human")
        assert code == 1 and "invalide" in out["error"]
    os.symlink(tmp_path / "ailleurs", skills / "lien")  # the target skill is a link
    brouillon(props, dossier="2026-10-27-lien", nom="lien")
    h = next(x for x in liste(capsys) if x["nom"] == "lien")
    code, out = installer(capsys, monkeypatch, "2026-10-27-lien", h["empreinte"])
    assert code == 1 and "lien symbolique" in out["error"]


def test_jeter_moves_the_draft_and_deletes_nothing(lieux, capsys):
    props, skills = lieux
    brouillon(props)
    code, out = run_cli(capsys, "habitudes", "jeter", "--dossier", DOSSIER)
    assert code == 0, out
    assert (props / "jetees" / DOSSIER / "SKILL.md").read_text() == SKILL
    assert "Statut : JETÉ" in (props / "jetees" / DOSSIER / "BROUILLON.md").read_text()
    assert liste(capsys) == [] and list(skills.iterdir()) == []


def test_catalogue_signs_habitude_on_folder_and_fingerprint_alike_on_both_sides():
    a = catalogue.PAR_ID["habitude"]
    _, args, stdin = catalogue.valider("habitude", {"dossier": DOSSIER, "empreinte": "0123456789abcdef"})
    assert a.signe == "habitude" and catalogue.cible(a, args, stdin) == f"{DOSSIER}:0123456789abcdef"
    assert a.detail == "habitude-voir" and catalogue.PAR_ID["habitudes"].type == "lecture"
    assert catalogue.PAR_ID["habitude-jeter"].type == "simple"
    for bad in ({"dossier": "../x"}, {"dossier": "2026-10-25-A"}, {"empreinte": "0123"}):
        with pytest.raises(catalogue.Refused):
            catalogue.valider("habitude", {"dossier": DOSSIER, "empreinte": "0123456789abcdef", **bad})
    assert close_key.ACTIONS == closing.ACTIONS and close_key.DASHBOARD_ACTIONS == closing.DASHBOARD_ACTIONS
    key, target = b"k" * 64, f"{DOSSIER}:0123456789abcdef"
    assert close_key.token("habitude", target, key=key, now=1000) == closing.sign(key, "habitude", target, 1000)
    assert close_key.dashboard_token("iphone", "habitude", target, key=key, now=1000) == \
        closing.sign_dashboard(key, "iphone", "habitude", target, 1000)
    tok = closing.sign_dashboard(close_key.load_or_create(), "iphone", "habitude", target, int(time.time()))
    assert closing.check(tok, "habitude", target)[1] == "dashboard:iphone"
    with pytest.raises(closing.CloseRefused):
        closing.check(tok, "rapports-lot", target)
    fake = catalogue.projeter(catalogue.PAR_ID["habitudes"].donnees, apercu.LECTURES["habitudes"])
    assert [h["complet"] for h in fake["habitudes"]] == [True, False] and fake["habitudes"][0]["texte"]


def test_the_sheet_shows_the_exact_text_and_refuses_a_changed_one(dash, monkeypatch):
    lu = []
    voir = {"empreinte": "0123456789abcdef", "lignes": ["# relire-diff", "", "1. git diff"]}
    monkeypatch.setattr(dash.acts, "_lire_detail", lambda reading, args: lu.append((reading.id, args)) or voir)
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "habitude", "dossier": DOSSIER, "empreinte": "0123456789abcdef"})
    assert out["ok"] and out["detail"] == voir["lignes"] and out["detail_brut"] is True
    assert lu == [("habitude-voir", {"dossier": DOSSIER})]
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["habitudes", "installer", f"--dossier={DOSSIER}", "--empreinte=0123456789abcdef"]
    assert closing.check(call["env"][closing.TOKEN_ENV], "habitude", f"{DOSSIER}:0123456789abcdef")[1].startswith("dashboard:")
    _, out, _ = prepare(dash, sid, {"action": "habitude", "dossier": DOSSIER, "empreinte": "fedcba9876543210"})
    assert not out["ok"] and "a changé" in out["erreur"]
    voir["lignes"] = ["x"] * (habitudes.LIGNES_MAX + 1)
    _, out, _ = prepare(dash, sid, {"action": "habitude", "dossier": DOSSIER, "empreinte": "0123456789abcdef"})
    assert not out["ok"] and "trop long" in out["erreur"] and len(dash.calls()) == 1


def test_a_replacement_swaps_the_whole_skill_and_keeps_the_old_one(lieux, capsys, monkeypatch):
    props, skills = lieux
    (skills / "ultron-retex").mkdir()
    (skills / "ultron-retex" / "SKILL.md").write_text("# ancien\n")
    brouillon(props, mode="remplace", nom="ultron-retex", texte="# nouveau\n")
    [h] = liste(capsys)
    assert h["mode"] == "remplace" and h["texte"] == "# nouveau\n" and h["resume"].startswith("Remplacement de la skill « ultron-retex »")
    code, out = installer(capsys, monkeypatch, DOSSIER, h["empreinte"])
    assert code == 0, out
    assert (skills / "ultron-retex" / "SKILL.md").read_text() == "# nouveau\n"
    assert (skills / "ultron-retex" / f"SKILL.md.bak-{DOSSIER}").read_text() == "# ancien\n"


def test_a_replacement_of_a_missing_skill_is_refused(lieux, capsys, monkeypatch):
    props, skills = lieux
    brouillon(props, mode="remplace", nom="absente", texte="# x\n")
    code, out = installer(capsys, monkeypatch, DOSSIER, liste(capsys)[0]["empreinte"])
    assert code != 0 and not (skills / "absente").exists()
