from pathlib import Path

import pytest

from conftest import write_task
from ultron_hub.common.errors import (
    InvalidRequestError,
    TaskConfigError,
    TaskNotFoundError,
    TemplateError,
)
from ultron_hub.core.registry import Registry, render_template



def test_default_model_is_sonnet(tmp_path):
    write_task(tmp_path, "nomodel", "name: nomodel\ntimeout_s: 5\n")
    spec = Registry(tmp_path).load("nomodel")
    assert spec.model == "sonnet"


def test_default_timeout(tmp_path):
    write_task(tmp_path, "t", "name: t\n")
    assert Registry(tmp_path, default_timeout_s=42).load("t").timeout_s == 42


@pytest.mark.parametrize("model", ["gpt-4", "claude-3", "Sonnet", "default"])
def test_invalid_model_rejected(tmp_path, model):
    write_task(tmp_path, "bad", f"name: bad\nmodel: {model}\n")
    with pytest.raises(TaskConfigError, match="invalid model"):
        Registry(tmp_path).load("bad")


def test_unknown_action(tmp_path):
    with pytest.raises(TaskNotFoundError):
        Registry(tmp_path).load("nope")


@pytest.mark.parametrize("action", ["../etc", "Ping", "a/b", "", ".hidden"])
def test_invalid_action_name(tmp_path, action):
    with pytest.raises(InvalidRequestError):
        Registry(tmp_path).load(action)


def test_unknown_key_rejected(tmp_path):
    write_task(tmp_path, "t", "name: t\nmodle: haiku\n")
    with pytest.raises(TaskConfigError, match="unknown keys"):
        Registry(tmp_path).load("t")


def test_name_must_match_directory(tmp_path):
    write_task(tmp_path, "t", "name: other\n")
    with pytest.raises(TaskConfigError):
        Registry(tmp_path).load("t")


@pytest.mark.parametrize("bad_dir", ["relative/dir", "/", str(Path.home()), str(Path.home() / ".ssh"),
                                     str(Path.home() / ".hermes" / "x")])
def test_dangerous_add_dirs_rejected(tmp_path, bad_dir):
    write_task(tmp_path, "t", f"name: t\nadd_dirs: [{bad_dir!r}]\n")
    with pytest.raises(TaskConfigError):
        Registry(tmp_path).load("t")


def test_missing_param(tmp_path):
    write_task(tmp_path, "t", "name: t\n", prompt="Hello {name}, check {path}")
    spec = Registry(tmp_path).load("t")
    with pytest.raises(TemplateError, match="name, path"):
        spec.render_prompt({})
    assert spec.render_prompt({"name": "Ultron", "path": "/srv"}) == "Hello Ultron, check /srv"


def test_template_escaping():
    # Values are inserted verbatim (no re-expansion), {{ }} are literal braces,
    # and unrelated braces such as JSON examples are left untouched.
    out = render_template('{{x}} {a} {"k": 1}', {"a": "{b}"})
    assert out == '{x} {b} {"k": 1}'
    assert render_template("n={n} l={l}", {"n": 3, "l": [1, "é"]}) == 'n=3 l=[1, "é"]'
