"""Exact inventory of the Ultron system (todo #452): no AI, read only, best effort.

Every element: nom, type, chemin, taille (bytes), modifie (ISO), dernier_usage (ISO or None), source_usage.
Only os.scandir/os.lstat on folders; file CONTENT is read only for the usage sources (logs, journal, request.json of
runs, Claude Code transcripts). Secret paths (menage.yaml `secrets`/`exclusions` + ~/forum, ~/.ssh, ~/.hermes,
~/.config/gh, ~/.sudo_pass, auth.json, .env, .credentials.json) are never opened, stat'ed nor listed.
An unreadable source becomes a line of `warnings`, never an exception.
"""

import argparse
import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

from ultron_hub.common.settings import Settings
from ultron_hub.menage.audit import _is_protected, _protected_patterns

EXTRA_PROTECTED = ("~/forum", "~/.ssh", "~/.hermes", "~/.config/gh", "~/.sudo_pass",
                   "*/auth.json", "*/.env", "*/.credentials.json")
WALK_MAX = 500_000          # entries counted per folder before giving up (a warning)
SYSTEMCTL_TIMEOUT_S = 20
USAGE_DAYS = 30
LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})")
LOG_CMD_TEXT = re.compile(r"command=([a-z0-9][a-z0-9 -]*?)(?::|$|\s{2})")
LOG_CMD_JSON = re.compile(r'"command":\s*"([a-z0-9_.-]+)')


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")


def _element(nom: str, kind: str, chemin: str, taille: int | None = None, modifie: str | None = None,
             dernier_usage: str | None = None, source_usage: str = "aucune mesure", **extra: Any) -> dict[str, Any]:
    return {"nom": nom, "type": kind, "chemin": chemin, "taille": taille, "modifie": modifie,
            "dernier_usage": dernier_usage, "source_usage": source_usage, **extra}


class Inventory:
    def __init__(self, settings: Settings, home: str, now: float, run: Callable[..., Any] | None = None):
        self.settings = settings
        self.home = home
        self.now = now
        self.run = run or subprocess.run  # looked up at call time (tests patch it)
        self.warnings: list[str] = []
        self.elements: list[dict[str, Any]] = []
        self.patterns = self._patterns()

    # --- secrets ---------------------------------------------------------------------------------------------------
    def _patterns(self) -> list[str]:
        text = ""
        try:
            text = (self.settings.root / "config" / "menage.yaml").read_text(encoding="utf-8")
        except OSError:
            self.warnings.append("menage.yaml illisible : seules les exclusions de base s'appliquent")
        patterns = _protected_patterns(text, self.home, self.warnings)
        for extra in EXTRA_PROTECTED:
            patterns.append(self.home + extra[1:] if extra.startswith("~") else extra)
        return patterns

    def protected(self, path: str) -> bool:
        return _is_protected(path, self.patterns, self.home)

    # --- folders ---------------------------------------------------------------------------------------------------
    def folder_size(self, path: str) -> int | None:
        """Sum of lstat sizes below `path`; links are not followed, protected entries are skipped unseen."""
        total, seen, stack = 0, 0, [path]
        try:
            while stack:
                current = stack.pop()
                with os.scandir(current) as it:
                    for entry in it:
                        if self.protected(entry.path):
                            continue
                        seen += 1
                        if seen > WALK_MAX:
                            self.warnings.append(f"{path} : plus de {WALK_MAX} entrées, taille partielle")
                            return total
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        total += st.st_size
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
        except OSError as exc:
            self.warnings.append(f"{path} : illisible ({type(exc).__name__})")
            return None
        return total

    def children(self, path: str) -> list[str]:
        """Sub-folders (not hidden, not protected) of `path`, sorted."""
        if self.protected(path):
            return []
        try:
            with os.scandir(path) as it:
                names = sorted(e.path for e in it if not e.name.startswith(".") and not self.protected(e.path)
                               and e.is_dir(follow_symlinks=False))
        except OSError as exc:
            self.warnings.append(f"{path} : illisible ({type(exc).__name__})")
            return []
        return names

    def folder(self, path: str, kind: str, **extra: Any) -> dict[str, Any]:
        try:
            modified = _iso(os.lstat(path).st_mtime)
        except OSError:
            modified = None
        return _element(os.path.basename(path), kind, path, self.folder_size(path), modified, **extra)

    def collect_folders(self, skills_usage: dict[str, tuple[str | None, int]]) -> None:
        h = self.home
        for p in self.children(f"{h}/workspace/projects/active"):
            self.elements.append(self.folder(p, "projet"))
        notes = f"{h}/notes"
        if os.path.isdir(notes) and not self.protected(notes):
            self.elements.append(self.folder(notes, "notes"))
            for p in self.children(notes):
                self.elements.append(self.folder(p, "notes-dossier"))
        for p in self.children(f"{h}/ultron"):
            self.elements.append(self.folder(p, "ultron"))
        skills = f"{h}/.claude/skills"
        for p in self.children(skills):
            name = os.path.basename(p)
            if name == "synced":
                continue
            last, count = skills_usage.get(name, (None, 0))
            self.elements.append(self.folder(p, "skill", dernier_usage=last, appels_30j=count,
                                             source_usage="transcripts ~/.claude/projects (tool_use Skill)"))
        for p in self.children(str(self.settings.var_dir)):
            self.elements.append(self.folder(p, "var"))

    # --- CLI commands ----------------------------------------------------------------------------------------------
    def cli_commands(self) -> list[tuple[str, str]]:
        """(name, path of the module defining the parser is unknown) -> (name, help) for levels 1 and 2."""
        from ultron_hub.cli import build_parser
        out: list[tuple[str, str]] = []

        def subs(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
            found: dict[str, argparse.ArgumentParser] = {}
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    found.update(action.choices)
            return found

        try:
            top = subs(build_parser())
        except Exception as exc:  # noqa: BLE001 - a broken parser is a warning, not a crash
            self.warnings.append(f"parser argparse illisible ({type(exc).__name__})")
            return out
        for name, parser in sorted(top.items()):
            out.append((name, parser.description or ""))
            for sub_name in sorted(subs(parser)):
                out.append((f"{name} {sub_name}", ""))
        return out

    def log_usage(self) -> dict[str, str]:
        """Last timestamp (ISO) per command name found in var/logs/ultron-hub.log*."""
        last: dict[str, str] = {}
        logs = self.settings.logs_dir
        try:
            files = sorted(p for p in logs.iterdir() if p.name.startswith(self.settings.log_file) and p.is_file())
        except OSError as exc:
            self.warnings.append(f"logs illisibles ({type(exc).__name__})")
            return last
        for f in files:
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if "command" not in line:
                            continue
                        m = LOG_CMD_TEXT.search(line) or LOG_CMD_JSON.search(line)
                        ts = LOG_TS.match(line)
                        if not m or not ts:
                            continue
                        name = m.group(1).replace(".", " ").strip()
                        stamp = f"{ts.group(1)}T{ts.group(2)}"
                        if stamp > last.get(name, ""):
                            last[name] = stamp
            except OSError as exc:
                self.warnings.append(f"{f.name} illisible ({type(exc).__name__})")
        return last

    def journal_entries(self) -> list[dict]:
        from ultron_hub.esprit import store
        rows: list[dict] = []
        base = self.settings.esprit_dir
        if base is None:
            return rows
        paths = sorted((base / store.ARCHIVES).glob("journal-*.jsonl")) if (base / store.ARCHIVES).is_dir() else []
        paths.append(base / store.JOURNAL)
        for p in paths:
            try:
                with open(p, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict):
                            rows.append(row)
            except OSError as exc:
                self.warnings.append(f"journal {p.name} illisible ({type(exc).__name__})")
        return rows

    def collect_commands(self, journal: list[dict]) -> None:
        usage = self.log_usage()
        for name, helptext in self.cli_commands():
            logs = [v for k, v in usage.items() if k == name or k.startswith(name + " ")]
            needle = f"ultron-hub {name}"
            jr = [r.get("date") for r in journal if needle in str(r.get("texte", "")) and r.get("date")]
            candidates = [(v, "var/logs/ultron-hub.log") for v in logs] + [(d, "journal commun") for d in jr]
            last = max(candidates, key=lambda c: _sortable(c[0])) if candidates else None
            self.elements.append(_element(
                name, "commande", "ultron-hub " + name, dernier_usage=last[0] if last else None,
                source_usage=last[1] if last else "logs + journal : aucune mention", aide=helptext))

    # --- tasks -----------------------------------------------------------------------------------------------------
    def collect_tasks(self) -> None:
        last: dict[str, str] = {}
        runs = self.settings.runs_dir
        try:
            run_dirs = sorted((p for p in runs.iterdir() if p.is_dir()), reverse=True)
        except OSError as exc:
            self.warnings.append(f"var/runs illisible ({type(exc).__name__})")
            run_dirs = []
        for d in run_dirs:
            try:
                action = json.loads((d / "request.json").read_text(encoding="utf-8")).get("action")
            except (OSError, ValueError, AttributeError):
                continue
            m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", d.name)
            if isinstance(action, str) and m and action not in last:
                last[action] = "{}-{}-{}T{}:{}:{}Z".format(*m.groups())
        try:
            task_dirs = sorted(p for p in self.settings.tasks_dir.iterdir() if (p / "task.yaml").is_file())
        except OSError as exc:
            self.warnings.append(f"tasks/ illisible ({type(exc).__name__})")
            return
        for d in task_dirs:
            f = d / "task.yaml"
            model = None
            try:
                model = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("model")
                st = f.stat()
            except (OSError, yaml.YAMLError, AttributeError) as exc:
                self.warnings.append(f"{f} illisible ({type(exc).__name__})")
                continue
            self.elements.append(_element(
                d.name, "tache", str(f), st.st_size, _iso(st.st_mtime), last.get(d.name),
                "var/runs/*/request.json" if d.name in last else "aucun run", modele=model))

    # --- systemd ---------------------------------------------------------------------------------------------------
    def _systemctl(self, *args: str) -> list[dict]:
        cmd = ["systemctl", "--user", *args, "--output=json"]
        try:
            res = self.run(cmd, capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S, check=False)
            data = json.loads(res.stdout)
            if res.returncode != 0 or not isinstance(data, list):
                raise ValueError(f"rc={res.returncode}")
            return [d for d in data if isinstance(d, dict)]
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.warnings.append(f"systemctl {' '.join(args)} : {type(exc).__name__} {exc}"[:200])
            return []

    def collect_systemd(self) -> None:
        for t in self._systemctl("list-timers", "--all"):
            last = t.get("last")
            when = _iso(last / 1e6) if isinstance(last, (int, float)) and last > 0 else None
            self.elements.append(_element(str(t.get("unit", "?")), "timer", str(t.get("unit", "?")),
                                          dernier_usage=when, source_usage="systemctl list-timers (last)",
                                          declenche=t.get("activates")))
        for s in self._systemctl("list-units", "--type=service", "--all"):
            name = str(s.get("unit", "?"))
            if not name.endswith(".service"):
                continue
            self.elements.append(_element(name, "service", name, source_usage="systemctl list-units (état)",
                                          etat=f"{s.get('active')}/{s.get('sub')}"))

    # --- skills usage ----------------------------------------------------------------------------------------------
    def skills_usage(self) -> dict[str, tuple[str | None, int]]:
        root = Path(self.home) / ".claude" / "projects"
        stats: dict[str, list] = {}
        cutoff = _sortable(_iso(self.now - USAGE_DAYS * 86400))
        try:
            files = [Path(r) / f for r, _d, fs in os.walk(root) for f in fs if f.endswith(".jsonl")]
        except OSError as exc:
            self.warnings.append(f"transcripts illisibles ({type(exc).__name__})")
            return {}
        for f in files:
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"Skill"' not in line:
                            continue
                        for name, ts in _skill_calls(line):
                            cur = stats.setdefault(name, [None, 0])
                            if ts and (cur[0] is None or _sortable(ts) > _sortable(cur[0])):
                                cur[0] = ts
                            if ts and _sortable(ts) >= cutoff:
                                cur[1] += 1
            except OSError as exc:
                self.warnings.append(f"transcript {f.name} illisible ({type(exc).__name__})")
        out: dict[str, tuple[str | None, int]] = {}
        for name, (ts, count) in stats.items():
            short = name.rsplit(":", 1)[-1]
            old = out.get(short, (None, 0))
            best = max((x for x in (old[0], ts) if x), key=_sortable, default=None)
            out[short] = (best, old[1] + count)
        return out

    # --- dashboard -------------------------------------------------------------------------------------------------
    def collect_dashboard(self, journal: list[dict]) -> None:
        try:
            from ultron_hub.dashboard import catalogue
            actions = catalogue.CATALOGUE
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"catalogue dashboard illisible ({type(exc).__name__})")
            return
        rows = [(str(r.get("texte", "")), r.get("date")) for r in journal if r.get("source") == "dashboard"]
        for a in actions:
            regexes = [_template_regex(t) for t in a.journal if t]
            dates = [d for text, d in rows if d and any(r.search(text) for r in regexes if r)]
            last = max(dates, key=_sortable) if dates else None
            self.elements.append(_element(
                a.id, "action-dashboard", "dashboard:" + a.id, dernier_usage=last,
                source_usage="journal commun (source=dashboard)" if last else "journal commun : non relié",
                argv=" ".join(a.argv), risque=a.risque))

    # --- all -------------------------------------------------------------------------------------------------------
    def build(self) -> dict[str, Any]:
        steps: list[tuple[str, Callable[[], Any]]] = []
        journal: list[dict] = []
        skills: dict[str, tuple[str | None, int]] = {}

        def j() -> None:
            journal.extend(self.journal_entries())

        def s() -> None:
            skills.update(self.skills_usage())
        for label, fn in (("journal", j), ("skills", s),
                          ("dossiers", lambda: self.collect_folders(skills)),
                          ("commandes", lambda: self.collect_commands(journal)),
                          ("taches", self.collect_tasks), ("systemd", self.collect_systemd),
                          ("dashboard", lambda: self.collect_dashboard(journal))):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - one source failing never stops the rest
                self.warnings.append(f"{label} : {type(exc).__name__} {exc}"[:200])
        by_type = Counter(e["type"] for e in self.elements)
        size: Counter = Counter()
        for e in self.elements:
            if e["taille"]:
                size[e["type"]] += e["taille"]
        return {"elements": self.elements,
                "totaux": {"elements": len(self.elements), "par_type": dict(by_type),
                           "taille_par_type": dict(size),
                           "avec_usage": sum(1 for e in self.elements if e["dernier_usage"])},
                "warnings": self.warnings}


def _sortable(iso: str | None) -> str:
    """Comparable UTC string for the mixed ISO forms (naive local logs, offset dates, Z)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _skill_calls(line: str) -> Iterator[tuple[str, str | None]]:
    try:
        row = json.loads(line)
    except ValueError:
        return
    content = (row.get("message") or {}).get("content") if isinstance(row, dict) else None
    if not isinstance(content, list):
        return
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") == "Skill":
            name = (item.get("input") or {}).get("skill")
            if isinstance(name, str) and name:
                yield name, row.get("timestamp")


def _template_regex(template: str) -> re.Pattern[str] | None:
    parts = re.split(r"\{[^}]*\}", template)
    literal = [p for p in parts if p.strip()]
    if not literal:
        return None
    return re.compile(".*".join(re.escape(p) for p in parts), re.S)


def build(settings: Settings, home: str, now: float, run: Callable[..., Any] | None = None) -> dict[str, Any]:
    return Inventory(settings, home, now, run).build()
