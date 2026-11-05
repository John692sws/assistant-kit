"""Weekly tidy-up report: short Telegram text, full markdown, state files. Never deletes or moves anything."""

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_LINES = 20
AI_INPUT_MAX_BYTES = 3000
HISTORY_MAX_LINES = 200

CHECK_LABELS = {
    1: "dossier attendu absent", 2: "dossier non déclaré", 3: "fichier en vrac", 4: "projet inactif",
    5: "worktree hors convention", 6: "worktree d'une todo close", 7: "reste de fusion d'essai",
    8: "fichier jetable", 9: "__pycache__ orphelin", 10: "vieux dossier de job", 11: "journal trop vieux ou gros",
    12: "doublon", 13: "disque", 14: "croissance", 15: "cache trop gros", 16: "code testé hors dépôt",
    17: "service hors dépôt", 18: "plugin Hermes copié", 19: "fichier vivant trop gros", 20: "sujet d'escouade incomplet",
}


def ai_input(anomalies: list[dict[str, Any]], limit: int = AI_INPUT_MAX_BYTES) -> str:
    """Compact JSON of the anomalies for the AI summary, cut to `limit` bytes on whole items."""
    items: list[dict[str, Any]] = []
    for a in anomalies:
        item = {"n": a.get("check"), "quoi": CHECK_LABELS.get(a.get("check"), ""), "path": a.get("path", ""),
                "detail": str(a.get("detail", ""))[:120], "action": str(a.get("action", ""))[:160]}
        candidate = json.dumps({"total": len(anomalies), "anomalies": items + [item]}, ensure_ascii=False)
        if len(candidate.encode("utf-8")) > limit:
            break
        items.append(item)
    return json.dumps({"total": len(anomalies), "anomalies": items}, ensure_ascii=False)


def _disk_line(disks: list[dict[str, Any]]) -> str:
    if not disks:
        return "disque : inconnu"
    worst = max(disks, key=lambda d: d.get("used_pct") or 0)
    return f"disque {worst.get('used_pct')} % ({worst.get('free_gib')} Gio libres)"


def _counts_line(counts: dict[str, int]) -> list[str]:
    rows = sorted(((int(k), v) for k, v in counts.items() if v), key=lambda kv: -kv[1])
    return [f"• {v} × {CHECK_LABELS.get(k, k)}" for k, v in rows]


AUDIT_LINE = "🔍 Lancer l'audit de la carte de rangement ? (tous les 60 jours) : /menage"


def short_text(scan: dict[str, Any], summary: dict[str, Any] | None, audit_due: bool = False) -> str:
    """Telegram text, at most MAX_LINES lines: ✅ what is fine, ⚠️ what is off, 👉 proposed actions."""
    anomalies = scan.get("anomalies") or []
    counts = scan.get("counts") or {}
    ok_checks = len(CHECK_LABELS) - sum(1 for v in counts.values() if v)
    lines = ["🧹 Ménage du serveur", f"✅ {ok_checks}/{len(CHECK_LABELS)} vérifications sans écart · {_disk_line(scan.get('disk') or [])}"]
    tail = [AUDIT_LINE] if audit_due else []
    if not anomalies:
        lines.append("Tout est rangé, rien à faire.")
        return "\n".join(lines + tail)
    tail.append("Détail : var/menage/last.md · rien n'est supprimé sans toi.")
    lines.append(f"⚠️ {sum(counts.values()) or len(anomalies)} écart(s) :")
    lines += _counts_line(counts)[:6]
    if summary:
        lines.append(summary.get("summary", "").strip())
        for i, act in enumerate(summary.get("actions") or [], 1):
            lines.append(f"👉 {i}. {act.get('quoi', '')} : {act.get('ou', '')}")
    else:
        for i, a in enumerate(anomalies[:5], 1):
            lines.append(f"👉 {i}. {a.get('action', '')} ({a.get('path', '')})")
    lines = [x for x in lines if x][:MAX_LINES - len(tail)]
    return "\n".join(lines + tail)


def full_markdown(scan: dict[str, Any], summary: dict[str, Any] | None, when: str) -> str:
    out = [f"# Ménage du serveur ({when})", "", _disk_line(scan.get("disk") or []), ""]
    if summary:
        out += ["## Résumé", summary.get("summary", ""), ""]
        for i, act in enumerate(summary.get("actions") or [], 1):
            out.append(f"{i}. **{act.get('quoi', '')}** : `{act.get('ou', '')}`. {act.get('pourquoi', '')}")
            if act.get("commande"):
                out.append(f"   `{act['commande']}`")
        out.append("")
    by_check: dict[int, list[dict[str, Any]]] = {}
    for a in scan.get("anomalies") or []:
        by_check.setdefault(a.get("check"), []).append(a)
    for n in sorted(by_check):
        out.append(f"## {n}. {CHECK_LABELS.get(n, '')} ({len(by_check[n])})")
        for a in by_check[n]:
            out.append(f"- `{a.get('path', '')}` : {a.get('detail', '')}")
            if a.get("action"):
                out.append(f"  - proposé : `{a['action']}`")
        out.append("")
    if scan.get("truncated"):
        out.append(f"Listes coupées à 50 par vérification : {scan['truncated']}")
    if scan.get("warnings"):
        out += ["## Avertissements du scan"] + [f"- {w}" for w in scan["warnings"]]
    return "\n".join(out).rstrip() + "\n"


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def save_state(state_dir: Path, scan: dict[str, Any], markdown: str, now: float | None = None) -> None:
    """Write last.json, last.md and append the measured sizes to history.jsonl (bounded)."""
    now = time.time() if now is None else now
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write(state_dir / "last.json", json.dumps(scan, ensure_ascii=False, indent=2))
    _write(state_dir / "last.md", markdown)
    history = state_dir / "history.jsonl"
    lines = history.read_text(encoding="utf-8").splitlines() if history.is_file() else []
    lines.append(json.dumps({"ts": now, "sizes": scan.get("sizes") or {}}, ensure_ascii=False))
    _write(history, "\n".join(lines[-HISTORY_MAX_LINES:]) + "\n")


def build(scan: dict[str, Any], summarize: Callable[[str], dict[str, Any]], when: str,
          audit_due: bool = False) -> dict[str, Any]:
    """Compose the report. `summarize` (the AI) is called only when there are anomalies; best effort."""
    summary, ai_error = None, None
    if scan.get("anomalies"):
        try:
            summary = summarize(ai_input(scan["anomalies"]))
        except Exception as exc:  # the report never fails because of the AI
            ai_error = f"{type(exc).__name__}: {exc}"
    return {"text": short_text(scan, summary, audit_due), "markdown": full_markdown(scan, summary, when),
            "summary": summary, "ai_called": bool(scan.get("anomalies")), "ai_error": ai_error}
