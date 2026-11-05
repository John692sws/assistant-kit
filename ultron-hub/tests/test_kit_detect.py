import json

import pytest

from ultron_hub.cli import main
from ultron_hub.kit import detect

SAMPLES = {
    "home-en-dur": "path = '/home/" + "alice/x'",
    "ip-tailscale": "host = 100." + "101.5.9",
    "tailnet-ts-net": "url = box.tail1234." + "ts.net",
    "adresse-mail": "mail = bob" + "@societe.fr",
    "jeton-telegram": "token = 987654321" + ":" + "A" * 35,
}


@pytest.mark.parametrize("rule", sorted(SAMPLES))
def test_each_rule_finds_its_case(tmp_path, rule):
    (tmp_path / "a.txt").write_text("ok\n" + SAMPLES[rule] + "\n")
    assert detect.scan(tmp_path, ["zzz"]) == [detect.Finding("a.txt", 2, rule)]


def test_cgnat_range_string_is_not_flagged(tmp_path):
    (tmp_path / "a.md").write_text("Tailscale uses 100.64.0.0/10 addresses\n")
    assert detect.scan(tmp_path, ["zzz"]) == []


def test_ip_outside_range_is_not_flagged(tmp_path):
    (tmp_path / "a.md").write_text("100.63.1.1 and 100.128.0.1\n")
    assert detect.scan(tmp_path, ["zzz"]) == []


def test_personal_pattern_case_insensitive(tmp_path):
    (tmp_path / "a.txt").write_text("hello MyCompany\n")
    assert detect.scan(tmp_path, ["mycompany"]) == [detect.Finding("a.txt", 1, "interdit-perso")]


def test_forbidden_names(tmp_path):
    for d in ("var/runs", "config"):
        (tmp_path / d).mkdir(parents=True)
    for f in ("x.db", "k.key", "c.pem", "p.pass", ".env", "auth.json", "credentials.json", "config/dashboard.yaml",
              "config/groupe.yaml", "var/runs/inner.txt"):
        (tmp_path / f).write_text("x")
    (tmp_path / "config" / "other.yaml").write_text("x")
    paths = sorted(f.path for f in detect.scan(tmp_path, ["zzz"]))
    assert paths == sorted([".env", "auth.json", "c.pem", "config/dashboard.yaml", "config/groupe.yaml",
                            "credentials.json", "k.key", "p.pass", "var", "x.db"])


def test_binary_and_skipped_dirs_ignored(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\0" + SAMPLES["adresse-mail"].encode())
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "c").write_text(SAMPLES["adresse-mail"])
    assert detect.scan(tmp_path, ["zzz"]) == []


def test_missing_list_is_refused(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("x")
    code = main(["kit", "scan", str(tmp_path), "--interdits", str(tmp_path / "absent.txt")])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["status"] == "error" and out["data"] is None
    with pytest.raises(detect.RefusedError):
        detect.load_interdits(tmp_path / "absent.txt")


def test_load_interdits_comments(tmp_path):
    f = tmp_path / "i.txt"
    f.write_text("# c\n\n  Foo Bar \nbaz\n")
    assert detect.load_interdits(f) == ["foo bar", "baz"]


def test_cli_output_has_no_value(tmp_path, capsys):
    (tmp_path / "a.txt").write_text(SAMPLES["adresse-mail"] + "\nsecretword here\n" + SAMPLES["jeton-telegram"] + "\n")
    lst = tmp_path / "i.txt"
    lst.write_text("secretword\n")
    target = tmp_path / "t"
    target.mkdir()
    (target / "a.txt").write_text((tmp_path / "a.txt").read_text())
    code = main(["kit", "scan", str(target), "--interdits", str(lst)])
    raw = capsys.readouterr().out
    out = json.loads(raw)
    assert code == 1 and out["data"]["count"] == 3
    for leaked in ("bob", "secretword", "societe.fr", "AAAA"):
        assert leaked not in raw
    assert set(out["data"]["findings"][0]) == {"path", "line", "rule"}


def test_cli_clean_folder_exit_0(tmp_path, capsys):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "a.txt").write_text("nothing\n")
    (tmp_path / "i.txt").write_text("secretword\n")
    code = main(["kit", "scan", str(tmp_path / "t"), "--interdits", str(tmp_path / "i.txt")])
    assert code == 0 and json.loads(capsys.readouterr().out)["data"]["count"] == 0


def test_fictive_test_data_is_tolerated(tmp_path):
    (tmp_path / "a.md").write_text(
        "alice@example.com 100.64.0.1 serveur-test.exemple.ts.net /home/exemple /home/exemple2/x\n"
        "token 123456789:" + "A" * 35 + " https://u:pw@www.example.com/x\n")
    assert detect.scan(tmp_path, ["zzz"]) == []


def test_fictive_lookalikes_are_still_flagged(tmp_path):
    (tmp_path / "a.md").write_text("100.64.0." + "10\n")
    (tmp_path / "b.md").write_text("autre.ts" + ".net\n")
    (tmp_path / "c.md").write_text("bob" + "@example.com.evil.io\n")
    assert {f.path for f in detect.scan(tmp_path, ["zzz"])} == {"a.md", "b.md", "c.md"}
