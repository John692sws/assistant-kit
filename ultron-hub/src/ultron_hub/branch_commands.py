"""`ultron-hub branch ...`: code branches linked to todos (`todo-<id>/<name>`).

Every command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "branch.<name>", "summary": str, "data": obj|null, "error": str|null}
"""

import argparse
from typing import Any

from ultron_hub import after_merge, auto_merge, autorisation, branch_etat, branch_lot, branches, user_actions
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import etat
from ultron_hub.todo import closing, service
from ultron_hub.todo.store import TodoStore


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"branch.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    branch = sub.add_parser("branch", help="code branches linked to todos (todo-<id>/<name>)")
    cmds = branch.add_subparsers(dest="branch_command", required=True, parser_class=parser_class)
    cmds.add_parser("info", help="branch of a todo and its commits not in main").add_argument("todo_id", type=int)
    merge = cmds.add_parser("merge", help="trial merge + tests, then merge into main, close the todo, restart the gateway")
    merge.add_argument("todo_id", type=int)
    merge.add_argument("--i-am-human", dest="human", action="store_true",
                       help="at a real terminal: type back a code to confirm (refused for agents)")
    auto = cmds.add_parser("auto-merge", help="orchestrator only: merge a green branch like 🔀 but leave the todo open "
                           "(refused from a runner job, on sensitive files, with after-merge actions)")
    auto.add_argument("todo_id", type=int)
    cmds.add_parser("auto-merge-lot", help="orchestrator only, under the user's 🤝 authorisation: merge every ready todo "
                    "like auto-merge, one by one, the refused ones listed with their reason")
    cmds.add_parser("etat", help="state of the merges: branches in progress, waiting for the user's 🔀 and why, automatic merges "
                    "(read only)")
    cmds.add_parser("auto-last", help="the last automatic merge and the target ↩️ signs (read only)")
    revert = cmds.add_parser("revert-last", help="revert the last automatic merge (signed `revert-fusion`)")
    revert.add_argument("--cible", default=None, help="the target shown (`branch auto-last` → data.cible): <id>-<sha12>")
    revert.add_argument("--i-am-human", dest="human", action="store_true",
                        help="at a real terminal: type back a code to confirm (refused for agents)")
    auth = cmds.add_parser("autorisation", help="the user's signed advance authorisation of automatic merges (todo #495)")
    auth_cmds = auth.add_subparsers(dest="autorisation_command", required=True, parser_class=parser_class)
    auth_cmds.add_parser("etat", help="valid or not, until when, by whom, the current perimeter (read only)")
    give = auth_cmds.add_parser("donner", help="authorise automatic merges for N days (signed `autorisation`)")
    give.add_argument("--jours", type=int, required=True, choices=autorisation.JOURS, help="duration in days")
    give.add_argument("--perimetre", required=True, help="the perimeter shown (`branch autorisation etat` → data.perimetre)")
    give.add_argument("--i-am-human", dest="human", action="store_true",
                      help="at a real terminal: type back a code to confirm (refused for agents)")
    auth_cmds.add_parser("retirer", help="withdraw the authorisation (no signature: it removes rights)")
    lot = cmds.add_parser("lot", help="todos ready to merge, in merge order, and the lot's fingerprint (read only)")
    lot.add_argument("--sans", default="", help="todos left out of the lot, e.g. 134,135")
    merge_lot = cmds.add_parser("merge-lot", help="merge a whole lot (signed on its fingerprint), stop at the first refusal")
    merge_lot.add_argument("--ids", required=True, help="the lot's todos, e.g. 81,82,83")
    merge_lot.add_argument("--empreinte", required=True, help="fingerprint shown with the lot (`branch lot`)")
    merge_lot.add_argument("--i-am-human", dest="human", action="store_true",
                           help="at a real terminal: type back a code to confirm (refused for agents)")
    cmds.add_parser("after", help="server actions (no sudo) declared by the todo for after its merge"
                    ).add_argument("todo_id", type=int)
    exe = cmds.add_parser("after-exec", help="(internal) run an already approved after-merge action; started by after-run in its own unit")
    exe.add_argument("todo_id", type=int)
    exe.add_argument("n", type=int)
    for name, helptext in (("after-run", "approve and run one pending after-merge action (via ultron-user)"),
                           ("after-refuse", "refuse one pending after-merge action")):
        p = cmds.add_parser(name, help=helptext)
        p.add_argument("todo_id", type=int)
        p.add_argument("n", type=int)
        if name == "after-run":
            p.add_argument("--i-am-human", dest="human", action="store_true",
                           help="at a real terminal: type back a code to confirm (refused for agents)")


def after_text(req: dict[str, Any], todo_id: int) -> str:
    return user_actions.action_text(req, owner=f"Déclarée par la tâche #{todo_id} pour après la fusion.")


def merge_text(r: dict[str, Any]) -> str:
    check = r["check"]
    if not check["ok"]:
        conflict = check["stage"] == "conflict"
        slow = bool(check.get("timeout"))  # todo #401: a slow suite is not a red one
        why = "conflit avec main" if conflict else "tests trop longs" if slow else "les tests échouent"
        todo_hint = ("rebase la branche sur main (ou demande à Claude Code) pour résoudre ces fichiers, puis relance 🔀."
                     if conflict else
                     "allège la suite de tests (ou demande à Claude Code) : elle dépasse le délai de la fusion, "
                     "puis relance 🔀." if slow else
                     "corrige les tests en échec sur la branche (ou demande à Claude Code), puis relance 🔀.")
        files = "".join(f"\n• {f}" for f in check.get("files", [])[:branches.MAX_FILES_SHOWN])
        more = len(check.get("files", [])) - branches.MAX_FILES_SHOWN
        files += f"\n… et {more} autres" if more > 0 else ""
        return (f"❌ Fusion refusée ({why}) : {r['branch']}\n\n{check['detail']}"
                + (f"\n\nFichiers en cause :{files}" if files else "")
                + f"\n\nÀ faire : {todo_hint}\n\nLa tâche #{r['todo_id']} reste ouverte, main n'a pas changé.")
    tests = f"🧪 Tests : {check['detail']}" if check.get("tested", True) else f"⚠️ Non testé : {check['detail']}"
    lines = [f"🔀 {r['branch']} fusionnée dans main" if r["merged"] else f"ℹ️ {r['branch']} : rien à fusionner",
             tests, f"✅ Tâche #{r['todo_id']} terminée"]
    if r.get("cleanup"):
        lines.append(f"⚠️ Branche ou worktree à supprimer à la main : {r['cleanup']}")
    if r["restart"] == "planned":
        lines.append(f"🔄 Le bot redémarre dans {branches.RESTART_DELAY_S} s pour charger le nouveau code.")
    elif r["restart"]:
        lines.append(f"⚠️ Redémarrage du bot : {r['restart']}")
    if r.get("dashboard") == "planned":
        lines.append(f"🔄 Le dashboard redémarre dans {branches.RESTART_DELAY_S} s (son code a changé).")
    elif r.get("dashboard"):
        lines.append(f"⚠️ Redémarrage du dashboard : {r['dashboard']}")
    todo = after_merge.pending(r.get("after_merge") or [])
    if todo:
        lines.append(f"🧰 {len(todo)} action(s) serveur prévue(s) après la fusion : à valider (✅ Exécuter) "
                     "ou refuser une par une.")
        lines += [f"• {user_actions.action_line(a)}" for a in todo]
    invalid = [a for a in r.get("after_merge") or [] if a["status"] == "invalid"]
    if invalid:
        lines.append(f"⛔ {len(invalid)} action(s) après fusion hors liste blanche : jamais exécutée(s).")
    if r.get("after_merge_error"):
        lines.append(f"⚠️ Actions après fusion illisibles : {r['after_merge_error']}")
    return "\n".join(lines)


def auto_text(r: dict[str, Any]) -> str:
    """Text of an automatic merge: like 🔀, but the todo stays open for the user's ✅ (📥 À toi)."""
    if not r["check"]["ok"]:
        return merge_text(r)
    check = r["check"]
    tests = f"🧪 Tests : {check['detail']}" if check.get("tested", True) else f"⚠️ Non testé : {check['detail']}"
    shas = ", ".join((x.get("sha") or "")[:auto_merge.SHA12_LEN] for x in r.get("repos") or [] if x.get("merged"))
    lines = [f"🤖 {r['branch']} fusionnée seule dans main ({shas})" if r["merged"] else f"ℹ️ {r['branch']} : rien à fusionner",
             tests, f"📥 Tâche #{r['todo_id']} encore ouverte : ton ✅ dans À toi (↩️ Annuler la dernière fusion possible)"]
    if r.get("cleanup"):
        lines.append(f"⚠️ Branche ou worktree à supprimer à la main : {r['cleanup']}")
    if r["restart"] == "planned":
        lines.append(f"🔄 Le bot redémarre dans {branches.RESTART_DELAY_S} s pour charger le nouveau code.")
    elif r["restart"]:
        lines.append(f"⚠️ Redémarrage du bot : {r['restart']}")
    if r.get("dashboard") == "planned":
        lines.append(f"🔄 Le dashboard redémarre dans {branches.RESTART_DELAY_S} s (son code a changé).")
    elif r.get("dashboard"):
        lines.append(f"⚠️ Redémarrage du dashboard : {r['dashboard']}")
    return "\n".join(lines)


def auto_lot_text(r: dict[str, Any]) -> str:
    """Text of the automatic lot (todo #533): one line per todo, merged with its sha or refused with its reason."""
    lines = [f"🤖 Fusion automatique du lot : {len(r['fusionnees'])}/{len(r['ids'])} tâche(s) fusionnée(s), "
             "encore ouvertes pour ton ✅"]
    for b in r["bilan"]:
        lines.append(f"• #{b['todo_id']} fusionnée ({(b['sha'] or '')[:auto_merge.SHA12_LEN]})"
                     if b["statut"] == "fusionnee" else f"• #{b['todo_id']} refusée : {b['raison']}")
    lines += [f"• #{e['todo_id']} écartée : {e['raison']}" for e in r.get("ecartees") or []]
    if r["restart"] == "planned":
        lines.append(f"🔄 Le bot redémarre dans {branches.RESTART_DELAY_S} s pour charger le nouveau code.")
    elif r["restart"]:
        lines.append(f"⚠️ Redémarrage du bot : {r['restart']}")
    if r.get("dashboard") == "planned":
        lines.append(f"🔄 Le dashboard redémarre dans {branches.RESTART_DELAY_S} s (son code a changé).")
    elif r.get("dashboard"):
        lines.append(f"⚠️ Redémarrage du dashboard : {r['dashboard']}")
    return "\n".join(lines)


def revert_text(r: dict[str, Any]) -> str:
    lines = [f"↩️ Fusion automatique {r['cible']} annulée (todo #{r['todo_id']} reste ouverte)"]
    lines += [f"• {x['repo']} : {x['sha'][:auto_merge.SHA12_LEN]} annulée par {x['revert'][:auto_merge.SHA12_LEN]}"
              for x in r["repos"]]
    if r["restart"] == "planned":
        lines.append(f"🔄 Le bot redémarre dans {branches.RESTART_DELAY_S} s pour charger le code d'avant.")
    elif r["restart"]:
        lines.append(f"⚠️ Redémarrage du bot : {r['restart']}")
    if r.get("dashboard") == "planned":
        lines.append(f"🔄 Le dashboard redémarre dans {branches.RESTART_DELAY_S} s (son code a changé).")
    elif r.get("dashboard"):
        lines.append(f"⚠️ Redémarrage du dashboard : {r['dashboard']}")
    return "\n".join(lines)


def lot_text(r: dict[str, Any]) -> str:
    """Telegram text of 🔀 Tout fusionner: one line per merged todo, then where and why it stopped."""
    total = len(r["ids"])
    lines = [f"🔀 Tout fusionner : {len(r['results'])}/{total} tâche(s) fusionnée(s) et terminée(s)"]
    # todo #401: the outcome first (the dashboard keeps the first 3000 characters), then the details
    bilan = r.get("bilan") or []
    merged = [b for b in bilan if b["fusionnee"]]
    opened = [b for b in bilan if not b["fusionnee"]]
    if merged:
        lines.append("✅ Fusionnées : " + ", ".join(f"#{b['todo_id']}" for b in merged))
    if opened:
        lines.append("⏸ Restées ouvertes, main n'a rien d'elles :")
        lines += [f"• #{b['todo_id']} {b['title']} : {b['raison']}" for b in opened]
    out = [e for e in r.get("ecartees") or [] if e["raison"] != "retirée du lot"]
    if out:
        lines.append("🚫 Hors du lot, pas tentées :")
        lines += [f"• #{e['todo_id']} {e['title']} : {e['raison']}" for e in out[:10]]
    if bilan or out:
        lines.append("")
    for m in r["results"]:
        what = "fusionnée dans main" if m["merged"] else "rien à fusionner"
        lines.append(f"✅ #{m['todo_id']} : {m['branch']} {what}")
        if m.get("cleanup"):
            lines.append(f"   ⚠️ Branche ou worktree à supprimer à la main : {m['cleanup']}")
        for a in m.get("after_merge") or []:
            if (a.get("approval") or {}).get("via") == "fusion-lot":
                state = {"done": "✅ faite", "failed": "❌ échec", "running": "⚙️ en cours",
                         "interrupted": "⚠️ interrompue"}.get(a["status"], a["status"])
                lines.append(f"   🧰 {user_actions.action_line(a)} : {state}"
                             + (f" ({a['error']})" if a.get("error") else ""))
        todo = after_merge.pending(m.get("after_merge") or [])
        if todo:
            lines.append(f"   🧰 {len(todo)} action(s) serveur à valider après la fusion de #{m['todo_id']}")
        if m.get("actions_note"):
            lines.append(f"   ⚠️ {m['actions_note']}")
        if m.get("after_merge_error"):
            lines.append(f"   ⚠️ Actions après fusion illisibles : {m['after_merge_error']}")
    if r.get("actions_note"):
        lines.append(f"⚠️ {r['actions_note']}")
    stop = r.get("stopped")
    if stop:
        text = merge_text(stop["result"]) if "result" in stop else stop["refusal"]
        lines += ["", f"⛔ Arrêt à #{stop['todo_id']} :", text]
        if r["remaining"] and not bilan:
            lines += ["", "⏸ Pas fusionnées, restent ouvertes : " + ", ".join(f"#{t}" for t in r["remaining"])]
    if r["restart"] == "planned":
        lines += ["", f"🔄 Le bot redémarre dans {branches.RESTART_DELAY_S} s pour charger le nouveau code."]
    elif r["restart"]:
        lines += ["", f"⚠️ Redémarrage du bot : {r['restart']}"]
    if r.get("dashboard") == "planned":
        lines += ["", f"🔄 Le dashboard redémarre dans {branches.RESTART_DELAY_S} s (son code a changé)."]
    elif r.get("dashboard"):
        lines += ["", f"⚠️ Redémarrage du dashboard : {r['dashboard']}"]
    return "\n".join(lines)


def autorisation_handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    sub = args.autorisation_command
    cmd = f"autorisation.{sub}"
    if sub == "etat":
        data = autorisation.etat(settings)
        summary = (f"autorisation valide jusqu'au {data['jusqu_au']} ({data['by']})" if data["valide"]
                   else "aucune autorisation valide : règles habituelles (fichiers sensibles)")
        return envelope(cmd, summary=summary, data=data)
    if sub == "donner":
        target = autorisation.check(args.jours, args.perimetre)
        fin = autorisation.fin(args.jours)
        by = closing.authorize("autorisation", target, human=args.human,
                               what=f"Autoriser les fusions automatiques jusqu'au {fin} (sauf la liste de sécurité) ?",
                               used=lambda proof: autorisation.proof_used(settings, proof))
        data = autorisation.donner(settings, args.jours, args.perimetre, by)
        return envelope(cmd, summary=f"autorisation donnée jusqu'au {data['jusqu_au']} : l'orchestrateur fusionne seul "
                        "tout sauf la liste de sécurité", data=data)
    data = autorisation.retirer(settings)
    return envelope(cmd, summary="autorisation retirée" if data["retiree"] else "aucune autorisation à retirer", data=data)


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.branch_command
    if cmd == "info":
        data = branches.info(settings, args.todo_id)
        summary = (f"{data['branch']} : {data['ahead']} commit(s) hors main" if data["branch"]
                   else f"{len(data['branches'])} branche(s) todo-{args.todo_id}/")
        return envelope(cmd, summary=summary, data=data)
    if cmd == "merge":
        with TodoStore(settings.todo_db) as todos:
            task = todos.get(args.todo_id)
            by = closing.authorize("merge", task.id, human=args.human,
                                   what=f"Fusionner la branche et terminer {service.format_line(task)} ?",
                                   used=lambda proof: todos.proof_used(task.id, proof))
        r = branches.merge_and_close(settings, args.todo_id, by=by)
        if r["merged"]:
            etat.refresh(settings)
        ok = r["closed"]
        return envelope(cmd, status="ok" if ok else "error", summary=merge_text(r).splitlines()[0],
                        data={**r, "text": merge_text(r)}, error=None if ok else r["check"]["detail"])
    if cmd == "auto-merge":
        r = auto_merge.auto_merge(settings, args.todo_id)
        if r["merged"]:
            etat.refresh(settings)
        ok = r["check"]["ok"]
        text = auto_text(r)
        return envelope(cmd, status="ok" if ok else "error", summary=text.splitlines()[0],
                        data={**r, "text": text}, error=None if ok else r["check"]["detail"])
    if cmd == "auto-merge-lot":
        r = auto_merge.auto_merge_lot(settings)
        if r["fusionnees"]:
            etat.refresh(settings)
        text = auto_lot_text(r)
        ok = not r["refusees"]
        return envelope(cmd, status="ok" if ok else "error", summary=text.splitlines()[0], data={**r, "text": text},
                        error=None if ok else "refusée(s) : " + ", ".join(f"#{t}" for t in r["refusees"]))
    if cmd == "etat":
        data = branch_etat.etat(settings)
        return envelope(cmd, summary=f"{len(data['en_cours'])} en cours, {len(data['attente'])} en attente du 🔀, "
                        f"{len(data['auto'])} fusion(s) automatique(s)", data=data)
    if cmd == "auto-last":
        info = auto_merge.last(settings)
        data = {"cible": info["cible"], "annulee": info["annulee"], "fusion": info["fusion"]}
        summary = (f"dernière fusion automatique : {info['cible']}" + (" (annulée)" if info["annulee"] else "")
                   if info["cible"] else "aucune fusion automatique")
        return envelope(cmd, summary=summary, data=data)
    if cmd == "revert-last":
        info = auto_merge.check_revert(settings, args.cible)
        by = closing.authorize("revert-fusion", info["cible"], human=args.human,
                               what=f"Annuler la fusion automatique {info['cible']} ?",
                               used=lambda proof: proof in info["proofs"])
        r = auto_merge.revert_last(settings, by, cible=info["cible"])
        etat.refresh(settings)
        text = revert_text(r)
        return envelope(cmd, summary=text.splitlines()[0], data={**r, "text": text})
    if cmd == "autorisation":
        return autorisation_handle(args, settings)
    if cmd == "lot":
        sans = branch_lot.parse_ids(args.sans) if args.sans else []
        data = branch_lot.lot(settings, sans=sans)
        return envelope(cmd, summary=f"{len(data['lot'])} tâche(s) prête(s) à fusionner", data=data)
    if cmd == "merge-lot":
        ids = branch_lot.parse_ids(args.ids)
        if branch_lot.EMPREINTE_RE.fullmatch(args.empreinte) is None:
            return envelope(cmd, status="error", error="empreinte invalide (16 caractères hexadécimaux)")
        data = branch_lot.check(settings, ids, args.empreinte)
        with TodoStore(settings.todo_db) as todos:
            by = closing.authorize("fusion-lot", data["empreinte"], human=args.human,
                                   what="Fusionner et terminer " + ", ".join(f"#{t}" for t in data["ids"]) + " ?",
                                   used=lambda proof: any(todos.proof_used(t, proof) for t in ids))
        r = branch_lot.run(settings, data, by, launch=after_merge.launch_unit)
        etat.refresh(settings)
        text = lot_text(r)
        ok = r["stopped"] is None
        return envelope(cmd, status="ok" if ok else "error", summary=text.splitlines()[0],
                        data={**r, "text": text}, error=None if ok else f"arrêt à #{r['stopped']['todo_id']}")
    if cmd == "after":
        actions = after_merge.reconcile(settings, args.todo_id)["actions"]
        requests = [{**a, "line": user_actions.action_line(a), "text": after_text(a, args.todo_id)} for a in actions]
        return envelope(cmd, summary=f"{len(actions)} action(s) après fusion",
                        data={"todo_id": args.todo_id, "requests": requests})
    if cmd == "after-exec":
        req = after_merge.execute(settings, args.todo_id, args.n)
        return envelope(cmd, status="ok" if req["status"] == "done" else "error", summary=user_actions.action_line(req),
                        data=req, error=req.get("error"))
    if cmd in ("after-run", "after-refuse"):
        by = None
        if cmd == "after-run":
            by = closing.authorize("user", after_merge.target(args.todo_id, args.n), human=args.human,
                                   what=f"Exécuter l'action #{args.n} prévue après la fusion de #{args.todo_id} ?",
                                   used=lambda proof: after_merge.proof_used(settings, args.todo_id, proof))
        req = after_merge.decide(settings, args.todo_id, args.n, approve=cmd == "after-run", by=by,
                                 launch=after_merge.launch_unit)
        ok = req["status"] in ("done", "refused")
        return envelope(cmd, status="ok" if ok else "error", summary=user_actions.action_line(req),
                        data={**req, "text": after_text(req, args.todo_id)}, error=None if ok else req.get("error"))
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
