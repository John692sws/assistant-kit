"""Dashboard over HTTPS (todo #239): TLS served by the server itself, same whois gate, HTTP fallback, `dashboard cert`.

No network: a fake `tailscale` and a self-signed certificate made by openssl in tmp_path.
"""

import http.client
import json
import shutil
import socket
import ssl
import subprocess
import sys
import threading

import pytest

from ultron_hub.dashboard import auth, commands, server, tls, views

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl needed to make a test certificate")

SELF_ID, PHONE_ID, STRANGER_ID = "nSELF", "nPHONE", "nSTRANGER"
DEVICES = (auth.Device("telephone-test", PHONE_ID), auth.Device("serveur-test", SELF_ID))
DOMAIN = "serveur-test.exemple.ts.net"


def make_cert(directory, cn="localhost"):
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / tls.CERT, directory / tls.KEY
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", f"/CN={cn}",
                    "-addext", f"subjectAltName=DNS:{cn},IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    key.chmod(0o600)
    return cert, key


@pytest.fixture
def certs(tmp_path):
    d = tmp_path / "dashboard-tls"
    make_cert(d)
    return d


def start(whois, ctx):
    gate = auth.Gate(DEVICES, SELF_ID, "100.64.0.1", whois=whois)
    srv = server.make_server("127.0.0.1", 0, gate, tls_ctx=ctx)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def https(srv, cert, method, path, headers=None, body=None):
    ctx = ssl.create_default_context(cafile=str(cert))
    conn = http.client.HTTPSConnection("127.0.0.1", srv.server_address[1], context=ctx, timeout=10)
    conn.request(method, path, body=body, headers={"Host": f"127.0.0.1:{srv.server_address[1]}", **(headers or {})})
    r = conn.getresponse()
    out = r.status, r.read(), r.getheader("Set-Cookie")
    conn.close()
    return out


def test_tls_is_served_with_secure_cookie_and_https_origin(certs, monkeypatch):
    monkeypatch.setattr(views, "state", lambda: {"ok": True})
    ctx, why = tls.context(certs)
    assert ctx is not None and why == ""
    srv = start(lambda ip: PHONE_ID, ctx)
    try:
        assert srv.scheme == "https"
        code, _, cookie = https(srv, certs / tls.CERT, "GET", "/")
        assert code == 200 and "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert https(srv, certs / tls.CERT, "GET", "/api/state")[:2] == (200, b'{"ok": true}')
        sid = cookie.split(";")[0].split("=", 1)[1]
        host = f"127.0.0.1:{srv.server_address[1]}"
        body = json.dumps({"action": "stop", "id": "x"}).encode()
        base = {"Cookie": f"{server.COOKIE}={sid}", "Content-Type": "application/json"}
        code, raw, _ = https(srv, certs / tls.CERT, "POST", "/api/action/preparer", {**base, "Origin": f"http://{host}"}, body)
        assert code == 403 and "origine" in json.loads(raw)["erreur"]  # the http origin no longer counts
        code, raw, _ = https(srv, certs / tls.CERT, "POST", "/api/action/preparer", {**base, "Origin": f"https://{host}"}, body)
        assert code != 403  # past the Origin check (the action itself is judged by actions.py)
    finally:
        srv.shutdown()
        srv.server_close()


def test_device_outside_the_list_and_the_server_itself_get_403_over_https(certs, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a command ran for a refused caller")
    monkeypatch.setattr(views, "state", boom)
    ctx, _ = tls.context(certs)
    for node in (STRANGER_ID, SELF_ID, ""):
        srv = start(lambda ip, node=node: node, ctx)
        try:
            for path, method in (("/", "GET"), ("/api/state", "GET"), ("/api/action/preparer", "POST")):
                code, _, cookie = https(srv, certs / tls.CERT, method, path)
                assert code == 403 and cookie is None, (node, path)
        finally:
            srv.shutdown()
            srv.server_close()


def test_plain_http_on_the_tls_port_gets_nothing_and_the_server_keeps_serving(certs, monkeypatch):
    monkeypatch.setattr(views, "state", lambda: {"ok": True})
    ctx, _ = tls.context(certs)
    srv = start(lambda ip: PHONE_ID, ctx)
    try:
        with socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5) as s:
            s.sendall(b"GET /api/state HTTP/1.0\r\n\r\n")
            assert b'"ok"' not in s.recv(4096)
        assert https(srv, certs / tls.CERT, "GET", "/api/state")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_without_certificate_the_dashboard_falls_back_to_http(tmp_path, monkeypatch, caplog):
    ctx, why = tls.context(tmp_path / "absent")
    assert ctx is None and "pas de certificat" in why
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / tls.CERT).write_text("not a certificate")
    (broken / tls.KEY).write_text("not a key")
    assert tls.context(broken)[0] is None
    # serve(): the warning is logged and the server is plain HTTP, with the same gate
    monkeypatch.setattr(auth, "load_devices", lambda p: DEVICES)
    monkeypatch.setattr(auth, "tailscale_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(auth, "self_status", lambda: (SELF_ID, (DOMAIN,)))
    made = {}

    class Stop(Exception):
        pass

    def fake_make(ip, port, gate, names=(), acts=None, tls_ctx=None):
        made["tls"] = tls_ctx
        srv = server.Server((ip, 0), gate, names, acts, tls_ctx)
        srv.serve_forever = lambda: (_ for _ in ()).throw(Stop())
        made["scheme"] = srv.scheme
        return srv
    monkeypatch.setattr(server, "make_server", fake_make)
    with caplog.at_level("WARNING"), pytest.raises(Stop):
        server.serve(0, tmp_path / "dashboard.yaml", tls_dir=tmp_path / "absent")
    assert made == {"tls": None, "scheme": "http"}
    assert any("TLS off" in r.getMessage() for r in caplog.records)


def test_permissive_key_is_reported(certs):
    (certs / tls.KEY).chmod(0o644)
    ctx, why = tls.context(certs)
    assert ctx is not None and "lisible" in why


# --- dashboard cert: fake tailscale -------------------------------------------------------------------------

@pytest.fixture
def fake_tailscale(tmp_path, monkeypatch):
    """`tailscale status --json` gives CertDomains; `tailscale cert` copies the pair of <src> into the asked files."""
    src = tmp_path / "ca-src"
    make_cert(src, DOMAIN)
    log = tmp_path / "ts-calls.jsonl"
    script = tmp_path / "fakebin" / "tailscale"
    script.parent.mkdir()
    script.write_text(f"""#!{sys.executable}
import json, os, shutil, sys
a = sys.argv[1:]
open({str(log)!r}, "a").write(json.dumps(a) + "\\n")
if a[:2] == ["status", "--json"]:
    print(json.dumps({{"CertDomains": [{DOMAIN!r}], "Self": {{"ID": "nSELF"}}}}))
elif a[0] == "cert":
    if os.path.exists({str(src / "fail")!r}):
        print("boom", file=sys.stderr); sys.exit(1)
    shutil.copy({str(src / tls.CERT)!r}, a[a.index("--cert-file") + 1])
    shutil.copy({str(src / tls.KEY)!r}, a[a.index("--key-file") + 1])
""")
    script.chmod(0o755)
    monkeypatch.setattr(auth, "TAILSCALE", str(script))
    monkeypatch.setattr(tls, "DEFAULT_DIR", tmp_path / "state" / "dashboard-tls")

    class F:
        source = src
        out = tmp_path / "state" / "dashboard-tls"

        @staticmethod
        def calls():
            return [json.loads(line) for line in log.read_text().splitlines()]
    return F


def test_cert_fetches_stores_0600_and_restarts_only_when_changed(fake_tailscale):
    restarts = []
    first = commands.cert(True, restart=lambda: restarts.append(1) or True)
    assert first["status"] == "ok" and first["data"]["changed"] and first["data"]["restarted"]
    assert first["data"]["domain"] == DOMAIN and first["data"]["expire"]
    out = fake_tailscale.out
    assert (out.stat().st_mode & 0o777) == 0o700
    assert (out / tls.KEY).stat().st_mode & 0o777 == 0o600 and (out / tls.CERT).stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in out.iterdir()) == [tls.CERT, tls.KEY]  # no .new left behind
    assert ["cert", "--cert-file", str(out / "cert.pem.new"), "--key-file", str(out / "key.pem.new"), DOMAIN] in fake_tailscale.calls()
    assert restarts == [1]

    again = commands.cert(True, restart=lambda: restarts.append(2) or True)
    assert again["status"] == "ok" and again["data"]["changed"] is False and again["data"]["restarted"] is False
    assert "inchangé" in again["summary"] and restarts == [1]  # same certificate: no restart
    assert tls.context(out)[0] is not None


def test_cert_renewed_restarts_and_failure_keeps_the_old_one(fake_tailscale):
    restarts = []
    commands.cert(False, restart=lambda: restarts.append(1) or True)
    assert restarts == []  # without --redemarrer nothing is restarted
    old = (fake_tailscale.out / tls.CERT).read_bytes()
    (fake_tailscale.source / "fail").write_text("")
    res = commands.cert(True, restart=lambda: restarts.append(1) or True)
    assert res["status"] == "error" and res["exit_code"] == 1 and restarts == []
    assert (fake_tailscale.out / tls.CERT).read_bytes() == old
    (fake_tailscale.source / "fail").unlink()
    make_cert(fake_tailscale.source, DOMAIN)  # renewed by the CA
    res = commands.cert(True, restart=lambda: restarts.append(1) or True)
    assert res["data"]["changed"] and res["data"]["restarted"] and restarts == [1]
    assert (fake_tailscale.out / tls.CERT).read_bytes() != old


def test_cert_refuses_a_broken_pair_and_keeps_nothing(fake_tailscale):
    (fake_tailscale.source / tls.KEY).write_text("garbage")
    res = commands.cert(True, restart=lambda: pytest.fail("restarted"))
    assert res["status"] == "error" and "inutilisable" in res["error"]
    assert list(fake_tailscale.out.iterdir()) == []


def test_cert_restart_failure_is_reported(fake_tailscale):
    res = commands.cert(True, restart=lambda: False)
    assert res["status"] == "warn" and res["exit_code"] == 1 and "PAS redémarré" in res["summary"]


def test_cert_needs_https_enabled_on_the_tailnet(fake_tailscale, monkeypatch):
    monkeypatch.setattr(auth, "_run_json", lambda *a: {"CertDomains": None})
    res = commands.cert(True, restart=lambda: pytest.fail("restarted"))
    assert res["status"] == "error" and "HTTPS" in res["error"]


def test_cert_timer_is_accepted_by_ultron_user_without_widening_it():
    from ultron_hub import after_merge, user_actions
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    plan = after_merge.parse((root / "deploy/user/apres-fusion/todo-239.json").read_text(encoding="utf-8"))
    assert [(a["action"], a["args"]) for a in plan] == [
        ("user_unit_enable", ["ultron-hub-dashboard-cert.timer"]),
        ("user_unit_restart", ["ultron-hub-dashboard-cert.service"]),
        ("user_unit_restart", ["ultron-hub-dashboard.service"])]
    assert user_actions._spec().ENABLE_SERVICES == ("ultron-hub-dashboard.service",)
    unit = (root / "deploy/systemd/ultron-hub-dashboard-cert.service").read_text(encoding="utf-8")
    execs = [line for line in unit.splitlines() if line.startswith("ExecStart")]
    assert execs == ["ExecStart=%h/workspace/projects/active/ultron-hub/.venv/bin/ultron-hub dashboard cert --redemarrer"]
