"""Tokens et coût lus dans les flux .jsonl de Claude Code (cout/usage_jsonl.py)."""

import json

from ultron_hub.cout import usage_jsonl


def test_consommation_cumulates_sessions(tmp_path):
    def line(**kw):
        return json.dumps(kw) + "\n"
    u = lambda i, o, cw, cr: {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": cw,  # noqa: E731
                              "cache_read_input_tokens": cr}
    (tmp_path / "session-1.jsonl").write_text(
        line(type="system") + line(type="assistant", message={"id": "m1", "usage": u(1, 2, 3, 4)})
        + line(type="assistant", message={"id": "m1", "usage": u(1, 2, 3, 4)})
        + line(type="result", total_cost_usd=0.5, usage=u(10, 20, 30, 40)) + "pas du json\n")
    (tmp_path / "session-2.jsonl").write_text(  # cut short: no result event
        line(type="assistant", message={"id": "a", "usage": u(5, 5, 0, 1)})
        + line(type="assistant", message={"id": "b", "usage": u(2, 1, 0, 0)}))
    total = usage_jsonl.consommation(tmp_path)
    assert total == {"entree": 17, "sortie": 26, "cache_ecriture": 30, "cache_lecture": 41, "cout_usd": 0.5,
                     "sessions": 2, "incompletes": 1}
    assert usage_jsonl.consommation(tmp_path / "vide")["sessions"] == 0
