"""Health alerts (todo #160): plain rules, no AI, silent while nothing changes.

`ultron-hub health alertes` runs after each `health collect --record`. It lists the current anomalies, compares
them with the ones already known (var/health/alertes.json) and sends ONE Telegram message only when the set
changes (new anomaly, or anomaly resolved). Thresholds: config/sante.yaml. The state is saved only once the
message went out, so a failed send is retried at the next run instead of being lost.

Known breakdowns (todo #161, health/reflexes.py) add their diagnosis as one more anomaly, `reflexe:<name>`: it comes and
goes with the message like the others, and var/health/reflexes.json keeps it for the /sante button.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store as journal
from ultron_hub.health import reflexes, seuils
from ultron_hub.health.collect import collect
from ultron_hub.todo import service

log = logging.getLogger("ultron_hub.health")

PROBES = ("services", "disks", "backup", "depot")


def state_path(root: Path) -> Path:
    return root / "var" / "health" / "alertes.json"


def _hours_since(iso: str | None, now: datetime) -> float | None:
    try:
        at = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (now - at).total_seconds() / 3600


def evaluer(metrics: dict[str, Any], cfg: dict[str, dict[str, float]] | None = None,
            now: datetime | None = None) -> dict[str, str]:
    """Current anomalies as {stable key: one-line text}. A probe that failed raises nothing and yields nothing."""
    th = (cfg or seuils.charger())["alertes"]
    now = now or datetime.now(timezone.utc)
    found: dict[str, str] = {}
    services = metrics.get("services") or {}
    for scope, names in (("système", services.get("failed_system") or []), ("utilisateur", services.get("failed_user") or [])):
        for name in names:
            found[f"unite:{scope}:{name}"] = f"unité en échec ({scope}) : {name}"
    for disk in metrics.get("disks") or []:
        if disk["used_pct"] > th["disque_pct"]:
            found[f"disque:{disk['mount']}"] = f"disque {disk['mount']} plein à {disk['used_pct']:.0f} % (seuil {th['disque_pct']:g} %)"
    backup = metrics.get("backup") or {}
    if backup.get("configured"):
        age = backup.get("last_success_age_h")
        if age is None:
            found["sauvegarde:ancienne"] = "aucune sauvegarde réussie enregistrée"
        elif age > th["sauvegarde_max_h"]:
            found["sauvegarde:ancienne"] = f"dernière sauvegarde réussie il y a {age:.0f} h (seuil {th['sauvegarde_max_h']:g} h)"
        if backup.get("last_run_status") == "error":
            found["sauvegarde:erreur"] = "la dernière sauvegarde est en erreur" + (
                f" : {str(backup['last_run_error'])[:150]}" if backup.get("last_run_error") else "")
        check_age = _hours_since(backup.get("last_check_at"), now)
        if check_age is None:
            found["check:ancien"] = "aucune vérification des sauvegardes (backup check) enregistrée"
        elif check_age > th["check_max_jours"] * 24:
            found["check:ancien"] = (f"dernier backup check il y a {check_age / 24:.0f} j "
                                     f"(seuil {th['check_max_jours']:g} j)")
        restore_age = _hours_since(backup.get("last_restore_at"), now)
        if backup.get("last_restore_status") == "error":
            found["restauration:echec"] = "le dernier test de restauration a échoué" + (
                f" : {str(backup['last_restore_error'])[:150]}" if backup.get("last_restore_error") else "")
        elif restore_age is not None and restore_age > th["restauration_max_jours"] * 24:
            found["restauration:ancien"] = (f"dernier test de restauration il y a {restore_age / 24:.0f} j "
                                            f"(seuil {th['restauration_max_jours']:g} j)")
    depot = metrics.get("depot") or {}
    if depot:
        age, behind = depot.get("last_push_age_h"), depot.get("behind")
        if age is None or age > th["depot_max_h"]:
            quand = "aucun push réussi enregistré" if age is None else f"dernier push réussi il y a {age:.0f} h"
            found["depot:push"] = f"push GitHub manquant : {quand} (seuil {th['depot_max_h']:g} h)" + (
                f", {behind} commit(s) de main absents d'origin/main" if behind else "")
    return found


def changements(known: dict[str, str], current: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """(new anomalies, resolved anomalies). An anomaly whose text only changed (age grows) is not news."""
    new = {k: v for k, v in current.items() if k not in known}
    resolved = {k: v for k, v in known.items() if k not in current}
    return new, resolved


def message(new: dict[str, str], resolved: dict[str, str], current: dict[str, str]) -> str:
    lines: list[str] = []
    if new:
        lines += ["🚨 Santé du serveur : " + ("nouvelle anomalie" if len(new) == 1 else "nouvelles anomalies")]
        lines += [f"• {text}" for text in new.values()]
    if resolved:
        lines += ["✅ Résolu"] if not lines else ["", "✅ Résolu"]
        lines += [f"• {text}" for text in resolved.values()]
    if not current:
        lines += ["", "Plus aucune anomalie."]
    elif resolved or new:
        rest = [t for k, t in current.items() if k not in new]
        if rest:
            lines += ["", "Toujours en cours :"] + [f"• {t}" for t in rest]
    return "\n".join(lines)


def read_state(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        active = data["actives"]
        return {str(k): str(v) for k, v in active.items()} if isinstance(active, dict) else {}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return {}


def write_state(path: Path, current: dict[str, str], now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated_at": now.isoformat(timespec="seconds"), "actives": current},
                              ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def messages_path(root: Path) -> Path:
    return root / "var" / "health" / "alertes-messages.json"


def _lire_messages(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, dict) and "message_id" in v and "chat" in v}
    except (OSError, ValueError, AttributeError):
        return {}


def _ecrire_messages(path: Path, data: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def _editer(chat: str, message_id: int, texte: str) -> bool:
    """Édite un message via `envoi.appeler("editMessageText")` (mode direct seulement). Ne lève jamais."""
    try:
        from ultron_hub import envoi
        if envoi.mode() != "direct":
            return False
        envoi.appeler("editMessageText", {"chat_id": chat, "message_id": message_id, "text": texte[:envoi.MAX_MESSAGE]})
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("editMessageText impossible : %s", exc)
        return False


def suivre_messages(root: Path, new: dict[str, str], resolved: dict[str, str], res: Any, texte: str, now: datetime,
                    edit: Callable[[str, int, str], bool] | None = None) -> None:
    """Mode direct (todo #208) : retient l'id du message d'alerte par clé ; une alerte réglée édite ce message
    (« ✅ réglée à HHhMM » + texte d'origine) quand plus aucune clé active ne s'y rattache. Jamais d'exception."""
    try:
        from ultron_hub import envoi
        path = messages_path(root)
        suivis = _lire_messages(path)
        edit = edit or _editer
        for cle in resolved:
            infos = suivis.pop(cle, None)
            if infos and not any(v["message_id"] == infos["message_id"] and v["chat"] == infos["chat"]
                                 for v in suivis.values()):
                edit(infos["chat"], infos["message_id"], f"✅ réglée à {now.astimezone():%Hh%M}\n\n{infos['texte']}")
        mid = getattr(res, "message_id", None)
        if mid is not None and new:
            for cle in new:
                suivis[cle] = {"message_id": mid, "chat": getattr(res, "chat", None), "texte": texte}
        _ecrire_messages(path, suivis)
    except Exception as exc:  # noqa: BLE001 - ne casse jamais le contrôle de santé
        log.warning("health alertes: suivi des messages en échec (%s: %s)", type(exc).__name__, exc)


def run_alertes(settings: Settings, *, metrics: dict[str, Any] | None = None, cfg: dict | None = None,
                now: datetime | None = None, send: Callable[[str], None] | None = None,
                diagnose: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    metrics = metrics if metrics is not None else collect(only=PROBES)
    current = evaluer(metrics, cfg, now)
    try:
        diags = (diagnose or reflexes.diagnostiquer)(metrics)
    except Exception as exc:  # a reflex never stops the alerts
        log.warning("health alertes: réflexes en échec (%s: %s)", type(exc).__name__, exc)
        diags = []
    reflexes.ecrire(settings.root, diags, now)
    for diag in diags:
        current[f"reflexe:{diag['nom']}"] = reflexes.anomalie(diag)
    path = state_path(settings.root)
    known = read_state(path)
    new, resolved = changements(known, current)
    result: dict[str, Any] = {"actives": current, "new": list(new), "resolved": list(resolved), "sent": False,
                              "diagnostics": diags,
                              "send_error": None, "probe_errors": metrics.get("errors") or {}}
    if not new and not resolved:
        if not path.exists():
            write_state(path, current, now)
        return result
    text = message(new, resolved, current)
    try:
        res = (send or (lambda t: service.send_message(t, settings.todo_notify_target, settings.hermes_binary,
                                                       sujet="alertes")))(text)
    except Exception as exc:  # state untouched: the same message is retried at the next run
        result["send_error"] = f"{type(exc).__name__}: {exc}"
        log.warning("health alertes: envoi échoué (%s)", result["send_error"])
        return result
    write_state(path, current, now)
    result["sent"] = True
    suivre_messages(settings.root, new, resolved, res, text, now)
    journal.record(settings, "ultron-hub", "sante",
                   f"Alertes santé : {len(new)} nouvelle(s), {len(resolved)} résolue(s), {len(current)} active(s)")
    return result
