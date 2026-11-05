"""`--help` must work on every command and sub-command: a stray `%` in a help string crashes argparse (todo #421)."""

import argparse

import pytest

from ultron_hub import cli


def _walk(parser, path=()):
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _walk(child, path + (name,))


PARSERS = list(_walk(cli.build_parser()))


@pytest.mark.parametrize("path,parser", PARSERS, ids=lambda v: " ".join(v) if isinstance(v, tuple) else "")
def test_help_renders_for_every_command(path, parser):
    assert "usage:" in parser.format_help()


@pytest.mark.parametrize("path", [p for p, _ in PARSERS], ids=lambda p: " ".join(p) or "root")
def test_help_flag_exits_cleanly(path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args([*path, "--help"])
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out
