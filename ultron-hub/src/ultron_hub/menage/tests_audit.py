"""Employé « tests » du ménage (todo #144) : surveille la suite de tests, sans IA et sans rien modifier.

Il lance la suite (même commande que `repos.tests`) avec `--durations` et `--junitxml` dans un dossier jetable, avec un
faux `hermes` sur le PATH (0 appel attendu : sinon un test parle au vrai Telegram, cf. todo #139), puis signale :
durée totale vs TESTS_TIMEOUT_S, part par fichier, nouveaux tests lents, `time.sleep` réels, noms en double.
Il propose, l'utilisateur décide : aucun test n'est touché. Historique : var/menage/tests-history.jsonl.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ultron_hub import repos
from ultron_hub.common.settings import Settings

DEFAULTS: dict[str, Any] = {"warn_pct": 80, "slow_test_s": 2.0, "top_files": 5, "max_listed": 5, "history_max_lines": 200}
REPO_NAME = "ultron-hub"
FAKE_HERMES = '#!/bin/sh\necho "$@" >> "$HERMES_FAKE_LOG"\nexit 0\n'

Runner = Callable[[list[str], dict[str, str], Path, float], "subprocess.CompletedProcess[str]"]


def load_thresholds(settings: Settings) -> dict[str, Any]:
    """`tests:` section of config/menage.yaml; missing or unreadable = defaults."""
    try:
        raw = yaml.safe_load((settings.root / "config" / "menage.yaml").read_text(encoding="utf-8"))
        section = raw.get("tests") if isinstance(raw, dict) else None
    except (OSError, yaml.YAMLError):
        section = None
    return {**DEFAULTS, **(section if isinstance(section, dict) else {})}


def build_command(settings: Settings, junit: Path) -> list[str]:
    """The `repos.tests` command of ultron-hub, plus --durations and --junitxml (fallback: this interpreter)."""
    cmd = repos.test_command(settings, REPO_NAME, settings.root) or [
        sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider"]
    if not Path(cmd[0]).exists() and "/" in cmd[0]:
        cmd[0] = sys.executable  # a worktree has no .venv of its own
    return [*cmd, "--durations=0", "--durations-min=0.5", f"--junitxml={junit}"]


def run_pytest(cmd: list[str], env: dict[str, str], cwd: Path, timeout_s: float) -> "subprocess.CompletedProcess[str]":
    """The only place where the real suite is started (the test suite replaces it: see tests/conftest.py)."""
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout_s)


def parse_junit(path: Path) -> list[dict[str, Any]]:
    """[{file, name, time, status}] from a junit xml; `file` = tests/test_x.py."""
    out = []
    for case in ET.parse(path).getroot().iter("testcase"):
        cls = case.get("classname", "")
        parts = cls.split(".")
        mod = next((i for i, p in enumerate(parts) if p.startswith("test_")), None)
        file = "/".join(parts[:mod + 1]) + ".py" if mod is not None else cls
        status = "failed" if case.find("failure") is not None or case.find("error") is not None else (
            "skipped" if case.find("skipped") is not None else "passed")
        out.append({"file": file, "name": ".".join(parts[mod + 1:] + [case.get("name", "")]),
                    "time": float(case.get("time") or 0), "status": status})
    return out


def per_file(cases: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for c in cases:
        totals[c["file"]] = totals.get(c["file"], 0.0) + c["time"]
    return dict(sorted(totals.items(), key=lambda kv: -kv[1]))


def scan_sources(tests_dir: Path) -> dict[str, Any]:
    """Static look at tests/: real `time.sleep(...)` calls and test names defined in several files."""
    sleeps: dict[str, int] = {}
    names: dict[str, set[str]] = {}
    for path in sorted(tests_dir.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        count = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Attribute) and n.func.attr == "sleep" and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "time") or (isinstance(n.func, ast.Name) and n.func.id == "sleep")))
        if count:
            sleeps[path.name] = count
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                names.setdefault(node.name, set()).add(path.name)
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, ast.FunctionDef) and sub.name.startswith("test"):
                        names.setdefault(f"{node.name}.{sub.name}", set()).add(path.name)
    dups = {n: sorted(f) for n, f in sorted(names.items()) if len(f) > 1}
    return {"sleeps": sleeps, "duplicates": dups}


def load_history(path: Path) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)]


def append_history(path: Path, entry: dict[str, Any], keep: int) -> None:
    rows = load_history(path) + [entry]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[-keep:]), encoding="utf-8")
    tmp.replace(path)


def analyse(settings: Settings, *, runner: Runner | None = None, now: float | None = None,
            timeout_s: float | None = None, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Run the suite once and measure. Never raises: a failure is reported in `error` / `ok`."""
    th = load_thresholds(settings)
    limit = float(timeout_s if timeout_s is not None else repos.TESTS_TIMEOUT_S)
    data: dict[str, Any] = {"ok": False, "error": None, "limit_s": limit, "thresholds": th}
    data.update(scan_sources(settings.root / "tests"))
    scratch = tempfile.TemporaryDirectory(prefix="menage-tests-")
    work = Path(scratch.name)
    try:
        fake = work / "bin" / "hermes"
        fake.parent.mkdir()
        fake.write_text(FAKE_HERMES, encoding="utf-8")
        fake.chmod(0o755)
        log = work / "hermes-calls.log"
        junit = work / "junit.xml"
        env = {**os.environ, "PATH": f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}",
               "HERMES_FAKE_LOG": str(log), "ULTRON_HUB_HERMES_BIN": str(fake)}
        started = clock()
        try:
            proc = (runner or run_pytest)(build_command(settings, junit), env, settings.root, limit)
            data["returncode"] = proc.returncode
            data["tail"] = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        except subprocess.TimeoutExpired:
            data["error"] = f"suite interrompue après {limit:.0f} s (délai max)"
        except OSError as exc:
            data["error"] = f"suite non lancée ({type(exc).__name__})"
        data["duration_s"] = round(clock() - started, 1)
        data["hermes_calls"] = len(log.read_text(encoding="utf-8").splitlines()) if log.is_file() else 0
        if data["error"] is None:
            try:
                cases = parse_junit(junit)
            except (OSError, ET.ParseError):
                cases = []
                data["error"] = "pas de junitxml lisible (suite plantée ?)"
            data["tests"] = len(cases)
            data["failed"] = sum(1 for c in cases if c["status"] == "failed")
            data["ok"] = data["error"] is None and data.get("returncode") == 0 and not data["failed"]
            files = per_file(cases)
            total = sum(files.values()) or 1.0
            data["files"] = [{"file": f, "s": round(s, 1), "pct": round(100 * s / total)}
                             for f, s in list(files.items())[:int(th["top_files"])]]
            data["slow"] = sorted(({"id": f"{c['file']}::{c['name']}", "s": round(c["time"], 1)} for c in cases
                                   if c["time"] > float(th["slow_test_s"])), key=lambda x: -x["s"])
    finally:
        scratch.cleanup()
    history_path = settings.var_dir / "menage" / "tests-history.jsonl"
    rows = load_history(history_path)
    previous = rows[-1] if rows else None
    known = {s.get("id") for s in (previous or {}).get("slow", [])}
    data["new_slow"] = [s for s in data.get("slow", []) if s["id"] not in known] if previous else []
    data["previous_duration_s"] = previous.get("duration_s") if previous else None
    data["warn"] = bool(data.get("duration_s", 0) >= limit * float(th["warn_pct"]) / 100 or data["error"]
                        or data.get("hermes_calls") or data.get("failed"))
    if data.get("tests"):  # a suite that did not run says nothing about the trend
        append_history(history_path, {"ts": now if now is not None else time.time(), "duration_s": data["duration_s"],
                                      "tests": data["tests"], "failed": data["failed"], "slow": data["slow"],
                                      "files": data["files"], "hermes_calls": data["hermes_calls"]},
                       int(th["history_max_lines"]))
    return data


def lines(data: dict[str, Any]) -> list[str]:
    """Short text (Telegram / section of the weekly report), about ten lines."""
    th = data["thresholds"]
    n = int(th["max_listed"])
    limit, dur = data["limit_s"], data.get("duration_s", 0)
    pct = round(100 * dur / limit) if limit else 0
    head = f"🧪 Tests : {dur:.0f} s sur {limit:.0f} s ({pct} %)"
    if data.get("tests"):
        head += f" · {data['tests']} tests"
    if data.get("previous_duration_s"):
        head += f" · avant {data['previous_duration_s']:.0f} s"
    out = [("⚠️ " if pct >= int(th["warn_pct"]) else "") + head]
    if data["error"]:
        out.append(f"⚠️ {data['error']}")
    elif not data["ok"]:
        out.append(f"⚠️ suite rouge : {data.get('failed', 0)} échec(s)")
    if data.get("files"):
        out.append("Part par fichier : " + ", ".join(f"{f['file'].removeprefix('tests/')} {f['pct']} %"
                                                      for f in data["files"][:3]))
    if data["new_slow"]:
        out.append(f"⚠️ {len(data['new_slow'])} nouveau(x) test(s) > {th['slow_test_s']} s : " + ", ".join(
            f"{s['id'].split('::')[-1]} ({s['s']} s)" for s in data["new_slow"][:n]))
    elif data.get("slow"):
        out.append(f"{len(data['slow'])} test(s) > {th['slow_test_s']} s (aucun nouveau)")
    if data["sleeps"]:
        out.append(f"⚠️ time.sleep réel : {sum(data['sleeps'].values())} appel(s) dans "
                   + ", ".join(sorted(data["sleeps"])[:n]))
    if data["duplicates"]:
        out.append(f"{len(data['duplicates'])} nom(s) de test en double entre fichiers, ex. "
                   + ", ".join(list(data["duplicates"])[:3]))
    if data.get("hermes_calls"):
        out.append(f"⚠️ fuite : {data['hermes_calls']} appel(s) au faux hermes (un test envoie un vrai message ?)")
    elif data["ok"] or data.get("tests"):
        out.append("✅ aucune fuite vers hermes")
    return out


def markdown(data: dict[str, Any]) -> str:
    out = ["## Tests", "", *[f"- {x}" for x in lines(data)], ""]
    if data.get("files"):
        out += ["Fichiers les plus longs :"] + [f"- `{f['file']}` : {f['s']} s ({f['pct']} %)" for f in data["files"]]
    if data.get("slow"):
        out += ["", "Tests lents :"] + [f"- `{s['id']}` : {s['s']} s" for s in data["slow"][:20]]
    if data["duplicates"]:
        out += ["", "Noms en double :"] + [f"- `{k}` : {', '.join(v)}" for k, v in list(data["duplicates"].items())[:20]]
    if data["sleeps"]:
        out += ["", "time.sleep dans tests/ :"] + [f"- `{k}` : {v}" for k, v in data["sleeps"].items()]
    return "\n".join(out).rstrip() + "\n"
