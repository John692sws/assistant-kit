"""`ultron-hub menage ...` subcommands. Nothing is ever deleted or moved: they report, the user decides.
The one exception, `appliquer` (todo #362, ultron_hub/menage_lot.py: this package stays read only), applies a prepared tidy-up only on the user's signed ✅.

`scan` is read only and never calls Claude; `report` adds a haiku summary only when there are anomalies and
writes its state under var/menage/.

Every command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "menage.<name>", "summary": str, "data": obj|null, "error": str|null}
"""

import argparse
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.settings import Settings
from ultron_hub.core.job import JobRequest
from ultron_hub.core.runner import run_job
from ultron_hub.esprit import store as journal
from ultron_hub import menage_lot as proposition
from ultron_hub.menage import audit, checks, rangement, report, rules, skills, tests_audit, usage
from ultron_hub.todo import closing, service

log = logging.getLogger("ultron_hub.menage")


MAX_PER_CHECK = 50


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"menage.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    menage = sub.add_parser("menage", help="server tidiness (read only: it reports, the user decides)")
    cmds = menage.add_subparsers(dest="menage_command", required=True, parser_class=parser_class)
    cmds.add_parser("scan", help="compare the server with config/menage.yaml and list the gaps")
    rep = cmds.add_parser("report", help="scan, short report (AI summary only if anomalies), state in var/menage/")
    rep.add_argument("--send", action="store_true", help="send the short report on Telegram (envoi)")
    tst = cmds.add_parser("tests", help="run the test suite once and report its health (duration, slow tests, leaks)")
    tst.add_argument("--send", action="store_true", help="send the result on Telegram (envoi)")
    cmds.add_parser("last", help="last report (var/menage/last.md and its short text)")
    cmds.add_parser("audit", help="sonnet re-reads the tidy-up map and proposes changes (applies nothing)")
    use = cmds.add_parser("usage", help="what is really used on Telegram, and what never is (todo #166, no AI)")
    use.add_argument("--jours", type=int, default=usage.DAYS)
    pro = cmds.add_parser("proposer", help="prepare a tidy-up for the user's one signed ✅ (todo #362, applies nothing)")
    pro.add_argument("--cancel", action="append", default=[], metavar="ID:RAISON",
                     help="an open todo to give up, with a one-line reason (repeatable)")
    pro.add_argument("--archiver", action="append", default=[], metavar="CHANTIER",
                     help="an open chantier to archive, after the todos (repeatable)")
    pro.add_argument("--corbeille", action="append", default=[], metavar="NOM:RAISON",
                     help="a skill of ~/.claude/skills to move to .trash (reversible), with a one-line reason (repeatable)")
    pro.add_argument("--dossier", action="append", default=[], metavar="CHEMIN:RAISON",
                     help="a folder flagged by tidy-up check 10 (aged_dirs of config/menage.yaml) to move to var/.trash "
                          "(reversible), absolute path, one-line reason (repeatable, todo #474)")
    pro.add_argument("--par", default="agent", help="who proposes (shown on the card)")
    cmds.add_parser("skills", help="how much each skill of ~/.claude/skills is used (todo #451, no AI, read only)")
    cmds.add_parser("proposition", help="the pending tidy-up proposal and its fingerprint (read only)")
    cmds.add_parser("appliquer-auto", help="apply the proposal alone (by auto:bilan) if nothing is really deleted; refused from a job (todo #535)")
    app = cmds.add_parser("appliquer", help="apply the proposal (signed menage-lot on its fingerprint), stop at the first refusal")
    app.add_argument("--empreinte", required=True, help="fingerprint shown with the proposal (`menage proposition`)")
    app.add_argument("--i-am-human", dest="human", action="store_true",
                     help="confirm at a real terminal (typed code) instead of a signed button")


def run_scan(settings: Settings, *, home: str | None = None, config_path: Path | None = None,
             probe: Callable[[], list[dict[str, Any]]] | None = None,
             now: float | None = None) -> dict[str, Any]:
    """Run the 20 checks. Never raises because of the disk: a failing check becomes a warning."""
    cfg = rules.load_config(config_path or settings.root / "config" / "menage.yaml")
    scan = rules.Scan(cfg=cfg, home=home or rules.user_home())
    if now is not None:
        scan.now = now
    scan.todo_db = settings.todo_db
    scan.history_path = settings.var_dir / "menage" / "history.jsonl"
    if probe is None:
        from ultron_hub.health.collect import probe_disks as probe
    cached: list[list[dict[str, Any]]] = []

    def disks() -> list[dict[str, Any]]:
        if not cached:
            try:
                cached.append(probe())
            except Exception as exc:  # noqa: BLE001 - best effort
                scan.warn(f"disque illisible ({type(exc).__name__})")
                cached.append([])
        return cached[0]

    scan.probe = disks
    anomalies: list[dict[str, Any]] = []
    for check in checks.CHECKS:
        scan.reset_budget()
        try:
            anomalies += check(scan)
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            scan.warn(f"{check.__name__} a échoué ({type(exc).__name__})")
    anomalies.sort(key=lambda a: (a["check"], a["path"]))
    counts = dict(sorted(Counter(str(a["check"]) for a in anomalies).items(), key=lambda kv: int(kv[0])))
    kept: list[dict[str, Any]] = []
    truncated: dict[str, int] = {}
    shown: Counter = Counter()
    for item in anomalies:  # `counts` keeps the true totals; the list is bounded per check
        key = str(item["check"])
        shown[key] += 1
        if shown[key] <= MAX_PER_CHECK:
            kept.append(item)
        else:
            truncated[key] = shown[key] - MAX_PER_CHECK
    return {"anomalies": kept, "counts": counts, "truncated": truncated, "sizes": scan.sizes, "disk": disks(),
            "warnings": scan.warnings}


def state_dir(settings: Settings) -> Path:
    return settings.var_dir / "menage"


def load_ignores(path: Path) -> set[str]:
    """Accepted anomalies (`paths:` list in var/menage/ignores.yaml); unreadable file = nothing ignored."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, yaml.YAMLError):
        return set()
    paths = raw.get("paths") if isinstance(raw, dict) else None
    return {str(p) for p in paths if isinstance(p, str)} if isinstance(paths, list) else set()


def apply_ignores(scan: dict[str, Any], ignored: set[str]) -> dict[str, Any]:
    if not ignored:
        return scan
    kept = [a for a in scan["anomalies"] if a.get("path") not in ignored]
    counts = Counter(scan.get("counts") or {})
    for a in scan["anomalies"]:
        if a.get("path") in ignored:
            counts[str(a["check"])] -= 1
    return {**scan, "anomalies": kept, "counts": {k: v for k, v in counts.items() if v > 0},
            "ignored": len(scan["anomalies"]) - len(kept)}


def summarize(metrics: str, settings: Settings) -> dict[str, Any]:
    """tasks/menage-resume (haiku, tools: []); raises on failure, the report then keeps the script's actions."""
    result = run_job(JobRequest(action="menage-resume", params={"metrics": metrics}), settings)
    if not result.ok:
        raise UltronHubError(f"menage-resume failed: {result.error}")
    data = result.data
    if not isinstance(data, dict) or not isinstance(data.get("actions"), list):
        raise UltronHubError("menage-resume: invalid structured_output")
    return data


def do_tests(settings: Settings, send: bool, analyse: Callable[[Settings], dict[str, Any]] | None = None
             ) -> dict[str, Any]:
    data = (analyse or tests_audit.analyse)(settings)
    text = "\n".join(tests_audit.lines(data))
    if send:
        service.send_message(text, settings.todo_notify_target, settings.hermes_binary)
    return {**data, "text": text, "sent": send}


def tests_section(settings: Settings, analyse: Callable[[Settings], dict[str, Any]]) -> tuple[str, str]:
    """(short text, markdown) of the weekly report's tests section; a failure only becomes one line."""
    try:
        data = analyse(settings)
        return "\n".join(tests_audit.lines(data)), tests_audit.markdown(data)
    except Exception as exc:  # noqa: BLE001 - the rest of the report must go out
        line = f"🧪 Tests : section indisponible ({type(exc).__name__})"
        return line, f"## Tests\n\n- {line}\n"


def restore_test_line(settings: Settings) -> str:
    """One line on the last `backup restore-test` ('' when none was ever recorded)."""
    from ultron_hub.backup.run import default_layout, status
    last = status(default_layout(settings.var_dir, None))["last_restore_test"] or {}
    day = str(last.get("at") or "")[:10]
    if last.get("status") == "ok":
        return f"💾 Restauration testée le {day} : la sentinelle et la base du snapshot reviennent intactes"
    if last.get("status") == "error":
        return f"💾 Test de restauration du {day} en échec : {str(last.get('error'))[:120]}"
    return ""


def do_report(settings: Settings, send: bool, scan: dict[str, Any] | None = None,
              ai: Callable[[str], dict[str, Any]] | None = None,
              tests_analyse: Callable[[Settings], dict[str, Any]] | None = None) -> dict[str, Any]:
    if scan is None:  # automatic gesture (todo #231): archive the Reprise over its budget BEFORE the scan sees it
        try:
            rangement.archiver_reprises(settings, rules.load_config(Path(settings.root) / "config" / "menage.yaml"))
        except Exception:  # noqa: BLE001
            log.warning("menage: archivage automatique ignoré", exc_info=True)
        scan = run_scan(settings)
    scan = apply_ignores(scan, load_ignores(state_dir(settings) / "ignores.yaml"))
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    audit_due = audit.due(state_dir(settings), time.time(), audit_every_days(settings))
    built = report.build(scan, ai or (lambda m: summarize(m, settings)), when, audit_due=audit_due)
    try:  # real use of the Telegram doors (todo #166): one line, never a reason to fail the report
        use_line = usage.line(usage.summary(settings.var_dir))
        built["text"] += "\n" + use_line
        built["markdown"] += f"\n## Usage réel\n\n- {use_line}\n"
    except Exception:  # noqa: BLE001
        log.warning("menage: usage line skipped", exc_info=True)
    try:  # last monthly restore test (todo #162): one line, never a reason to fail the report
        restore_line = restore_test_line(settings)
        if restore_line:
            built["text"] += "\n" + restore_line
            built["markdown"] += f"\n## Restauration testée\n\n- {restore_line}\n"
    except Exception:  # noqa: BLE001
        log.warning("menage: restore-test line skipped", exc_info=True)
    try:  # skills never or no longer used (todo #451): one line, and a proposal if none is pending
        sk = skills.usage()
        built["text"] += "\n" + skills.line(sk)
        built["markdown"] += f"\n## Skills\n\n- {skills.line(sk)}\n"
        if sk["corbeille"] and not proposition.path(settings).exists():
            proposition.proposer(settings, [], [], par="menage-hebdo",
                                 corbeille=[f"{n}:aucun usage depuis 30 j" for n in sk["corbeille"]])
    except Exception:  # noqa: BLE001
        log.warning("menage: skills line skipped", exc_info=True)
    if tests_analyse is not None:  # the weekly report only (handle): the suite runs once, with a deadline
        tests_text, tests_md = tests_section(settings, tests_analyse)
        built["text"] += "\n\n" + tests_text
        built["markdown"] += "\n" + tests_md
    report.save_state(state_dir(settings), {**scan, "text": built["text"], "summary": built["summary"]},
                      built["markdown"])
    if send:
        service.send_message(built["text"], settings.todo_notify_target, settings.hermes_binary)
    try:  # pinned boards of the group topics (todo #202): never blocks the report
        from ultron_hub.groupe import tableau
        tableau.rafraichir(settings)
    except Exception:  # noqa: BLE001
        log.exception("tableaux du groupe")
    total = sum(scan["counts"].values())
    journal.record(settings, "ultron-hub", "menage", f"Ménage du serveur : {total} écart(s)"
                   + (", rapport envoyé" if send else ""))
    return {"text": built["text"], "anomalies": total, "ai_called": built["ai_called"], "ai_error": built["ai_error"],
            "sent": send, "path": str(state_dir(settings) / "last.md"), "audit_due": audit_due}


def audit_every_days(settings: Settings) -> int:
    try:
        return int(rules.load_config(Path(settings.root) / "config" / "menage.yaml")["report"]["audit_every_days"])
    except Exception:  # noqa: BLE001 - the map is checked by the scan; here the default is enough
        return audit.EVERY_DAYS


def do_last(settings: Settings) -> dict[str, Any]:
    folder = state_dir(settings)
    try:
        last = json.loads((folder / "last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"text": "", "exists": False}
    return {"text": last.get("text", ""), "exists": True, "path": str(folder / "last.md"),
            "counts": last.get("counts", {})}


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.menage_command
    if cmd == "scan":
        data = run_scan(settings)
        return envelope(cmd, summary=f"{sum(data['counts'].values())} anomalie(s)", data=data)
    if cmd == "report":
        data = do_report(settings, args.send, tests_analyse=tests_audit.analyse)
        return envelope(cmd, summary=f"{data['anomalies']} anomalie(s)" + (", envoyé" if data["sent"] else ""),
                        data=data)
    if cmd == "tests":
        data = do_tests(settings, args.send)
        return envelope(cmd, summary=data["text"].splitlines()[0], data=data)
    if cmd == "audit":
        data = audit.run(settings)
        return envelope(cmd, summary=f"{len(data['proposals'])} proposition(s)", data=data)
    if cmd == "usage":
        if not 1 <= args.jours <= 366:
            return envelope(cmd, status="error", error="--jours : entre 1 et 366")
        data = usage.summary(settings.var_dir, days=args.jours)
        data["text"] = usage.text(data)
        return envelope(cmd, summary=usage.line(data), data=data)
    if cmd == "proposer":
        data = proposition.proposer(settings, args.cancel, args.archiver, par=args.par, corbeille=args.corbeille,
                                     dossiers=args.dossier)
        return envelope(cmd, summary=f"🧹 ménage proposé ({data['n']}), en attente du ✅ de l'utilisateur", data=data)
    if cmd == "skills":
        data = skills.usage()
        return envelope(cmd, summary=skills.line(data), data=data)
    if cmd == "proposition":
        data = proposition.voir(settings)
        return envelope(cmd, summary=f"🧹 ménage proposé ({data['n']})" if data["n"] else "aucun ménage proposé", data=data)
    if cmd == "appliquer-auto":
        data = proposition.appliquer_auto(settings)
        resume = {"rien": "aucun ménage proposé", "refuse": "application automatique refusée",
                  "applique": (data.get("text") or "").split("\n")[0]}[data["statut"]]
        return envelope(cmd, status="error" if data["statut"] == "refuse" else "ok", summary=resume, data=data,
                        error=data.get("raison") if data["statut"] == "refuse" else None)
    if cmd == "appliquer":
        def authorize(view: dict[str, Any]) -> dict[str, Any]:
            what = (f"Abandonner {', '.join(f'#{c['id']}' for c in view['cancel']) or 'aucune todo'} et archiver "
                    f"{', '.join(a['sujet'] for a in view['archiver']) or 'aucun chantier'}"
                    + (f", skills à la corbeille : {', '.join(c['nom'] for c in view['corbeille'])}" if view["corbeille"] else "")
                    + (f", dossiers à var/.trash : {', '.join(d['chemin'] for d in view['dossiers'])}" if view["dossiers"] else "")
                    + " ?")
            return closing.authorize(proposition.ACTION, args.empreinte, human=args.human, what=what,
                                     used=lambda proof: proposition.proof_used(settings, proof))
        data = proposition.appliquer(settings, args.empreinte, authorize)
        ok = data["arret"] is None
        return envelope(cmd, status="ok" if ok else "error", summary=data["text"].splitlines()[0], data=data,
                        error=None if ok else f"arrêt à {data['arret']['item']} : {data['arret']['raison']}")
    if cmd == "last":
        data = do_last(settings)
        return envelope(cmd, summary="dernier rapport" if data["exists"] else "pas encore de rapport", data=data)
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
