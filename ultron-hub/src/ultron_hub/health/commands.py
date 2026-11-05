"""`ultron-hub health ...` subcommands (no Claude involved).

Every command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "health.<name>", "summary": str, "data": obj|null, "error": str|null}
"""

import argparse
from typing import Any

from ultron_hub.health import text
from ultron_hub.health.collect import collect, record_history


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"health.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    health = sub.add_parser("health", help="server metrics, collected without Claude")
    cmds = health.add_subparsers(dest="health_command", required=True, parser_class=parser_class)
    collect_cmd = cmds.add_parser("collect", help="all metrics as JSON")
    collect_cmd.add_argument("--record", action="store_true", help="also append a sample to var/health/history.jsonl")
    cmds.add_parser("guard", help="is the ultron-guard Hermes plugin loaded?")
    cmds.add_parser("alertes", help="rules on the health figures; Telegram message only when the state changes")
    cmds.add_parser("battery", help="AC adapter and battery state")
    cmds.add_parser("reflexes", help="known breakdowns diagnosed by the last `health alertes` (no network)")
    act = cmds.add_parser("reflexe-agir", help="the button of a diagnosis (signed token or --i-am-human); "
                                               "ultron-user checks every condition again before acting")
    act.add_argument("cible", help="<reflex>:<fingerprint>, from `health reflexes`")
    act.add_argument("--i-am-human", dest="human", action="store_true",
                     help="the user at a real terminal (typed code), instead of the Telegram button")


def handle(args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.health_command
    if cmd == "collect":
        metrics = collect()
        if args.record:
            record_history(metrics)
        failed = sorted(metrics["errors"])
        summary = "métriques collectées" + (f" (sondes en échec : {', '.join(failed)})" if failed else "")
        return envelope(cmd, summary=summary, data=metrics)
    if cmd == "alertes":
        from ultron_hub.common.settings import load_settings
        from ultron_hub.health import alertes
        res = alertes.run_alertes(load_settings())
        n = len(res["actives"])
        summary = (f"{n} anomalie(s) active(s)" if n else "rien à signaler") + (
            f", message envoyé ({len(res['new'])} nouvelle(s), {len(res['resolved'])} résolue(s))" if res["sent"] else "")
        if res["send_error"]:
            return envelope(cmd, status="error", summary=summary + ", envoi échoué", data=res, error=res["send_error"])
        return envelope(cmd, summary=summary, data=res)
    if cmd in ("reflexes", "reflexe-agir"):
        from ultron_hub.common.settings import load_settings
        from ultron_hub.esprit import store as journal
        from ultron_hub.health import reflexes
        settings = load_settings()
        if cmd == "reflexes":
            diags = reflexes.lire(settings.root)
            summary = f"{len(diags)} diagnostic(s)" if diags else "aucune panne connue en cours"
            return envelope(cmd, summary=summary, data={"diagnostics": diags})
        from ultron_hub.common.errors import InvalidRequestError
        try:
            res = reflexes.agir(settings.root, args.cible, human=args.human)
        except InvalidRequestError as exc:  # no proof, spent token, bad target: nothing done
            return envelope(cmd, status="error", summary=f"Refusé : {exc}", error=str(exc))
        journal.record(settings, "ultron-hub", "sante", f"Réflexe {args.cible} ({res['by'].get('by')}) : {res['message']}")
        data = {"cible": args.cible, "by": res["by"].get("by"), "result": res["result"]}
        if not res["ok"]:
            return envelope(cmd, status="error", summary=res["message"], data=data, error=res["message"])
        return envelope(cmd, summary=res["message"], data=data)
    if cmd == "guard":
        guard = collect(only=("guard",))["guard"]
        summary = "ultron-guard chargé" if guard["ok"] else f"ultron-guard NON chargé : {guard['problem']}"
        return envelope(cmd, status="ok", summary=summary, data=guard)
    if cmd == "battery":
        metrics = collect(only=("power",))
        if metrics["errors"]:
            return envelope(cmd, status="error", summary="batterie illisible", error=metrics["errors"]["power"])
        rendered = text.battery_text(metrics["power"])
        lines = rendered.splitlines()
        summary = next((line for line in lines if "Batterie" in line), lines[0])
        return envelope(cmd, summary=summary, data={**metrics["power"], "text": rendered})
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
