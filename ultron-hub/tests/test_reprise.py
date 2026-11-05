"""Archive old Reprise entries of the orchestrator prompt (todo #169): nothing lost, FOCUS and the 3 latest kept."""

import json

import pytest

from tests.test_esprit import mind, run  # noqa: F401 (mind = fixture)
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.esprit import reprise

NOW = 1_792_000_000.0  # October 2026

PROMPT = """# Orchestrateur

## 7. Rendre compte
- règle

## 8. Reprise (la plus récente en haut)
- **FOCUS DE LA SEMAINE : reflexion + fba.**
  suite du focus
- **18/10 03h30, passation.**
  - détail 5
- **18/10 01h, passation.**
  - détail 4
- **18/10 00h, passation.**
  - détail 3
- **17/10 23h, passation.**
  - détail 2
  - détail 2 bis
- **17/10 18h, passation.**
  - détail 1
"""


def test_keeps_focus_and_three_latest_and_moves_the_rest(tmp_path):
    prompt, archives = tmp_path / "orchestrateur.md", tmp_path / "archives"
    prompt.write_text(PROMPT)
    data = reprise.archive(prompt, archives, now=NOW)
    assert (data["gardees"], data["archivees"], data["ecrit"]) == (3, 2, True)
    text = prompt.read_text()
    assert "FOCUS" in text and "détail 3" in text and "détail 2" not in text and text.startswith("# Orchestrateur")
    archived = (archives / "reprises-2026-10.md").read_text()
    assert archived.index("17/10 23h") < archived.index("17/10 18h") and "détail 2 bis" in archived
    prompt.write_text(text.replace("- **18/10 03h30", "- **18/10 05h, passation.**\n  - détail 6\n- **18/10 03h30"))
    reprise.archive(prompt, archives, now=NOW)  # the next one goes on top of the same month file
    archived = (archives / "reprises-2026-10.md").read_text()
    assert archived.count("# Reprises archivées") == 1
    assert archived.index("18/10 00h") < archived.index("17/10 23h") < archived.index("17/10 18h")


def test_nothing_to_move_writes_nothing(tmp_path):
    prompt = tmp_path / "orchestrateur.md"
    prompt.write_text(PROMPT)
    data = reprise.archive(prompt, tmp_path / "archives", keep=10, now=NOW)
    assert data["archivees"] == 0 and not (tmp_path / "archives").exists() and prompt.read_text() == PROMPT


def test_dry_run_and_refusals(tmp_path):
    prompt = tmp_path / "orchestrateur.md"
    prompt.write_text(PROMPT)
    assert reprise.archive(prompt, tmp_path / "a", dry_run=True, now=NOW)["archivees"] == 2
    assert prompt.read_text() == PROMPT and not (tmp_path / "a").exists()
    with pytest.raises(InvalidRequestError):
        reprise.archive(prompt, tmp_path / "a", keep=0)
    (tmp_path / "lien.md").symlink_to(prompt)
    with pytest.raises(InvalidRequestError):
        reprise.archive(tmp_path / "lien.md", tmp_path / "a")
    prompt.write_text("# sans section\n")
    with pytest.raises(InvalidRequestError):
        reprise.archive(prompt, tmp_path / "a")


def test_cli(mind, tmp_path, capsys):
    prompt = tmp_path / "orchestrateur.md"
    prompt.write_text(PROMPT)
    code, out = run(capsys, "esprit", "reprise-archiver", "--fichier", str(prompt), "--archives", str(tmp_path / "a"),
                    "--essai")
    assert code == 0 and out["summary"] == "2 entrée(s) à archiver, 3 gardée(s) (essai, rien écrit)"
    code, out = run(capsys, "esprit", "reprise-archiver", "--fichier", str(prompt), "--archives", str(tmp_path / "a"))
    assert code == 0 and out["data"]["ecrit"] is True


HEADINGS = """# Reprise fba

## 8. Reprise
### 20/10 FBA
- état 5
### 19/10 FBA
- état 4
### 18/10 FBA
- état 3
### 17/10 FBA
- état 2
### 16/10 FBA
- état 1
"""


def test_split_accepts_both_entry_formats():
    _, _, bullets, _ = reprise.split(PROMPT)
    assert len(bullets) == 6
    _, _, headings, after = reprise.split(HEADINGS + "\n## 9. Suite\n- x\n")
    assert [e[0] for e in headings][:2] == ["### 20/10 FBA", "### 19/10 FBA"]
    assert len(headings) == 5 and after[0] == "## 9. Suite"
    assert len(reprise.split("## 8. Reprise\n- **a**\n### b\n- c\n- **d**\n")[2]) == 3


def test_archive_heading_entries(tmp_path):
    prompt, archives = tmp_path / "fba.md", tmp_path / "archives"
    prompt.write_text(HEADINGS)
    data = reprise.archive(prompt, archives, now=NOW, nom="reprises-fba")
    assert (data["gardees"], data["archivees"], data["ecrit"]) == (3, 2, True)
    assert "### 16/10 FBA" in (archives / "reprises-fba-2026-10.md").read_text()
    assert "### 16/10 FBA" not in prompt.read_text() and "### 18/10 FBA" in prompt.read_text()
