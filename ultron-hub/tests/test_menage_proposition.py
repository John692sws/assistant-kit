"""Todo #362: 🧹 Ménage proposé, one signed ✅ on the proposal's fingerprint applies the whole tidy-up."""

import json
import time

import pytest

from hermes_plugin import close_key
from ultron_hub import cli
from ultron_hub.dashboard import apercu, catalogue
from ultron_hub.groupe import chantiers
from ultron_hub import menage_lot as proposition
from ultron_hub.todo import closing


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.fixture
def hub(project, prompts_dir, capsys, monkeypatch):
    monkeypatch.setattr(chantiers, "fermer", lambda settings, sujet: {"status": "skipped"})
    prompts_dir.mkdir()
    for name in ("vieux", "autre"):
        (prompts_dir / f"{name}.md").write_text(f"# Chantier « {name} »\n")
    for title, chantier in (("Une", "vieux"), ("Deux", None), ("Trois", None)):
        extra = ("--chantier", chantier) if chantier else ()
        code, out = run_cli(capsys, "todo", "add", "--title", title, "--notes", f"notes {title}", *extra)
        assert code == 0, out
    return project


def status(capsys, tid):
    return run_cli(capsys, "todo", "show", str(tid))[1]["data"]["status"]


def propose(capsys, *argv):
    code, out = run_cli(capsys, "menage", "proposer", *argv)
    assert code == 0, out
    return out["data"]


def apply(capsys, monkeypatch, emp, token_emp=None):
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("menage-lot", token_emp or emp))
    return run_cli(capsys, "menage", "appliquer", "--empreinte", emp)


def test_one_signature_cancels_then_archives_and_consumes(hub, capsys, monkeypatch, prompts_dir):
    data = propose(capsys, "--cancel", "1:remplacée", "--cancel", "2:plus utile", "--archiver", "vieux", "--par", "orch")
    assert data["n"] == 3 and data["par"] == "orch" and data["bloquee"] is None
    assert [c["titre"] for c in data["cancel"]] == ["Une", "Deux"]
    emp = data["empreinte"]
    assert run_cli(capsys, "menage", "proposition")[1]["data"]["empreinte"] == emp
    code, out = run_cli(capsys, "menage", "appliquer", "--empreinte", emp)  # no token
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and "menage appliquer" in out["error"]
    assert status(capsys, 1) == "todo"
    code, out = apply(capsys, monkeypatch, emp)
    assert code == 0 and out["data"]["faits"] == ["#1", "#2", "chantier vieux"] and "3/3" in out["data"]["text"]
    assert status(capsys, 1) == status(capsys, 2) == "cancelled" and status(capsys, 3) == "todo"
    shown = run_cli(capsys, "todo", "show", "1")[1]["data"]
    assert shown["notes"] == "notes Une"  # notes kept
    [event] = [h for h in shown["history"] if h["action"] == "status"]
    assert event["details"]["via"] == "menage-lot" and event["details"]["empreinte"] == emp and event["details"]["proof"]
    assert not (prompts_dir / "vieux.md").exists() and (prompts_dir / "termines" / "vieux.md").is_file()
    assert run_cli(capsys, "menage", "proposition")[1]["data"]["n"] == 0  # consumed
    code, out = run_cli(capsys, "menage", "appliquer", "--empreinte", emp)  # same token replayed
    assert code == 1 and "aucune proposition" in out["error"]
    # the same proposal written again within the token's life: the spent token is refused
    token = closing.os.environ[closing.TOKEN_ENV]
    monkeypatch.delenv(closing.TOKEN_ENV)
    run_cli(capsys, "todo", "reopen", "1")
    run_cli(capsys, "todo", "reopen", "2")
    (prompts_dir / "termines" / "vieux.md").rename(prompts_dir / "vieux.md")
    assert propose(capsys, "--cancel", "1:remplacée", "--cancel", "2:plus utile", "--archiver", "vieux")["empreinte"] == emp
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    code, out = run_cli(capsys, "menage", "appliquer", "--empreinte", emp)
    assert code == 1 and "déjà utilisé" in out["error"] and status(capsys, 1) == "todo"


def test_changed_proposal_is_refused_and_nothing_applied(hub, capsys, monkeypatch):
    old = propose(capsys, "--cancel", "2:raison")["empreinte"]
    new = propose(capsys, "--cancel", "2:autre raison")["empreinte"]
    assert old != new
    code, out = apply(capsys, monkeypatch, old)
    assert code == 1 and "a changé" in out["error"] and status(capsys, 2) == "todo"
    assert run_cli(capsys, "menage", "proposition")[1]["data"]["n"] == 1  # still there
    code, out = apply(capsys, monkeypatch, new, token_emp=old)  # token signed on another fingerprint
    assert code == 1 and status(capsys, 2) == "todo"
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("cancel", 2))  # a single ✖ token is not a ménage token
    assert run_cli(capsys, "menage", "appliquer", "--empreinte", new)[0] == 1 and status(capsys, 2) == "todo"
    assert run_cli(capsys, "menage", "appliquer", "--empreinte", "XYZ")[1]["error"].startswith("empreinte invalide")


def test_todo_closed_since_the_card_is_refused_whole(hub, capsys, monkeypatch):
    emp = propose(capsys, "--cancel", "2:raison", "--cancel", "3:raison")["empreinte"]
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("done", 3))
    assert run_cli(capsys, "todo", "done", "3")[0] == 0
    view = run_cli(capsys, "menage", "proposition")[1]["data"]
    assert view["bloquee"] == "todo #3 n'est plus ouverte (done)" and view["cancel"][1]["refus"]
    code, out = apply(capsys, monkeypatch, emp)
    assert code == 1 and "#3 n'est plus ouverte" in out["error"] and "Rien n'a été appliqué" in out["error"]
    assert status(capsys, 2) == "todo"


def test_first_refusal_stops_and_reports_the_rest(hub, capsys, monkeypatch, prompts_dir):
    # « vieux » still holds open todo #1: its archiving is refused, « autre » after it is not applied
    emp = propose(capsys, "--cancel", "2:raison", "--archiver", "vieux", "--archiver", "autre")["empreinte"]
    code, out = apply(capsys, monkeypatch, emp)
    data = out["data"]
    assert code == 1 and data["faits"] == ["#2"] and data["arret"]["item"] == "chantier vieux"
    assert "#1" in data["arret"]["raison"] and data["restants"] == ["chantier autre"]
    assert "Pas appliqué : chantier autre" in data["text"]
    assert status(capsys, 2) == "cancelled" and (prompts_dir / "vieux.md").is_file() and (prompts_dir / "autre.md").is_file()
    assert run_cli(capsys, "menage", "proposition")[1]["data"]["n"] == 0  # consumed once applying started


def test_proposer_refusals(hub, capsys):
    for argv, why in ((("--cancel", "99:raison"), "introuvable"), (("--cancel", "2"), "ID:raison"),
                      (("--cancel", "2:" + "x" * 201), "ID:raison"), (("--archiver", "inconnu"), "introuvable"),
                      (("--cancel", "2:a", "--cancel", "2:b"), "deux fois"), ((), "vide"),
                      (("--archiver", "main"), "refusé")):
        code, out = run_cli(capsys, "menage", "proposer", *argv)
        assert code == 1 and why in out["error"], (argv, out)
    assert run_cli(capsys, "menage", "proposition")[1]["data"]["n"] == 0


def test_catalogue_signs_menage_lot_on_the_fingerprint():
    a = catalogue.PAR_ID["menage-lot"]
    _, args, stdin = catalogue.valider("menage-lot", {"empreinte": "0123456789abcdef"})
    assert a.signe == "menage-lot" and catalogue.cible(a, args, stdin) == "0123456789abcdef"
    assert catalogue.commande(a, args, stdin) == ["menage", "appliquer", "--empreinte=0123456789abcdef"]
    for bad in ("0123", "0123456789ABCDEF", "0123456789abcdef0"):
        with pytest.raises(catalogue.Refused):
            catalogue.valider("menage-lot", {"empreinte": bad})
    assert "menage-lot" in closing.DASHBOARD_ACTIONS and close_key.ACTIONS == closing.ACTIONS
    tok = closing.sign_dashboard(close_key.load_or_create(), "iphone", "menage-lot", "0123456789abcdef", int(time.time()))
    assert closing.check(tok, "menage-lot", "0123456789abcdef")[1] == "dashboard:iphone"
    with pytest.raises(closing.CloseRefused):
        closing.check(tok, "menage-lot", "fedcba9876543210")
    projected = catalogue.projeter(catalogue.MENAGE, apercu.LECTURES["menage-proposition"])
    assert projected["n"] == 3 and projected["cancel"][0]["raison"]


# --- todo #451: skills to the trash ---------------------------------------------------------------------------------

@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    root = tmp_path / "home" / ".claude" / "skills"
    for name in ("vieille", "autre"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text("x")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return root


def test_corbeille_proposed_seen_applied_by_move(hub, skills_home, capsys, monkeypatch):
    data = propose(capsys, "--cancel", "2:raison", "--corbeille", "vieille:aucun usage", "--corbeille", "autre:doublon")
    assert data["n"] == 3 and [c["nom"] for c in data["corbeille"]] == ["vieille", "autre"] and data["bloquee"] is None
    assert data["corbeille"][0] == {"nom": "vieille", "raison": "aucun usage", "refus": None}
    emp = data["empreinte"]
    plain = proposition.empreinte([{"id": 2, "titre": "Deux", "raison": "raison"}], [])
    assert emp != plain  # corbeille lines come after the others
    assert proposition.empreinte([{"id": 2, "titre": "Deux", "raison": "raison"}], [], []) == plain
    code, out = apply(capsys, monkeypatch, emp)
    assert code == 0 and out["data"]["faits"] == ["#2", "skill vieille", "skill autre"]
    assert not (skills_home / "vieille").exists() and not (skills_home / "autre").exists()
    moved = sorted(p.name for p in (skills_home / ".trash").iterdir())
    assert len(moved) == 2 and moved[0].startswith("autre-") and moved[1].startswith("vieille-")
    assert all((skills_home / ".trash" / m / "SKILL.md").is_file() for m in moved)  # moved, not deleted
    assert run_cli(capsys, "menage", "proposition")[1]["data"]["n"] == 0


def test_corbeille_refusals_at_proposal(hub, skills_home, capsys):
    for arg, why in (("absente:r", "introuvable"), ("../x:r", "refusé"), ("a/b:r", "refusé"), ("vieille:", "NOM:raison"),
                     ("vieille:" + "x" * 201, "NOM:raison"), ("vieille", "NOM:raison"), (".trash:r", "refusé")):
        code, out = run_cli(capsys, "menage", "proposer", "--corbeille", arg)
        assert code == 1 and why in out["error"], (arg, out)
    code, out = run_cli(capsys, "menage", "proposer", "--corbeille", "vieille:a", "--corbeille", "vieille:b")
    assert code == 1 and "deux fois" in out["error"]
    assert run_cli(capsys, "menage", "proposition")[1]["data"]["n"] == 0


def test_corbeille_skill_gone_before_apply_is_refused_whole(hub, skills_home, capsys, monkeypatch):
    emp = propose(capsys, "--cancel", "2:raison", "--corbeille", "vieille:r")["empreinte"]
    (skills_home / "vieille" / "SKILL.md").unlink()
    view = run_cli(capsys, "menage", "proposition")[1]["data"]
    assert view["bloquee"] == "skill vieille introuvable"
    code, out = apply(capsys, monkeypatch, emp)
    assert code == 1 and "Rien n'a été appliqué" in out["error"] and status(capsys, 2) == "todo"


# --- todo #474: folders flagged by tidy-up check 10, moved to var/.trash -------------------------------------------

REAL_CONFIG = __import__("pathlib").Path(__file__).resolve().parents[1] / "config" / "menage.yaml"


@pytest.fixture
def aged(hub, tmp_path, monkeypatch):
    """The real config/menage.yaml, with ~ = a throwaway home: var/missions (and its work/) and var/runs exist."""
    from ultron_hub.menage import rules
    (hub / "config" / "menage.yaml").write_text(REAL_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    home = tmp_path / "vhome"
    monkeypatch.setattr(rules, "user_home", lambda: str(home))
    var = home / "workspace" / "projects" / "active" / "ultron-hub" / "var"
    (var / "missions" / "m1" / "work" / "venv").mkdir(parents=True)
    (var / "missions" / "m1" / "work" / "venv" / "x").write_text("x")
    (var / "missions" / "m1" / "result.json").write_text("{}")
    (var / "runs" / "r1").mkdir(parents=True)
    (var / "runs" / "r1" / "out.txt").write_text("o")
    return var


def test_dossier_proposed_in_fingerprint_and_moved_to_var_trash(aged, hub, capsys, monkeypatch):
    work, run = str(aged / "missions" / "m1" / "work"), str(aged / "runs" / "r1")
    data = propose(capsys, "--cancel", "2:raison", "--dossier", f"{work}:work/ d'une vieille mission", "--dossier", f"{run}:vieux job")
    assert data["n"] == 3 and data["bloquee"] is None and [d["chemin"] for d in data["dossiers"]] == [work, run]
    assert data["dossiers"][0]["raison"] == "work/ d'une vieille mission" and data["dossiers"][0]["refus"] is None
    plain = proposition.empreinte([{"id": 2, "titre": "Deux", "raison": "raison"}], [], [])
    assert data["empreinte"] != plain  # the folders are in the fingerprint
    assert catalogue.projeter(catalogue.MENAGE, data)["dossiers"][0] == {"chemin": work, "raison": data["dossiers"][0]["raison"],
                                                                         "refus": None}
    code, out = apply(capsys, monkeypatch, data["empreinte"])
    assert code == 0 and out["data"]["faits"] == ["#2", f"dossier {work}", f"dossier {run}"], out
    assert not (aged / "missions" / "m1" / "work").exists() and (aged / "missions" / "m1" / "result.json").is_file()
    assert not (aged / "runs" / "r1").exists()
    trash = hub / "var" / ".trash"
    moved = sorted(p.name for p in trash.iterdir())
    assert moved[0].startswith("missions-m1-work-") and moved[1].startswith("var-runs-r1-")  # moved, not deleted
    assert (trash / moved[0] / "venv" / "x").is_file() and (trash / moved[1] / "out.txt").is_file()
    assert run_cli(capsys, "menage", "proposition")[1]["data"]["n"] == 0


def test_dossier_refusals_at_proposal(aged, hub, tmp_path, capsys):
    (aged / "runs" / ".cache").mkdir()
    (aged / "runs" / "fichier").write_text("f")
    (tmp_path / "ailleurs" / "work").mkdir(parents=True)
    (aged / "runs" / "lien").symlink_to(tmp_path / "ailleurs")  # the entry itself is a symlink
    (aged / "missions" / "m2").symlink_to(tmp_path / "ailleurs")  # a symlink on the path to <entry>/work
    (tmp_path / "outside" / "x").mkdir(parents=True)
    run = aged / "runs" / "r1"
    for arg, why in ((f"runs/r1:r", "absolu"), (f"{aged}/runs/r1/../r1:r", "absolu"), (f"{run}/:r", "absolu"),
                     (f"{run}/.:r", "absolu"), (f"{tmp_path}/outside/x:r", "hors des dossiers"),
                     (f"{aged}/runs:r", "hors des dossiers"), (f"{aged}/missions/m1/result.json:r", "hors des dossiers"),
                     (f"{hub}:r", "racine du projet"), ("/:r", "racine du projet"), (f"{aged}/runs/.cache:r", "caché"),
                     (f"{aged}/runs/lien:r", "lien symbolique"), (f"{aged}/missions/m2/work:r", "lien symbolique"),
                     (f"{aged}/runs/fichier:r", "pas un dossier"), (f"{aged}/runs/absent:r", "introuvable"),
                     (f"{run}:", "CHEMIN:raison"), (str(run), "CHEMIN:raison"), (f"{run}:" + "x" * 201, "CHEMIN:raison")):
        code, out = run_cli(capsys, "menage", "proposer", "--dossier", arg)
        assert code == 1 and why in out["error"], (arg, out)
    code, out = run_cli(capsys, "menage", "proposer", "--dossier", f"{run}:a", "--dossier", f"{run}:b")
    assert code == 1 and "deux fois" in out["error"]
    m1 = aged / "missions" / "m1"
    code, out = run_cli(capsys, "menage", "proposer", "--dossier", f"{m1}:a", "--dossier", f"{m1}/work:b")
    assert code == 1 and "dans un autre dossier" in out["error"]
    assert run_cli(capsys, "menage", "proposition")[1]["data"]["n"] == 0 and run.is_dir()


def test_dossier_without_menage_config_is_refused(hub, tmp_path, capsys):
    (tmp_path / "d" / "e").mkdir(parents=True)
    code, out = run_cli(capsys, "menage", "proposer", "--dossier", f"{tmp_path}/d/e:r")
    assert code == 1 and "configuration du ménage illisible" in out["error"]


@pytest.mark.parametrize("change", ["gone", "modified", "replaced", "symlink"])
def test_dossier_gone_or_changed_before_apply_is_refused_whole(aged, hub, tmp_path, capsys, monkeypatch, change):
    run = aged / "runs" / "r1"
    emp = propose(capsys, "--cancel", "2:raison", "--dossier", f"{run}:vieux job")["empreinte"]
    import shutil
    if change == "gone":
        shutil.rmtree(run)
    elif change == "modified":
        time.sleep(0.01)
        (run / "nouveau").write_text("n")
    elif change == "replaced":
        shutil.rmtree(run)
        run.mkdir()
    else:
        shutil.rmtree(run)
        (tmp_path / "cible").mkdir()
        run.symlink_to(tmp_path / "cible")
    view = run_cli(capsys, "menage", "proposition")[1]["data"]
    assert view["bloquee"] and view["dossiers"][0]["refus"] == view["bloquee"]
    assert {"gone": "introuvable", "modified": "a changé", "replaced": "a changé", "symlink": "lien symbolique"}[change] in view["bloquee"]
    code, out = apply(capsys, monkeypatch, emp)
    assert code == 1 and "Rien n'a été appliqué" in out["error"] and status(capsys, 2) == "todo"
    assert not (hub / "var" / ".trash").exists()


def test_dossier_rechecked_at_the_move_itself(aged, hub, capsys):
    """The step checks again under the lock: an identity that no longer matches moves nothing."""
    from ultron_hub.common.errors import InvalidRequestError
    from ultron_hub.common.settings import load_settings
    run = aged / "runs" / "r1"
    with pytest.raises(InvalidRequestError, match="a changé"):
        proposition._dossier(load_settings(), {"chemin": str(run), "raison": "r", "ident": "0:0:0"}, {"by": "t"})
    assert run.is_dir() and not (hub / "var" / ".trash").exists()


# -- todo #535: applied alone (auto:bilan) when nothing is really deleted, listed by `bilan soir` -------------------------

def test_auto_applies_bounded_proposal_by_auto_bilan_and_bilan_lists_it(hub, capsys, monkeypatch, prompts_dir):
    from ultron_hub.cap import bilan_soir
    monkeypatch.delenv("ULTRON_HUB_JOB", raising=False)
    monkeypatch.delenv("ULTRON_ESCOUADE_DIR", raising=False)
    propose(capsys, "--cancel", "1:remplacée", "--archiver", "autre")
    code, out = run_cli(capsys, "menage", "appliquer-auto")  # no token at all
    assert code == 0 and out["data"]["statut"] == "applique" and out["data"]["faits"] == ["#1", "chantier autre"]
    assert status(capsys, 1) == "cancelled" and not (prompts_dir / "autre.md").exists()
    from ultron_hub.common.settings import load_settings
    out = bilan_soir.run(load_settings())
    sortes = sorted(((x["sorte"], x["todo_id"]) for x in out["lignes"]), key=str)
    assert sortes == [("ménage", 1), ("ménage", None)] and out["clotures"] == 0
    assert out["lignes"][-1]["titre"].endswith("chantier autre")
    code, out = run_cli(capsys, "menage", "appliquer-auto")
    assert out["data"]["statut"] == "rien"


def test_auto_refused_from_a_job_or_with_a_real_deletion_nothing_changes(hub, capsys, monkeypatch):
    monkeypatch.delenv("ULTRON_ESCOUADE_DIR", raising=False)
    data = propose(capsys, "--cancel", "2:plus utile")
    monkeypatch.setenv("ULTRON_HUB_JOB", "x")
    code, out = run_cli(capsys, "menage", "appliquer-auto")
    assert code == 1 and "job" in out["error"] and status(capsys, 2) == "todo"
    monkeypatch.delenv("ULTRON_HUB_JOB")
    from ultron_hub.common.settings import load_settings
    settings = load_settings()
    raw = json.loads(proposition.path(settings).read_text())
    raw["supprimer"] = [{"chemin": "/x", "raison": "y"}]  # a real deletion: not a bounded item
    proposition.path(settings).write_text(json.dumps(raw))
    code, out = run_cli(capsys, "menage", "appliquer-auto")
    assert code == 1 and "supprimer" in out["error"] and status(capsys, 2) == "todo" and proposition.path(settings).exists()
    del raw["supprimer"]
    proposition.path(settings).write_text(json.dumps(raw))
    assert run_cli(capsys, "menage", "proposition")[1]["data"]["empreinte"] == data["empreinte"]


def test_auto_refuses_when_fingerprint_changes_between_read_and_apply(hub, capsys, monkeypatch):
    monkeypatch.delenv("ULTRON_HUB_JOB", raising=False)
    monkeypatch.delenv("ULTRON_ESCOUADE_DIR", raising=False)
    from ultron_hub.common.settings import load_settings
    settings = load_settings()
    propose(capsys, "--cancel", "2:plus utile")
    real = proposition.suppression_reelle

    def swap(s):  # the proposal is replaced right after the first read
        if not getattr(swap, "done", False):
            swap.done = True
            propose(capsys, "--cancel", "3:autre")
        return real(s)

    monkeypatch.setattr(proposition, "suppression_reelle", swap)
    out = proposition.appliquer_auto(settings)
    assert out["statut"] == "refuse" and "changé" in out["raison"]
    assert status(capsys, 2) == status(capsys, 3) == "todo"


def test_rapports_auto_applies_menage_part_and_refuses_in_a_job(hub, capsys, monkeypatch):
    from ultron_hub import rapports
    from ultron_hub.common.settings import load_settings
    settings = load_settings()
    propose(capsys, "--cancel", "2:plus utile")
    monkeypatch.setenv("ULTRON_HUB_JOB", "x")
    assert rapports.appliquer_auto(settings)["statut"] == "refuse" and status(capsys, 2) == "todo"
    monkeypatch.delenv("ULTRON_HUB_JOB")
    monkeypatch.delenv("ULTRON_ESCOUADE_DIR", raising=False)
    out = rapports.appliquer_auto(settings)
    assert out["statut"] == "applique" and out["bilan"][0]["ok"] and status(capsys, 2) == "cancelled"
    rec = json.loads((settings.var_dir / "rapports" / "appliques.jsonl").read_text().splitlines()[-1])
    assert rec["by"] == "auto:bilan"
    assert rapports.appliquer_auto(settings)["statut"] == "rien"
