"""Todo #257: a control compares the docs to the active services (Hermes « en service » while hermes-gateway is down)."""

import subprocess
from pathlib import Path

from ultron_hub.health import reflexes

REPO = Path(__file__).resolve().parents[1]
inspecter = reflexes.inspecter_doc_hermes  # the real one: conftest replaces it during each test


def _etat(valeur):
    return lambda cmd: subprocess.CompletedProcess(cmd, 0 if valeur == "active" else 3, valeur + "\n", "")


def test_flags_a_false_claim_when_the_gateway_is_inactive(tmp_path):
    doc = tmp_path / "serveur.md"
    doc.write_text("ok\nPourquoi : Hermes est en service ; on étudie.\n- Telegram = interface principale\n")
    res = inspecter(REPO, _etat("inactive"), [doc])
    assert res["ok"] and res["stale"] and "serveur.md:2" in res["reason"] and "serveur.md:3" in res["reason"]


def test_quiet_when_the_gateway_is_active_or_docs_are_clean(tmp_path):
    doc = tmp_path / "d.md"
    doc.write_text("Hermes est en service\n")
    assert not inspecter(REPO, _etat("active"), [doc])["stale"]
    doc.write_text("Hermes (hermes-gateway) est désactivé depuis le 18/10.\nHermes était le guichet.\n")
    assert not inspecter(REPO, _etat("inactive"), [doc])["stale"]


def test_unknown_state_or_error_gives_no_diagnosis(tmp_path):
    assert inspecter(REPO, lambda cmd: subprocess.CompletedProcess(cmd, 1, "", ""), [])["ok"] is False

    def boom(cmd):
        raise OSError("x")
    assert inspecter(REPO, boom, [])["ok"] is False


def test_diagnosis_and_configuration():
    stale = {"ok": True, "stale": True, "reason": "serveur.md:8"}
    diags = reflexes.diagnostiquer({}, [reflexes.DOC_HERMES_DEFAUT], inspect_doc=lambda: stale)
    assert [d["nom"] for d in diags] == ["doc-hermes"] and diags[0]["bouton"] is None
    assert reflexes.diagnostiquer({}, [reflexes.DOC_HERMES_DEFAUT],
                                  inspect_doc=lambda: {"ok": True, "stale": False}) == []
    assert any(r["nom"] == "doc-hermes" and r["verification"] == "doc_hermes" for r in reflexes.charger())
    assert reflexes.charger(REPO / "config" / "sante.yaml") == reflexes.DEFAUTS


def test_real_docs_do_not_conflict_now():
    """Live check on the corrected docs (skipped outside the server)."""
    import pytest
    if not (Path.home() / "notes" / "serveur.md").is_file():
        pytest.skip("pas sur le serveur")
    res = inspecter(REPO, _etat("inactive"))
    assert res["ok"] and not res["stale"], res["reason"]
