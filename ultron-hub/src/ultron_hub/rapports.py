"""📋 Rapports du dimanche (todo #417): the last cap review, the proposed tidy-up and the health, on ONE card of Moi.

`rapports dimanche` (read only, no AI) gathers:
- cap: the last review (`cap last`, var/cap/last.json) and its corrections, each with the todo already made from it (a todo
  whose source is `cap:<date>` and whose title is the correction's, whatever its status: never created twice);
- menage: the pending proposal (`menage proposition`, menage_lot.py) with its fingerprint;
- sante: the active alerts (var/health/alertes.json) and the known breakdowns (var/health/reflexes.json). Reflexes are
  NEVER applied by the lot (risky): the card lists them and points to their own signed button.
`actions` = exactly what the lot does: one `todo` per correction not made yet, then `menage` if a proposal can be applied.
`empreinte` = 16 hex of the SHA-256 of one JSON line per action (sorted keys), null when there is nothing to do.

`rapports appliquer --empreinte X` recomputes everything (different fingerprint = refused, nothing done), needs the
`rapports-lot` token signed on that fingerprint (or `--i-am-human`), never replayed (proofs in var/rapports/appliques.jsonl),
then creates the todos (`todo add`'s own code: same checks, source `cap:<date>`) and applies the tidy-up
(`menage_lot.appliquer`: same checks, same trace + `via: rapports-lot`). Every action is tried; the result is one line each.

`rapports preparer` (todo #460, the one Sunday timer `ultron-hub-dimanche.timer`) runs the weekly reports without their own
`--send` (`menage report`, `cap report`, `bilan semaine`, then the focus question), each one even if another failed, writes
var/rapports/preparation.json and sends ONE message to the Fil: a title line, then one line per report (failure included).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ultron_hub import menage_lot
from ultron_hub.cap import report as cap_report
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store as journal
from ultron_hub.health import alertes, reflexes
from ultron_hub.todo import closing, service
from ultron_hub.todo.store import CLAIR_MAX, TodoStore

ACTION = "rapports-lot"
EMPREINTE_RE = re.compile(r"[0-9a-f]{16}")
VERDICTS = ("🟢", "🟠", "🔴")


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"rapports.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    top = sub.add_parser("rapports", help="Sunday reports: cap review, proposed tidy-up, health, applied by ONE signed ✅")
    cmds = top.add_subparsers(dest="rapports_command", required=True, parser_class=parser_class)
    cmds.add_parser("dimanche", help="the three reports and the exact actions of the lot, with their fingerprint (read only)")
    cmds.add_parser("preparer", help="run the weekly reports (menage, cap, bilan, focus question), each even if another "
                                      "failed, then send ONE message to the Fil (todo #460)")
    cmds.add_parser("appliquer-auto", help="apply the lot alone (by auto:bilan) if nothing is really deleted; refused from a job (todo #535)")
    app = cmds.add_parser("appliquer", help="create the cap todos and apply the tidy-up (signed rapports-lot on the fingerprint)")
    app.add_argument("--empreinte", required=True, help="fingerprint shown by `rapports dimanche`")
    app.add_argument("--i-am-human", dest="human", action="store_true",
                     help="confirm at a real terminal (typed code) instead of a signed button")


def folder(settings: Settings) -> Path:
    return settings.var_dir / "rapports"


def applied_path(settings: Settings) -> Path:
    return folder(settings) / "appliques.jsonl"


def todo_title(titre: str) -> str:
    """The title `todo add --truncate` gives this correction (what is compared to find it again)."""
    return service.resolve_title(titre, mode="truncate").title


def _cap_todos(settings: Settings, date: str) -> list[tuple[int, str]]:
    source = f"cap:{date}"
    with TodoStore(settings.todo_db) as store:
        return [(t.id, t.title) for t in store.list(status=None, limit=1_000_000) if t.source == source]


def _deja(todos: list[tuple[int, str]], titre: str) -> int | None:
    """The todo already made from this correction (from here, or from Telegram's /cap which cuts the title at 40)."""
    wanted = {" ".join(titre.split()), " ".join(titre[:40].split())}
    try:
        wanted.add(todo_title(titre))
    except InvalidRequestError:
        pass
    return next((tid for tid, title in todos if title in wanted), None)


def _cap(settings: Settings) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    last = cap_report.last(settings)
    if not last["exists"]:
        return {"date": None, "resume": "pas encore de revue de cap", "corrections": []}, []
    date = last["date"]
    todos = _cap_todos(settings, date)
    corrections, actions = [], []
    for c in last["corrections"]:
        tid = _deja(todos, c["titre"]) if c["titre"].strip() else None
        corrections.append({**c, "todo": tid})
        if tid is None and c["titre"].strip():
            actions.append({"type": "todo", "date": date, "titre": c["titre"], "chantier": c["chantier"],
                            "notes": c["notes"], "preuve": c["preuve"],
                            "texte": f"➕ Todo « {c['titre']} »" + (f" (chantier {c['chantier']})" if c["chantier"] else "")})
    verdicts = [line[:1] for line in last["text"].splitlines() if line[:1] in VERDICTS]
    counts = " ".join(f"{sum(v == x for v in verdicts)}{x}" for x in VERDICTS if x in verdicts)
    faites = sum(1 for c in corrections if c["todo"] is not None)
    resume = (f"{len(corrections)} correction(s)" + (f", {faites} déjà en todo" if faites else "")
              + (f" · {counts}" if counts else ""))
    return {"date": date, "resume": resume, "corrections": corrections}, actions


def _menage(settings: Settings) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    view = menage_lot.voir(settings)
    if not view["n"]:
        return {"date": None, "resume": "aucun ménage proposé", "empreinte": None, "n": 0, "bloquee": None,
                "cancel": [], "archiver": [], "corbeille": [], "dossiers": []}, []
    nc, na, ns, nd = len(view["cancel"]), len(view["archiver"]), len(view["corbeille"]), len(view["dossiers"])
    resume = (f"{nc} todo(s), {na} chantier(s)" + (f", {ns} skill(s)" if ns else "") + (f", {nd} dossier(s)" if nd else "")) + (f" · à refaire : {view['bloquee']}" if view["bloquee"] else "")
    data = {"date": view["cree_a"], "resume": resume, "empreinte": view["empreinte"], "n": view["n"],
            "bloquee": view["bloquee"], "par": view["par"],
            "cancel": [{"id": c["id"], "titre": c["titre"], "raison": c["raison"]} for c in view["cancel"]],
            "archiver": [a["sujet"] for a in view["archiver"]],
            "corbeille": [c["nom"] for c in view["corbeille"]], "dossiers": [d["chemin"] for d in view["dossiers"]]}
    if view["bloquee"] or not view["empreinte"]:
        return data, []
    detail = ", ".join([f"#{c['id']}" for c in view["cancel"]] + [f"chantier {a['sujet']}" for a in view["archiver"]]
                       + [f"skill {c['nom']}" for c in view["corbeille"]] + [f"dossier {d['chemin']}" for d in view["dossiers"]])
    return data, [{"type": "menage", "empreinte": view["empreinte"],
                   "texte": f"🧹 Appliquer le ménage proposé : abandonner/archiver {detail}"}]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _sante(settings: Settings) -> dict[str, Any]:
    state = _read_json(alertes.state_path(settings.root))
    actives = alertes.read_state(alertes.state_path(settings.root))
    pannes = [{"nom": d["nom"], "diagnostic": str(d.get("diagnostic") or ""), "detail": str(d.get("detail") or ""),
               "cible": str((d.get("bouton") or {}).get("cible") or "") if isinstance(d.get("bouton"), dict) else ""}
              for d in reflexes.lire(settings.root)]
    alerts = [{"cle": k, "texte": v} for k, v in sorted(actives.items()) if not k.startswith("reflexe:")]
    date = state.get("updated_at") or _read_json(reflexes.state_path(settings.root)).get("updated_at")
    if not alerts and not pannes:
        resume = "rien à signaler"
    else:
        resume = ", ".join(x for x in (f"{len(alerts)} alerte(s)" if alerts else "",
                                       f"{len(pannes)} panne(s) connue(s)" if pannes else "") if x)
    return {"date": date if isinstance(date, str) else None, "resume": resume, "alertes": alerts, "pannes": pannes}


def empreinte(actions: list[dict[str, Any]]) -> str | None:
    lines = [json.dumps({k: v for k, v in a.items() if k != "texte"}, sort_keys=True, ensure_ascii=False) for a in actions]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16] if lines else None


def dimanche(settings: Settings) -> dict[str, Any]:
    cap, cap_actions = _cap(settings)
    menage, menage_actions = _menage(settings)
    actions = cap_actions + menage_actions
    return {"cap": cap, "menage": menage, "sante": _sante(settings), "actions": actions, "empreinte": empreinte(actions)}


def proof_used(settings: Settings, proof: str) -> bool:
    try:
        lines = applied_path(settings).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        try:
            if json.loads(line).get("proof") == proof:
                return True
        except ValueError:
            continue
    return False


def _refuse(reason: str) -> None:
    raise InvalidRequestError(f"❌ Rapports refusés : {reason}\n\nRien n'a été fait. "
                              "À faire : rouvre la carte « Rapports du dimanche » (onglet Moi) pour voir l'état à jour.")


def _add_todo(settings: Settings, a: dict[str, Any]) -> str:
    from ultron_hub.todo import commands as todo_commands  # the same code (and checks) as `todo add`
    if _deja(_cap_todos(settings, a["date"]), a["titre"]) is not None:
        raise InvalidRequestError("todo déjà créée entre-temps")
    args = argparse.Namespace(todo_command="add", title=a["titre"], priority="normal", category=None, due=None,
                              notes=a["notes"], source=f"cap:{a['date']}", nature=None, chantier=a["chantier"] or None,
                              clair=" ".join(a["preuve"].split())[:CLAIR_MAX], title_mode="truncate")
    out = todo_commands.handle(args, settings)
    if out.get("status") != "ok":
        raise InvalidRequestError(out.get("error") or out.get("summary") or "todo add a échoué")
    return f"todo #{out['data']['id']} créée"


def appliquer(settings: Settings, expected: str, authorize: Callable[[dict[str, Any]], dict[str, Any]],
              garde: Callable[[dict[str, Any]], str | None] | None = None) -> dict[str, Any]:
    if EMPREINTE_RE.fullmatch(expected or "") is None:
        raise InvalidRequestError("empreinte invalide (16 caractères hexadécimaux)")
    folder(settings).mkdir(parents=True, exist_ok=True)
    with open(folder(settings) / "lot.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # two presses at once: the second sees the todos made and another fingerprint
        view = dimanche(settings)
        if not view["actions"]:
            _refuse("aucune action conseillée en attente (déjà appliquées ?)")
        if view["empreinte"] != expected:
            _refuse("les rapports ont changé depuis leur affichage")
        why = garde(view) if garde else None  # todo #535: the automatic path's own refusals, under the same lock
        if why:
            _refuse(why)
        by = authorize(view)
        if by.get("proof") and proof_used(settings, by["proof"]):
            raise closing.CloseRefused("jeton des rapports déjà utilisé")
        with applied_path(settings).open("a", encoding="utf-8") as fh:  # spent before anything runs
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": by.get("by"),
                                 "proof": by.get("proof"), "empreinte": expected, "actions": view["actions"]},
                                ensure_ascii=False) + "\n")
        bilan = []
        for a in view["actions"]:
            try:
                if a["type"] == "todo":
                    detail = _add_todo(settings, a)
                else:
                    res = menage_lot.appliquer(settings, a["empreinte"],
                                               lambda _v: {**by, "rapports": expected, "via_lot": ACTION})
                    detail = res["text"].splitlines()[0]
                    if res["arret"]:
                        raise InvalidRequestError(f"{detail} ; arrêt à {res['arret']['item']} : {res['arret']['raison']}")
                bilan.append({"action": a["texte"], "ok": True, "detail": detail})
            except Exception as exc:  # noqa: BLE001 - reported on its line, the other actions still run
                bilan.append({"action": a["texte"], "ok": False, "detail": str(exc)[:300]})
    ok = sum(b["ok"] for b in bilan)
    journal.record(settings, "ultron-hub", "rapports", f"Rapports du dimanche appliqués ({by.get('by')}) : {ok}/{len(bilan)}")
    return {"empreinte": expected, "bilan": bilan, "text": texte(bilan)}


def appliquer_auto(settings: Settings, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Apply the Sunday lot WITHOUT the user's ✅ when it deletes nothing for real (todo #535), by `auto:bilan`.

    The cap todos are plain additions; the tidy-up goes through `menage_lot` with the same bounded-only rule. A job or a
    squad, a real deletion in the proposal, or a fingerprint that changed between reading and applying = refused, nothing
    changed, the ✅ stays as before. The record (var/rapports/appliques.jsonl, by auto:bilan) is what `bilan soir` lists."""
    import os
    env = os.environ if env is None else env
    job = next((n for n in menage_lot.JOB_ENVS if env.get(n)), None)
    if job:
        return {"statut": "refuse", "raison": f"application automatique interdite depuis un job ({job})"}
    try:
        view = dimanche(settings)
        if not view["actions"]:
            return {"statut": "rien"}
        why = menage_lot.suppression_reelle(settings) if any(a["type"] == "menage" for a in view["actions"]) else None
        if why:
            return {"statut": "refuse", "raison": why, "empreinte": view["empreinte"]}
        res = appliquer(settings, view["empreinte"], lambda _v: {"by": menage_lot.AUTO_BY},
                        garde=lambda _v: menage_lot.suppression_reelle(settings))
    except InvalidRequestError as exc:
        return {"statut": "refuse", "raison": str(exc).splitlines()[0][:300]}
    return {"statut": "applique", **res}


def texte(bilan: list[dict[str, Any]]) -> str:
    lines = [f"📋 Actions conseillées appliquées : {sum(b['ok'] for b in bilan)}/{len(bilan)}"]
    lines += [f"{'✅' if b['ok'] else '❌'} {b['action']} : {b['detail']}" for b in bilan]
    return "\n".join(lines)


PREPARATION_TITRE = "📋 Rapports du dimanche prêts, onglet Moi"
LIGNE_MAX = 160


def _court(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= LIGNE_MAX else text[:LIGNE_MAX - 1].rstrip() + "…"


def _etape_menage(settings: Settings) -> tuple[bool, str]:
    from ultron_hub.menage import commands as menage_commands  # same code as `menage report` (without --send)
    out = menage_commands.handle(argparse.Namespace(menage_command="report", send=False), settings)
    return out["status"] == "ok", out["summary"] if out["status"] == "ok" else (out.get("error") or out["summary"])


def _etape_cap(settings: Settings) -> tuple[bool, str]:
    from ultron_hub.cap import commands as cap_commands  # same code as `cap report` (without --send)
    data = cap_commands.handle(argparse.Namespace(cap_command="report", send=False), settings)["data"]
    if data["status"] == "ok":
        return True, f"{len(data['corrections'])} correction(s) proposée(s)"
    return data["status"] == "skipped", data["summary"]


def _etape_bilan(settings: Settings) -> tuple[bool, str]:
    from ultron_hub.cap import commands as cap_commands  # same code as `bilan semaine` (without --send)
    out = cap_commands.handle_bilan(argparse.Namespace(bilan_command="semaine", send=False), settings)
    return out["status"] == "ok", out["summary"]


def _etape_focus(settings: Settings) -> tuple[bool, str]:
    from ultron_hub.esprit import focus
    current = focus.read(settings)
    actuel = " + ".join(current["domaines"]) if current else "aucun"
    return True, f"actuel : {actuel}. Sans réponse il reste ; le changer : Moi > 🧭 Focus. Quel focus cette semaine ?"


ETAPES: tuple[tuple[str, str, Callable[[Settings], tuple[bool, str]]], ...] = (
    ("menage", "🧹 Ménage", _etape_menage),
    ("cap", "🔭 Revue de cap", _etape_cap),
    ("bilan", "📊 Bilan de la semaine", _etape_bilan),
    ("focus", "🧭 Focus", _etape_focus),  # last: the message ends on the question (genre « question » in the Fil)
)


def preparer(settings: Settings, etapes=ETAPES, sender: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Every report, each even if another failed, then ONE message to the Fil. Never raises because of a report."""
    resultats = []
    for cle, libelle, etape in etapes:
        try:
            ok, detail = etape(settings)
        except Exception as exc:  # noqa: BLE001 - one broken report must not stop the others
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        resultats.append({"rapport": cle, "ok": bool(ok), "detail": _court(detail)})
    lignes = [PREPARATION_TITRE] + [f"{libelle} : {r['detail']}" if r["ok"] else f"{libelle} : ❌ échec : {r['detail']}"
                                    for (_c, libelle, _e), r in zip(etapes, resultats)]
    text = "\n".join(lignes)
    sender = sender or service.send_message
    try:
        sender(text, settings.todo_notify_target, settings.hermes_binary, sujet="revue-de-cap", genre="question")
        sent, send_error = True, None
    except Exception as exc:  # noqa: BLE001 - the reports are written; a failed send is reported
        sent, send_error = False, _court(f"{type(exc).__name__}: {exc}")
    data = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rapports": resultats, "text": text,
            "sent": sent, "send_error": send_error}
    try:
        folder(settings).mkdir(parents=True, exist_ok=True)
        tmp = folder(settings) / "preparation.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(folder(settings) / "preparation.json")
    except OSError:
        pass  # the message and the journal line still say what happened
    try:  # todo #535: a lot that deletes nothing for real is applied alone and listed by `bilan soir`; else the ✅ stays
        data["auto"] = appliquer_auto(settings)
        if data["auto"]["statut"] == "rien":  # no Sunday lot: a tidy-up proposal may still wait on its own
            data["auto"] = menage_lot.appliquer_auto(settings)
    except Exception as exc:  # noqa: BLE001 - never breaks the preparation
        data["auto"] = {"statut": "refuse", "raison": _court(f"{type(exc).__name__}: {exc}")}
    ok = sum(r["ok"] for r in resultats)
    journal.record(settings, "ultron-hub", "rapports", f"Rapports du dimanche préparés : {ok}/{len(resultats)}"
                   + (", message envoyé" if sent else ", envoi échoué"))
    return data


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.rapports_command
    if cmd == "preparer":
        data = preparer(settings)
        failed = [r for r in data["rapports"] if not r["ok"]]
        errors = [f"{r['rapport']} : {r['detail']}" for r in failed] + ([f"envoi : {data['send_error']}"] if not data["sent"] else [])
        return envelope(cmd, status="error" if errors else "ok",
                        summary=f"{len(data['rapports']) - len(failed)}/{len(data['rapports'])} rapport(s) prêt(s)"
                                + (", message envoyé" if data["sent"] else ", envoi échoué"),
                        data=data, error="; ".join(errors) or None)
    if cmd == "dimanche":
        data = dimanche(settings)
        n = len(data["actions"])
        return envelope(cmd, summary=f"{n} action(s) conseillée(s)" if n else "aucune action conseillée", data=data)
    if cmd == "appliquer-auto":
        data = appliquer_auto(settings)
        resume = {"rien": "aucune action conseillée", "refuse": "application automatique refusée",
                  "applique": (data.get("text") or "").split("\n")[0]}[data["statut"]]
        return envelope(cmd, status="error" if data["statut"] == "refuse" else "ok", summary=resume, data=data,
                        error=data.get("raison") if data["statut"] == "refuse" else None)
    if cmd == "appliquer":
        def authorize(view: dict[str, Any]) -> dict[str, Any]:
            what = "Appliquer :\n" + "\n".join(f"- {a['texte']}" for a in view["actions"]) + "\n?"
            return closing.authorize(ACTION, args.empreinte, human=args.human, what=what,
                                     used=lambda proof: proof_used(settings, proof))
        data = appliquer(settings, args.empreinte, authorize)
        failed = [b for b in data["bilan"] if not b["ok"]]
        return envelope(cmd, status="error" if failed else "ok", summary=data["text"].splitlines()[0], data=data,
                        error="; ".join(f"{b['action']} : {b['detail']}" for b in failed) or None)
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
