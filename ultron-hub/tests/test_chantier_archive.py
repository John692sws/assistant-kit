"""Todo #122: archive / reopen a chantier (CLI core + /chantiers screens). Throwaway folders only."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from cli_contract import check_argv
from ultron_hub.chantier import core
from ultron_hub.common.errors import InvalidRequestError


@pytest.fixture
def env(tmp_path, monkeypatch):
    prompts = tmp_path / "prompt"
    prompts.mkdir()
    (prompts / "alpha.md").write_text("# Chantier « Alpha »\n")
    (prompts / "beta.md").write_text("# Chantier « Beta »\n")
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, status TEXT, chantier TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?,?,?,?)", [(1, "ouverte", "todo", "beta"), (2, "faite", "done", "alpha")])
    conn.commit()
    conn.close()
    settings = SimpleNamespace(todo_db=db)
    src = core.sources(str(prompts), str(tmp_path / "boites"))
    return settings, src, prompts


def test_round_trip(env):
    settings, src, prompts = env
    core.archiver(settings, src, "alpha")
    assert not (prompts / "alpha.md").exists() and (prompts / "termines" / "alpha.md").read_text().startswith("# Chantier")
    assert core.tableau(settings, src)["archives"] == ["alpha"]
    assert [c["sujet"] for c in core.tableau(settings, src)["chantiers"]] == ["beta"]
    core.rouvrir(src, "alpha")
    assert (prompts / "alpha.md").is_file() and not (prompts / "termines" / "alpha.md").exists()


def test_refused_with_open_todos(env):
    settings, src, prompts = env
    with pytest.raises(InvalidRequestError, match="#1"):
        core.archiver(settings, src, "beta")
    assert (prompts / "beta.md").is_file()


def test_refused_when_todos_unreadable(env, tmp_path):
    settings, src, _ = env
    settings.todo_db = tmp_path / "nope.db"
    with pytest.raises(InvalidRequestError, match="illisibles"):
        core.archiver(settings, src, "alpha")


@pytest.mark.parametrize("name", ["", "../x", "A", "-a", "a/b", "a.b", "x" * 41, ".hidden", "_socle"])
def test_invalid_names(env, name):
    settings, src, _ = env
    with pytest.raises(InvalidRequestError):
        core.archiver(settings, src, name)
    with pytest.raises(InvalidRequestError):
        core.rouvrir(src, name)


def test_target_exists_and_nothing_overwritten(env):
    settings, src, prompts = env
    (prompts / "termines").mkdir()
    (prompts / "termines" / "alpha.md").write_text("ancien")
    with pytest.raises(InvalidRequestError, match="existe déjà"):
        core.archiver(settings, src, "alpha")
    assert (prompts / "termines" / "alpha.md").read_text() == "ancien" and (prompts / "alpha.md").is_file()
    with pytest.raises(InvalidRequestError, match="existe déjà"):
        (prompts / "termines" / "beta.md").write_text("b")
        core.rouvrir(src, "beta")


def test_symlinks_are_never_followed(env, tmp_path):
    settings, src, prompts = env
    outside = tmp_path / "dehors.md"
    outside.write_text("secret")
    (prompts / "lien.md").symlink_to(outside)
    with pytest.raises(InvalidRequestError, match="ordinaire"):
        core.archiver(settings, src, "lien")
    assert outside.read_text() == "secret"
    real = tmp_path / "ailleurs"
    real.mkdir()
    (prompts / "termines").symlink_to(real)
    with pytest.raises(InvalidRequestError, match="vrai dossier"):
        core.archiver(settings, src, "alpha")
    assert list(real.iterdir()) == []
    (real / "z.md").write_text("z")
    with pytest.raises(InvalidRequestError):
        core.rouvrir(src, "z")


def test_unknown_chantier(env):
    settings, src, _ = env
    with pytest.raises(InvalidRequestError, match="inconnu"):
        core.archiver(settings, src, "zzz")
    with pytest.raises(InvalidRequestError, match="inconnu"):
        core.rouvrir(src, "zzz")
