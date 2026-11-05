"""`ultron-hub prompt essai <chantier>` (todo #232): replay rule scenarios against the assembled prompt.

Each scenario is answered by tasks/prompt-essai (fixed model, no tool) and compared by script, no AI judge.
"""

from pathlib import Path
from typing import Any, Callable

import yaml

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings, project_root
from ultron_hub.core.job import JobRequest
from ultron_hub.core.runner import run_job
from ultron_hub.escouade import core

SCENARIOS_FILE = "prompt-essais.yaml"


def load_scenarios(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or project_root() / "config" / SCENARIOS_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidRequestError(f"scenarios unreadable ({path.name}): {exc}") from exc
    items = raw.get("scenarios") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise InvalidRequestError(f"{path.name}: expected a non-empty 'scenarios' list")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise InvalidRequestError(f"{path.name}: scenario #{i + 1} is not a mapping")
        nom, situation = item.get("nom"), item.get("situation")
        if not isinstance(nom, str) or not nom or not isinstance(situation, str) or not situation.strip():
            raise InvalidRequestError(f"{path.name}: scenario #{i + 1} needs 'nom' and 'situation'")
        lists: dict[str, list[str]] = {}
        for key, required in (("attendu", True), ("interdit", False)):
            val = item.get(key, [])
            if not isinstance(val, list) or not all(isinstance(v, str) and v for v in val) or (required and not val):
                raise InvalidRequestError(f"{path.name}: scenario {nom!r}: '{key}' must be a list of strings")
            lists[key] = val
        out.append({"nom": nom, "situation": situation, **lists})
    return out


def check(reply: str, scenario: dict[str, Any]) -> dict[str, Any]:
    low = reply.lower()
    found = any(w.lower() in low for w in scenario["attendu"])
    forbidden = [w for w in scenario.get("interdit", []) if w.lower() in low]
    return {"ok": found and not forbidden, "manquants": [] if found else list(scenario["attendu"]),
            "interdits_trouves": forbidden}


def run_essai(chantier: str | None, settings: Settings, *, scenario: str | None = None, a_blanc: bool = False,
              dossier: str | None = None, scenarios_path: Path | None = None,
              run: Callable[..., Any] = run_job) -> dict[str, Any]:
    scenarios = load_scenarios(scenarios_path)
    if scenario:
        scenarios = [s for s in scenarios if s["nom"] == scenario]
        if not scenarios:
            raise InvalidRequestError(f"unknown scenario: {scenario!r}")
    prompt = core.build_interactif(chantier, dossier=dossier)
    if a_blanc:
        return {"ok": True, "a_blanc": True, "chantier": chantier, "scenarios": len(scenarios),
                "octets_prompt": len(prompt.encode())}
    results, total = [], 0.0
    for sc in scenarios:
        res = run(JobRequest(action="prompt-essai", params={"prompt": prompt, "situation": sc["situation"]}), settings)
        cost = getattr(res, "cost_usd", None)
        total += cost or 0.0
        reply = res.data.get("reponse") if res.ok and isinstance(res.data, dict) else None
        if not isinstance(reply, str):
            results.append({"nom": sc["nom"], "ok": False, "manquants": sc["attendu"], "interdits_trouves": [],
                            "cout_usd": cost, "erreur": str(getattr(res, "error", None) or "no answer")})
            continue
        results.append({"nom": sc["nom"], **check(reply, sc), "cout_usd": cost})
    return {"ok": all(r["ok"] for r in results), "chantier": chantier, "scenarios": results,
            "cout_total_usd": round(total, 6)}
