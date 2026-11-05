"""Weekly cap review, Opus pass (todo #190): Claude reads the facts of `cap collect` and judges, read only.

Spec ~/notes/spec-revue-cap.md section 3.2. Claude runs no tool: the facts go in `{metrics}` between markers, marked
as untrusted data (task `tasks/revue-cap/`). Nothing is applied. The answer is re-validated here (at most 3 corrections,
title of 40 characters at most, known chantier). `run` never raises: a skipped or failed review is a report too.
Files (0600, links never followed): var/cap/<date>.md and .json, and var/cap/last.json after a successful review only.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ultron_hub.cap import collect as cap_collect
from ultron_hub.common.logging import get_logger
from ultron_hub.core.job import JobRequest
from ultron_hub.core.runner import run_job
from ultron_hub.menage import audit

log = get_logger("cap.report")

QUOTA_MAX_PCT: float | None = None  # no default threshold (todo #424); set explicitly to skip the review
CORRECTIONS_MAX = 3
TITLE_MAX = 40
SUMMARY_MAX = 300
LINE_MAX = 300
NOTES_MAX = 800
PROOF_MAX = 200
PROOFS_MAX = 5
VERDICTS = {"vert": "🟢", "orange": "🟠", "rouge": "🔴"}
DASHES = re.compile(r"[—–]")


class BadAnswer(ValueError):
    """The structured output of Claude does not have the expected shape."""


def _clean(value: Any, limit: int) -> str:
    return DASHES.sub("-", str(value)).strip()[:limit]


def _str(raw: dict, key: str) -> str:
    if not isinstance(raw.get(key), str):
        raise BadAnswer(f"champ « {key} » absent ou invalide")
    return raw[key]


def check(data: Any, known: set[str]) -> tuple[str, list[dict[str, Any]], list[dict[str, str]], int]:
    """(summary, chantiers, corrections, number of corrections dropped). Structure errors raise BadAnswer.

    Corrections beyond 3 are cut; a title over 40 characters or an unknown chantier drops that correction."""
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str) \
            or not isinstance(data.get("chantiers"), list) or not isinstance(data.get("corrections"), list):
        raise BadAnswer("réponse structurée absente ou invalide")
    chantiers = []
    for raw in data["chantiers"]:
        if not isinstance(raw, dict):
            raise BadAnswer("chantier invalide")
        proofs = raw.get("preuves")
        if raw.get("verdict") not in VERDICTS or not isinstance(proofs, list) or not proofs \
                or not all(isinstance(p, str) and p.strip() for p in proofs):
            raise BadAnswer(f"chantier « {raw.get('nom')} » : verdict ou preuves invalides")
        chantiers.append({"nom": _clean(_str(raw, "nom"), 80), "verdict": raw["verdict"],
                          "objectif": _clean(_str(raw, "objectif"), LINE_MAX),
                          "technique": _clean(_str(raw, "technique"), LINE_MAX),
                          "derives": _clean(_str(raw, "derives"), LINE_MAX),
                          "preuves": [_clean(p, PROOF_MAX) for p in proofs[:PROOFS_MAX]]})
    corrections: list[dict[str, str]] = []
    dropped = 0
    for raw in data["corrections"][:CORRECTIONS_MAX]:
        ok = isinstance(raw, dict) and all(isinstance(raw.get(k), str) and raw[k].strip()
                                           for k in ("titre", "chantier", "notes", "preuve"))
        if not ok or len(raw["titre"].strip()) > TITLE_MAX or raw["chantier"] not in known:
            dropped += 1
            continue
        corrections.append({"titre": _clean(raw["titre"], TITLE_MAX), "chantier": raw["chantier"],
                            "notes": _clean(raw["notes"], NOTES_MAX), "preuve": _clean(raw["preuve"], PROOF_MAX)})
    return _clean(data["summary"], SUMMARY_MAX), chantiers, corrections, dropped


# --- quota ----------------------------------------------------------------------------------------------------------

def week_percent(settings: Any) -> float | None:
    """Week usage in % from the existing usage module; None when it cannot be read (the review then goes on)."""
    try:
        from ultron_hub.usage.collect import SQUAD_ENV, squad_quota, usage
        now = datetime.now(timezone.utc)
        folder = os.environ.get(SQUAD_ENV)
        result = (squad_quota(Path(folder), now) if folder else None) \
            or usage(Path(settings.var_dir) / "cache" / "usage.json", now)
        return float(result["week"]["percent"])
    except Exception:  # noqa: BLE001 - usage unreadable: unknown, not a reason to skip
        return None


# --- output ---------------------------------------------------------------------------------------------------------

def facts_summary(facts: dict[str, Any] | None) -> str:
    """The raw facts in one line, for the fallback report."""
    if not facts:
        return "faits indisponibles"
    todos = [sum(len(v) for v in (c.get("todos") or {}).values()) for c in facts.get("chantiers", [])]
    echecs = facts.get("echecs") or {}
    menage = (echecs.get("menage") or {}).get("total", 0)
    return (f"{len(facts.get('chantiers', []))} chantier(s), {sum(todos)} todo(s) de la semaine, "
            f"{len(facts.get('commits', []))} commit(s), "
            f"{menage} anomalie(s) de ménage, {len(echecs.get('reflexes', []))} diagnostic(s) du veilleur")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Revue de cap du {report['date']}", "", report["summary"], ""]
    cost, pct = report.get("cost_usd"), report.get("semaine_pct")
    lines += [f"Coût : {'%.2f $' % cost if cost is not None else 'n/a'} ; semaine : "
              f"{'%.0f %%' % pct if pct is not None else 'n/a'}.", "",
              "Rien n'est modifié ni lancé par la revue.", ""]
    if report["status"] != "ok":
        lines += [f"Faits de la semaine : {report['faits']}.", ""]
    for c in report["chantiers"]:
        lines += [f"## {VERDICTS[c['verdict']]} {c['nom']}", "", f"Objectif : {c['objectif']}", "",
                  f"Technique : {c['technique']}", "", f"Dérives : {c['derives']}", "", "Preuves :"]
        lines += [f"- {p}" for p in c["preuves"]] + [""]
    if report["status"] == "ok":
        lines.append("## Corrections proposées" if report["corrections"] else "Aucune correction proposée.")
        lines.append("")
    for n, k in enumerate(report["corrections"], 1):
        lines += [f"{n}. **{k['titre']}** ({k['chantier']})", "", f"   {k['notes']}", "", f"   Preuve : {k['preuve']}", ""]
    return "\n".join(lines).rstrip() + "\n"


def _write_private(path: Path, text: str) -> None:
    audit._write_private(path, text)


def _store(settings: Any, report: dict[str, Any]) -> str | None:
    folder = Path(settings.var_dir) / "cap"
    try:
        payload = json.dumps(report, ensure_ascii=False, indent=1) + "\n"
        _write_private(folder / f"{report['date']}.md", render_markdown(report))
        _write_private(folder / f"{report['date']}.json", payload)
        if report["status"] == "ok":
            _write_private(folder / "last.json", payload)
        return str(folder / f"{report['date']}.md")
    except OSError as exc:
        log.warning("cap report: not written (%s)", type(exc).__name__)
        return None


def last(settings: Any) -> dict[str, Any]:
    """Last successful review (`var/cap/last.json`) for /cap: its Telegram text and its corrections. Read only."""
    path = Path(settings.var_dir) / "cap" / "last.json"
    try:
        if path.is_symlink():
            raise OSError("symlink")
        report = json.loads(path.read_text(encoding="utf-8"))
        text = short_text(report)
        corrections = [{k: str(c.get(k) or "") for k in ("titre", "chantier", "notes", "preuve")}
                       for c in report["corrections"][:CORRECTIONS_MAX]]
        return {"exists": True, "date": str(report["date"]), "text": text, "corrections": corrections}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"exists": False, "date": "", "text": "", "corrections": []}


# --- Telegram text (todo #191) -----------------------------------------------------------------------------------

TEXT_MAX_LINES = 25
REASON_MAX = 80
CAP_TAIL = "👉 /cap pour créer les todos"


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _cost_line(report: dict[str, Any]) -> str:
    cost, pct = report.get("cost_usd"), report.get("semaine_pct")
    return (f"💰 {'%.2f $' % cost if cost is not None else 'coût n/a'} · semaine "
            f"{'%.0f %%' % pct if pct is not None else 'n/a'}")


def short_text(report: dict[str, Any]) -> str:
    """Telegram text, at most TEXT_MAX_LINES lines: one line per chantier, numbered corrections, cost, /cap."""
    lines = [f"🧭 Revue de cap du {report['date']}"]
    if report["status"] != "ok":
        lines += [_short(report["summary"], 200), _short(report.get("faits") or "", 200), _cost_line(report)]
        return "\n".join(x for x in lines if x)
    corrections = report["corrections"][:CORRECTIONS_MAX]
    tail = [_cost_line(report), CAP_TAIL] if corrections else [_cost_line(report), "Aucune correction proposée."]
    room = TEXT_MAX_LINES - len(lines) - len(tail) - (len(corrections) + 1 if corrections else 0)
    chantiers = report["chantiers"]
    shown = chantiers if len(chantiers) <= room else chantiers[:max(room - 1, 0)]
    for c in shown:
        reason = c["derives"] if c["verdict"] != "vert" else c["objectif"]
        lines.append(f"{VERDICTS[c['verdict']]} {c['nom']} : {_short(reason, REASON_MAX)}")
    if len(shown) < len(chantiers):
        lines.append(f"… +{len(chantiers) - len(shown)} autre(s) chantier(s) : voir var/cap/{report['date']}.md")
    if corrections:
        lines.append("Corrections :")
        lines += [f"{n}. {k['titre']} ({k['chantier']})" for n, k in enumerate(corrections, 1)]
    return "\n".join(lines + tail)


def send(settings: Any, report: dict[str, Any], sender: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Send the short text on Telegram (same path as `menage report --send`) and log one line. Never raises."""
    from ultron_hub.esprit import store as journal
    from ultron_hub.todo import service
    text = short_text(report)
    sender = sender or service.send_message
    sent, error = False, None
    try:
        sender(text, settings.todo_notify_target, settings.hermes_binary, sujet="revue-de-cap")
        sent = True
    except Exception as exc:  # noqa: BLE001 - the review is stored; a failed send is reported, not fatal
        error = f"{type(exc).__name__}: {_clean(exc, 160)}"
        log.warning("cap report: send failed (%s)", type(exc).__name__)
    try:
        journal.record(settings, "ultron-hub", "cap",
                       f"Revue de cap {report['date']} : {report['status']}, {len(report['corrections'])} correction(s)"
                       + (", envoyée" if sent else ", envoi échoué"))
    except Exception:  # noqa: BLE001
        log.warning("cap report: journal line skipped", exc_info=True)
    return {"text": text, "sent": sent, "send_error": error}


# --- run ------------------------------------------------------------------------------------------------------------

def run(settings: Any, home: str | None = None, now: float | None = None, job: Callable[..., Any] | None = None,
        week: Callable[[Any], float | None] | None = None) -> dict[str, Any]:
    """Quota guard, facts, one Opus call, validation, files. Never raises; `status` = ok | skipped | failed."""
    now = time.time() if now is None else now
    job, week = job or run_job, week or week_percent
    try:
        day = datetime.fromtimestamp(now, ZoneInfo(settings.todo_timezone)).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    report: dict[str, Any] = {"date": day, "status": "failed", "summary": "", "chantiers": [], "corrections": [],
                              "cost_usd": None, "semaine_pct": None, "faits": "faits indisponibles"}
    facts: dict[str, Any] | None = None
    try:
        report["semaine_pct"] = pct = week(settings)
        if pct is not None and QUOTA_MAX_PCT is not None and pct >= QUOTA_MAX_PCT:
            report.update(status="skipped", summary=f"revue sautée (quota) : semaine à {pct:.0f} %")
        else:
            facts = cap_collect.collect(settings, home=home, now=now)
            report["faits"] = facts_summary(facts)
            result = job(JobRequest(action="revue-cap", params={"metrics": json.dumps(facts, ensure_ascii=False)}),
                         settings)
            report["cost_usd"] = getattr(result, "cost_usd", None)
            if not result.ok:
                raise BadAnswer(_clean(result.error or "échec de Claude", 200))
            known = {c["nom"] for c in facts["chantiers"] if isinstance(c.get("nom"), str)}
            summary, chantiers, corrections, dropped = check(result.data, known)
            if dropped:
                log.warning("cap report: %d correction(s) dropped (long title or unknown chantier)", dropped)
            report.update(status="ok", summary=summary, chantiers=chantiers, corrections=corrections)
    except Exception as exc:  # noqa: BLE001 - a failed review is a report, never a crash
        reason = _clean(exc, 200) if isinstance(exc, BadAnswer) else f"{type(exc).__name__}: {_clean(exc, 160)}"
        report.update(status="failed", summary=f"revue impossible : {reason}", chantiers=[], corrections=[])
    report["path"] = _store(settings, report)
    return report
