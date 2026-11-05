"""Interactive prompt (todo #230): socle + mode + orchestrator + Reprise last, poser.sh on a fake HOME. Offline."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from tests.test_esprit import run
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.escouade import core
from ultron_hub.esprit import reprise

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SOCLE = (DEPLOY / "escouade/_socle.md").read_text()
MODE_ESC = (DEPLOY / "escouade/_mode-escouade.md").read_text()
MODE = (DEPLOY / "prompt/_mode-interactif.md").read_text()
ORCH = (DEPLOY / "prompt/orchestrateur-ultron.md").read_text()
REPRISE = "## Reprise (état du jour)\n- **FOCUS : test.**\n- **19/10 10h, passation.** DERNIERE_LIGNE\n"
SUJET = "# S\n\nProjet : ~/notes\n\n## 1. Lire d'abord\n- fiche du sujet\n\n## 4. Pistes\n- piste A\n"


def lines(text):
    return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


def pieces(tmp_path) -> Path:
    (tmp_path / "demarrage").mkdir(exist_ok=True)
    (tmp_path / "_socle.md").write_text(SOCLE)
    (tmp_path / "_mode-interactif.md").write_text(MODE)
    (tmp_path / "demarrage/orchestrateur-ultron.md").write_text(ORCH)
    (tmp_path / "reprise-orchestrateur.md").write_text(REPRISE)
    return tmp_path


def test_no_line_shared_between_socle_and_any_mode_or_orchestrator():
    for other in (MODE, ORCH, MODE_ESC):
        assert lines(SOCLE) & lines(other) == set()
    assert lines(MODE) & lines(ORCH) == set()


def test_every_marker_is_filled_and_reprise_comes_last(tmp_path):
    folder = pieces(tmp_path)
    (folder / "reprises").mkdir()
    (folder / "reprises/demo.md").write_text(REPRISE)  # todo #284: a chantier reads its own Reprise
    for sujet in (None, "demo"):
        (folder / "demo.md").write_text(SUJET)
        text = core.build_interactif(sujet, dossier=str(folder))
        assert not re.findall(r"\{(sujet|limite|fiche|drapeau|dossier|jeton)\}", text)
        assert text.rstrip().endswith("DERNIERE_LIGNE")
        assert text.count("## Reprise") == (1 if sujet is None else 2) and text.index("## Reprise") > text.index("## 7.")
        # hard rules first, then the way of working, mission and order of work, reporting and handover, Reprise
        order = [text.index(h) for h in ("## 2.", "## 5.", "## 3.", "## 8.", "## 1.", "## 4.", "## 6.", "## 7.", "## Reprise")]
        assert order == sorted(order)
        assert ("piste A" in text) == (sujet == "demo")
    assert "~/notes/chantier-demo.md" in text


def test_missing_piece_or_section_is_refused(tmp_path):
    folder = pieces(tmp_path)
    (folder / "reprise-orchestrateur.md").unlink()
    with pytest.raises(InvalidRequestError, match="introuvable"):
        core.build_interactif(dossier=str(folder))
    with pytest.raises(InvalidRequestError, match="incomplet"):
        core.assemble_interactif(SOCLE, "## 2. x\n", ORCH, REPRISE)


def test_reprise_file_is_not_a_subject(tmp_path):
    folder = pieces(tmp_path)
    (folder / "demo.md").write_text(SUJET)
    assert core.list_subjects(folder) == ["demo"]
    (folder / "_mode-escouade.md").write_text(MODE_ESC)
    assert core.incomplets(folder) == {}
    with pytest.raises(InvalidRequestError):
        core.check_name("reprise-orchestrateur")


def test_cli_writes_the_prompt_file(tmp_path, capsys):
    (tmp_path / "p").mkdir()
    folder = pieces(tmp_path / "p")
    out = tmp_path / "var" / "prompts" / "interactif.md"
    code, data = run(capsys, "prompt", "interactif", "--dossier", str(folder), "--sortie", str(out))
    assert code == 0 and data["command"] == "prompt.interactif" and data["data"]["fichier"] == str(out)
    assert out.read_text().rstrip().endswith("DERNIERE_LIGNE") and stat.S_IMODE(out.stat().st_mode) == 0o600
    code, data = run(capsys, "prompt", "interactif", "--dossier", str(tmp_path / "vide"), "--sortie", str(out))
    assert code == 1 and "introuvable" in data["error"]


def test_reprise_archiver_reads_the_new_file_else_the_old_one(tmp_path, monkeypatch):
    new, old = tmp_path / "reprise-orchestrateur.md", tmp_path / "orchestrateur-ultron.md"
    monkeypatch.setattr(reprise, "REPRISE", new)
    monkeypatch.setattr(reprise, "PROMPT", old)
    assert reprise.current() == old
    new.write_text("# R\n\n## Reprise (état)\n" + "".join(f"- **{i}h.**\n  détail {i}\n" for i in range(6)))
    assert reprise.current() == new
    data = reprise.archive(new, tmp_path / "arch", now=1_792_000_000.0)
    assert data["gardees"] == 3 and data["archivees"] == 3 and data["ecrit"]
    assert new.read_text().count("- **") == 3


# --- poser.sh on a fake HOME -------------------------------------------------------------------------------------

ULTRON = """#!/bin/sh
set -e
base="ultron-x"
msg="Lis ~/notes/prompt/demarrage/orchestrateur-ultron.md et applique-le."
[ -n "$1" ] && msg="$msg Consigne du jour : $*"
echo "$msg"
"""
OLD_ORCH = "# Ancien\n\n## 7. Rendre compte\n- règle\n\n## 8. Reprise (la plus récente en haut)\n- **FOCUS : f.**\n- **18/10, passation.**\n  - détail\n"


def poser(home, *args):
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    return subprocess.run(["bash", str(DEPLOY / "prompt/poser.sh"), *args], env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def fake_home(tmp_path):
    home = tmp_path / "home"
    (home / "notes/prompt/demarrage").mkdir(parents=True)
    (home / ".local/bin").mkdir(parents=True)
    (home / "notes/prompt/demarrage/orchestrateur-ultron.md").write_text(OLD_ORCH)
    (home / "notes/prompt/_socle.md").write_text("ancien socle\n")
    bin_ = home / ".local/bin/ultron"
    bin_.write_text(ULTRON)
    bin_.chmod(0o755)
    return home


def test_poser_lays_the_files_and_is_idempotent(fake_home):
    prompts, bin_ = fake_home / "notes/prompt", fake_home / ".local/bin/ultron"
    done = poser(fake_home, "--essai")
    assert done.returncode == 0 and (prompts / "_socle.md").read_text() == "ancien socle\n" and "reprise" not in os.listdir(prompts)
    assert bin_.read_text() == ULTRON
    first = poser(fake_home)
    assert first.returncode == 0, first.stderr
    assert (prompts / "_socle.md").read_text() == SOCLE and (prompts / "_socle.md.avant-230").read_text() == "ancien socle\n"
    assert (prompts / "_mode-escouade.md").read_text() == MODE_ESC and (prompts / "_mode-interactif.md").read_text() == MODE
    assert (prompts / "demarrage/orchestrateur-ultron.md").read_text() == ORCH
    assert (prompts / "demarrage/orchestrateur-ultron.md.avant-230").read_text() == OLD_ORCH
    # the Reprise left the old orchestrator without loss, entries intact, and stays the day's own file afterwards
    got = (prompts / "reprise-orchestrateur.md").read_text()
    assert got.startswith("## Reprise") and "- **FOCUS : f.**\n- **18/10, passation.**\n  - détail\n" in got
    assert "ancien" not in got and "## 8." not in got
    assert bin_.read_text() != ULTRON and "prompt interactif" in bin_.read_text() and bin_.stat().st_mode & 0o111
    assert bin_.with_name("ultron.avant-230").read_text() == ULTRON
    # prompt = what the real assembler reads back
    assert "DERNIERE_LIGNE" not in got
    snapshot = {p: p.read_bytes() for p in fake_home.rglob("*") if p.is_file()}
    (prompts / "reprise-orchestrateur.md").write_text(got + "- **19/10, nouvelle.**\n")
    again = poser(fake_home)
    assert again.returncode == 0 and "déjà à jour" in again.stdout
    after = {p: p.read_bytes() for p in fake_home.rglob("*") if p.is_file()}
    snapshot[prompts / "reprise-orchestrateur.md"] = (got + "- **19/10, nouvelle.**\n").encode()
    assert after == snapshot  # nothing else changed, backups kept, the Reprise not overwritten
    # the assembled prompt works from the posed files
    text = core.build_interactif(dossier=str(prompts))
    assert text.rstrip().endswith("- **19/10, nouvelle.**")


def test_posed_ultron_command_sends_the_new_message_or_falls_back(fake_home, tmp_path):
    poser(fake_home)
    hub = tmp_path / "hub"
    (hub / ".venv/bin").mkdir(parents=True)
    env = {"HOME": str(fake_home), "PATH": os.environ["PATH"], "ULTRON_HUB_DIR": str(hub)}
    bad = subprocess.run([str(fake_home / ".local/bin/ultron")], env=env, capture_output=True, text=True)
    assert "orchestrateur-ultron.md" in bad.stdout and "impossible" in bad.stderr  # no ultron-hub: old prompt
    fake = hub / ".venv/bin/ultron-hub"
    fake.write_text("#!/bin/sh\n[ \"$1 $2\" = \"prompt interactif\" ] || exit 1\n")
    fake.chmod(0o755)
    good = subprocess.run([str(fake_home / ".local/bin/ultron"), "focus X"], env=env, capture_output=True, text=True)
    assert good.stdout.startswith(f"Lis {hub}/var/prompts/interactif.md et applique-le.") and "Consigne du jour : focus X" in good.stdout


def test_poser_refuses_a_command_it_does_not_recognise(fake_home):
    (fake_home / ".local/bin/ultron").write_text("#!/bin/sh\necho autre\n")
    done = poser(fake_home)
    assert done.returncode != 0 and "rien modifié" in done.stderr
    assert (fake_home / ".local/bin/ultron").read_text() == "#!/bin/sh\necho autre\n"


# --- core mode (todo #358) ---
NOYAU = "## Noyau de session\nRole. fiche `{fiche}` NOYAU_LIGNE\n"
REPRISE_2 = ("## Reprise (état)\n- **FOCUS : test.**\n- **20/10 10h, passation.** RECENTE\n  - suite\n"
             "- **19/10 10h, passation.** ANCIENNE\n")


def test_noyau_mode_chosen_only_when_noyau_exists(tmp_path):
    folder = pieces(tmp_path)
    legacy = core.build_interactif(dossier=str(folder))
    assert "NOYAU_LIGNE" not in legacy and "## 4. Ordre du travail" in legacy
    (folder / "_noyau.md").write_text(NOYAU)
    text = core.build_interactif(dossier=str(folder))
    assert "NOYAU_LIGNE" in text and "## 4. Ordre du travail" not in text and "## 1. Mission" in text
    assert "{fiche}" not in text and "la fiche du chantier concerné" in text


def test_noyau_mode_keeps_only_latest_reprise_entry(tmp_path):
    folder = pieces(tmp_path)
    (folder / "_noyau.md").write_text(NOYAU)
    (folder / "reprise-orchestrateur.md").write_text(REPRISE_2)
    text = core.build_interactif(dossier=str(folder))
    assert "RECENTE" in text and "FOCUS : test" in text and "ANCIENNE" not in text and "  - suite" in text
    assert core.derniere_entree("## R\nAucune passation encore.\n") == "## R\nAucune passation encore.\n"


def test_noyau_mode_chantier_has_fiche_and_own_reprise(tmp_path):
    folder = pieces(tmp_path)
    (folder / "_noyau.md").write_text(NOYAU)
    (folder / "demo.md").write_text(SUJET)
    text = core.build_interactif("demo", dossier=str(folder))
    assert "~/notes/chantier-demo.md" in text and "piste A" in text and "Reprise du chantier demo" in text
    assert "## 4. Ordre du travail" not in text


def test_noyau_over_limit_refused(tmp_path):
    folder = pieces(tmp_path)
    (folder / "_noyau.md").write_text("x" * 5001)
    with pytest.raises(InvalidRequestError, match="noyau trop gros"):
        core.build_interactif(dossier=str(folder))


def test_mesure_flags_big_fiche(tmp_path, capsys):
    folder = pieces(tmp_path)
    (folder / "_noyau.md").write_text(NOYAU)
    (folder / "fiches").mkdir()
    (folder / "fiches/ok.md").write_text("petite")
    code, data = run(capsys, "prompt", "mesure", "--dossier", str(folder))
    assert code == 0 and data["data"]["ok"] and data["data"]["fiches"] == {"ok.md": 6}
    assert data["data"]["octets"]["mode"] == "noyau" and data["data"]["octets"]["total"] > data["data"]["octets"]["noyau"]
    (folder / "fiches/gros.md").write_text("y" * 4001)
    code, data = run(capsys, "prompt", "mesure", "--dossier", str(folder))
    assert code != 0 and data["data"]["ok"] is False and "gros.md" in data["data"]["raison"]


NOYAU_RENVOIS = """# Noyau
Voir fiches/a.md et fiches/b.md.
## Skills
- `sous-agent` avant un lancement ; `annonce-branche` quand prête. Un `send` puis avec `grep` ici.
## Autre
- `pas-une-skill` ailleurs
"""


def renvois_setup(tmp_path, skills=("sous-agent", "annonce-branche"), fiches=("a.md", "b.md")):
    folder = pieces(tmp_path)
    (folder / "_noyau.md").write_text(NOYAU_RENVOIS)
    (folder / "fiches").mkdir()
    for f in fiches:
        (folder / "fiches" / f).write_text("x")
    sdir = tmp_path / "skills"
    for k in skills:
        (sdir / k).mkdir(parents=True)
        (sdir / k / "SKILL.md").write_text("x")
    sdir.mkdir(exist_ok=True)
    return folder, sdir


def test_mesure_renvois_present_ok(tmp_path):
    folder, sdir = renvois_setup(tmp_path)
    data = core.mesure(dossier=str(folder), skills_dir=sdir)
    assert data["ok"] and data["renvois"] == {"fiches": ["fiches/a.md", "fiches/b.md"], "skills": ["annonce-branche", "sous-agent"]}


def test_mesure_renvois_missing_fiche(tmp_path):
    folder, sdir = renvois_setup(tmp_path, fiches=("a.md",))
    data = core.mesure(dossier=str(folder), skills_dir=sdir)
    assert not data["ok"] and "fiche citée absente : fiches/b.md" in data["raison"]


def test_mesure_renvois_missing_skill(tmp_path):
    folder, sdir = renvois_setup(tmp_path, skills=("sous-agent",))
    data = core.mesure(dossier=str(folder), skills_dir=sdir)
    assert not data["ok"] and "skill citée absente : annonce-branche" in data["raison"]
    assert "sous-agent" not in data["raison"]


def test_mesure_renvois_send_grep_not_skills(tmp_path):
    folder, sdir = renvois_setup(tmp_path)
    skills = core.mesure(dossier=str(folder), skills_dir=sdir)["renvois"]["skills"]
    assert "send" not in skills and "grep" not in skills and "pas-une-skill" not in skills
