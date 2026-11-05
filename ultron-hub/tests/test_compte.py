"""Two Claude accounts used in turn (todo #263): register, state, signed switch, detection of a session writing back.

Never the real ~/.claude nor ~/.config/ultron-hub (fixture `comptes_paths` of conftest.py), never the network: the usage
and profile calls are fakes keyed by the (fake) access token.
"""

import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_plugin import close_key
from ultron_hub import cli
from ultron_hub.compte import store
from ultron_hub.todo import closing

ROOT = Path(__file__).parents[1]
SECRETS = ("ACCESS-A1", "ACCESS-A2", "ACCESS-B1", "REFRESH-A1", "REFRESH-A2", "REFRESH-B1")


def creds(access, refresh, hours=1):
    expires = int((datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp() * 1000)
    return json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": expires,
                                         "scopes": ["user:inference", "user:profile"], "subscriptionType": "pro"}}).encode()


A1, A2, B1 = creds("ACCESS-A1", "REFRESH-A1"), creds("ACCESS-A2", "REFRESH-A2"), creds("ACCESS-B1", "REFRESH-B1")
USAGE = {"ACCESS-A1": 41.0, "ACCESS-A2": 42.0, "ACCESS-B1": 3.0}
PROFILES = {"ACCESS-A1": "uuid-a", "ACCESS-A2": "uuid-a", "ACCESS-B1": "uuid-b"}
EMAILS = {"uuid-a": "pro@exemple.fr", "uuid-b": "perso@exemple.fr"}  # todo #339: the profile's address (key email_address)


@pytest.fixture
def env(project, comptes_paths, monkeypatch):
    """A fake Claude login (the active file), fake endpoints, a throwaway closing key."""
    comptes_paths.parent.mkdir(parents=True)
    calls = {"usage": [], "profile": []}

    def fake_usage(token):
        calls["usage"].append(token)
        return {"five_hour": {"utilization": USAGE[token], "resets_at": "2026-10-19T15:00:00+00:00"},
                "seven_day": {"utilization": USAGE[token] + 10, "resets_at": "2026-10-25T08:00:00+00:00"}}

    def fake_profile(token):
        calls["profile"].append(token)
        return {"account": {"uuid": PROFILES[token], "email_address": EMAILS.get(PROFILES[token], "autre@exemple.fr")}}
    monkeypatch.setattr(store, "fetch_usage", fake_usage)
    monkeypatch.setattr(store, "fetch_profile", fake_profile)
    close_key.load_or_create()
    return {"active": comptes_paths, "calls": calls, "project": project}


def login(env, data):
    env["active"].write_bytes(data)
    env["active"].chmod(0o600)


def run(capsys, *argv):
    code = cli.main(["compte", *argv])
    out = capsys.readouterr().out
    for secret in (*SECRETS, "uuid-a", "uuid-b"):
        assert secret not in out, secret
    return code, json.loads(out.strip().splitlines()[-1])


def dashboard_press(monkeypatch, target, action="compte"):
    token = closing.sign_dashboard(closing.load_key(), "telephone-test", action, target, int(time.time()))
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    return token


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def register_both(env, capsys):
    login(env, A1)
    assert run(capsys, "enregistrer", "a")[0] == 0
    login(env, B1)
    assert run(capsys, "enregistrer", "b")[0] == 0
    login(env, A1)


def test_register_copies_the_active_file_privately(env, capsys):
    login(env, A1)
    code, out = run(capsys, "enregistrer", "a")
    assert code == 0 and out["data"]["nom"] == "a" and out["data"]["compte_reconnu"]
    assert (store.DIR / "a.json").read_bytes() == A1
    assert mode(store.DIR) == 0o700 and mode(store.DIR / "a.json") == 0o600 and mode(store.DIR / store.META) == 0o600
    meta = json.loads((store.DIR / store.META).read_text())
    assert set(meta["comptes"]["a"]) == {"enregistre_a", "compte", "adresse"} and meta["comptes"]["a"]["adresse"] == "pro@exemple.fr"
    assert out["data"]["adresse"] == "pro@exemple.fr" and "pro@exemple.fr" in out["data"]["text"]
    assert "uuid-a" not in json.dumps(meta)
    assert not list(store.DIR.glob(".compte-*"))  # no temporary file left


def test_register_fixes_a_folder_open_to_others(env, capsys):
    store.DIR.mkdir(mode=0o755)
    os.chmod(store.DIR, 0o755)
    login(env, A1)
    assert run(capsys, "enregistrer", "a")[0] == 0 and mode(store.DIR) == 0o700


@pytest.mark.parametrize("content, message", [(None, "aucun fichier"), (b"{pas du json", "illisible"),
                                              (b'{"claudeAiOauth": {}}', "sans jeton")])
def test_register_refuses_without_a_token(env, capsys, content, message):
    if content is not None:
        login(env, content)
    code, out = run(capsys, "enregistrer", "a")
    assert code == 1 and message in out["error"] and not (store.DIR / "a.json").exists()


def test_register_refuses_the_same_account_twice(env, capsys):
    login(env, A1)
    run(capsys, "enregistrer", "a")
    code, out = run(capsys, "enregistrer", "b")
    assert code == 1 and "déjà enregistré comme A" in out["error"] and not (store.DIR / "b.json").exists()
    login(env, A2)  # renewed tokens, same account (profile)
    code, out = run(capsys, "enregistrer", "b")
    assert code == 1 and "déjà enregistré comme A" in out["error"]


def test_register_refuses_to_overwrite_another_account_without_remplacer(env, capsys):
    login(env, A1)
    run(capsys, "enregistrer", "a")
    meta = json.loads((store.DIR / store.META).read_text())
    meta["comptes"]["a"]["compte"] = "0" * 16  # A holds an account that is neither the active one nor B
    (store.DIR / store.META).write_text(json.dumps(meta))
    login(env, B1)
    code, out = run(capsys, "enregistrer", "a")
    assert code == 1 and "--remplacer" in out["error"] and (store.DIR / "a.json").read_bytes() == A1
    assert run(capsys, "enregistrer", "a", "--remplacer")[0] == 0 and (store.DIR / "a.json").read_bytes() == B1


def test_register_refuses_an_occupied_slot_when_the_active_account_is_not_recognised(env, capsys, monkeypatch):
    login(env, A1)
    run(capsys, "enregistrer", "a")

    def unreachable(token):
        raise store.UsageError("profil : réseau indisponible")
    monkeypatch.setattr(store, "fetch_profile", unreachable)
    login(env, B1)  # unknown file, profile down: it may be A renewed or another account → never overwritten silently
    code, out = run(capsys, "enregistrer", "a")
    assert code == 1 and "--remplacer" in out["error"] and "pas pu être reconnu" in out["error"]
    assert (store.DIR / "a.json").read_bytes() == A1
    meta = json.loads((store.DIR / store.META).read_text())
    del meta["comptes"]["a"]["compte"]  # same refusal when no account id was ever recorded for A
    (store.DIR / store.META).write_text(json.dumps(meta))
    assert run(capsys, "enregistrer", "a")[0] == 1 and (store.DIR / "a.json").read_bytes() == A1
    assert run(capsys, "enregistrer", "b")[0] == 0  # a free slot needs no option
    login(env, A2)
    assert run(capsys, "enregistrer", "a", "--remplacer")[0] == 0 and (store.DIR / "a.json").read_bytes() == A2


def test_register_refuses_a_copy_whose_fingerprint_differs(env, capsys, monkeypatch):
    real = store.write_atomic
    monkeypatch.setattr(store, "write_atomic", lambda path, data: real(path, data[:-1] if path.name == "a.json" else data))
    login(env, A1)
    code, out = run(capsys, "enregistrer", "a")
    assert code == 1 and "empreinte" in out["error"] and not (store.DIR / store.META).exists()


def test_switch_checks_the_fingerprint_of_the_written_file_and_restores(env, capsys, monkeypatch):
    register_both(env, capsys)
    dashboard_press(monkeypatch, "b")
    real = store.write_atomic
    corrupt = {"n": 0}

    def short_write(path, data):  # the active file comes back different from B's copy (disk, another writer...)
        if path == env["active"] and data == B1:
            corrupt["n"] += 1
            data = data[:-2]
        real(path, data)
    monkeypatch.setattr(store, "write_atomic", short_write)
    code, out = run(capsys, "basculer")
    assert code == 1 and "empreinte" in out["error"] and "A remis en place" in out["error"] and corrupt["n"] == 1
    assert env["active"].read_bytes() == A1  # restored, and its own SHA-256 checked
    meta = json.loads((store.DIR / store.META).read_text())
    assert meta["derniere_bascule"] is None and len(meta["preuves"]) == 1  # not called switched; the proof is spent
    assert (store.DIR / "b.json").read_bytes() == B1


def test_switch_says_so_when_the_restore_fails_too(env, capsys, monkeypatch):
    register_both(env, capsys)
    dashboard_press(monkeypatch, "b")
    real = store.write_atomic
    monkeypatch.setattr(store, "write_atomic", lambda path, data: real(path, data[:-1] if path == env["active"] else data))
    code, out = run(capsys, "basculer")
    assert code == 1 and "a échoué aussi" in out["error"]


def test_state_names_the_active_account_and_reads_both_quotas(env, capsys):
    register_both(env, capsys)
    code, out = run(capsys, "etat")
    d = out["data"]
    assert code == 0 and d["actif"] == "a" and d["autre"] == "b" and d["alerte"] is None
    assert [(c["nom"], c["actif"], c["quota"]["session"]["percent"]) for c in d["comptes"]] == [("a", True, 41.0), ("b", False, 3.0)]
    assert "Actif : pro@exemple.fr" in d["text"]


def test_state_without_any_registered_account(env, capsys):
    login(env, A1)
    code, out = run(capsys, "etat")
    assert code == 0 and out["data"]["actif"] is None and out["data"]["alerte"] is None and out["data"]["autre"] is None
    assert env["calls"]["usage"] == [] and env["calls"]["profile"] == []


def test_state_reports_an_expired_token_without_refreshing(env, capsys):
    register_both(env, capsys)
    (store.DIR / "b.json").write_bytes(creds("ACCESS-B1", "REFRESH-B1", hours=-1))
    out = run(capsys, "etat")[1]["data"]
    assert out["comptes"][1]["quota"] is None and out["comptes"][1]["quota_attente"]
    assert out["comptes"][1]["quota_erreur"] == store.EN_ATTENTE and out["a_reconnecter"] == []  # todo #330: not a failure
    assert f"perso@exemple.fr : {store.EN_ATTENTE}" in out["text"] and "🔑" not in out["text"]
    assert "ACCESS-B1" not in env["calls"]["usage"]


B_OLD = creds("ACCESS-B1", "REFRESH-B1", hours=-3)  # todo #330: an hours-old copy, its access token expired


def test_a_switch_to_an_expired_copy_is_to_confirm_then_confirmed(env, capsys, monkeypatch):
    register_both(env, capsys)
    (store.DIR / "b.json").write_bytes(B_OLD)
    dashboard_press(monkeypatch, "b")
    code, out = run(capsys, "basculer")
    d = out["data"]
    assert code == 0 and env["active"].read_bytes() == B_OLD and d["verification"]["ok"] is None
    assert "Quota lisible dès la première session Claude" in d["text"] and "Vérification impossible" not in d["text"]
    assert store.load_meta()["derniere_bascule"]["verifie"] is None
    logs = "".join(p.read_text() for p in (env["project"] / "var" / "logs").glob("*.log*"))
    assert "verifie=None" in logs
    d = run(capsys, "etat")[1]["data"]
    assert d["actif"] == "b" and d["derniere_bascule"]["verifie"] is None and d["a_reconnecter"] == []
    monkeypatch.setitem(USAGE, "ACCESS-B2", 7.0)
    monkeypatch.setitem(PROFILES, "ACCESS-B2", "uuid-b")
    login(env, creds("ACCESS-B2", "REFRESH-B1"))  # Claude Code renewed the token of B
    d = run(capsys, "etat")[1]["data"]
    assert d["actif"] == "b" and d["comptes"][1]["quota"]["session"]["percent"] == 7.0
    assert d["derniere_bascule"]["verifie"] is True and store.load_meta()["derniere_bascule"]["verifie"] is True


def test_a_switch_never_confirmed_after_12_hours_is_to_log_into_again(env, capsys, monkeypatch):
    register_both(env, capsys)
    (store.DIR / "b.json").write_bytes(B_OLD)
    dashboard_press(monkeypatch, "b")
    assert run(capsys, "basculer")[0] == 0
    cache = env["project"] / "var" / "cache"
    later = datetime.now(timezone.utc) + timedelta(hours=11)
    assert store.etat(cache, now=later)["a_reconnecter"] == []
    later = datetime.now(timezone.utc) + timedelta(hours=13)
    d = store.etat(cache, now=later)
    assert d["derniere_bascule"]["verifie"] is None
    assert d["a_reconnecter"] == [{"nom": "b", "adresse": "perso@exemple.fr", "raison": "bascule jamais confirmée"}]


def test_a_refused_token_is_still_a_failed_check(env, capsys, monkeypatch):
    register_both(env, capsys)

    def refused(token):
        raise store.UsageError("jeton refusé (401)")
    monkeypatch.setattr(store, "fetch_usage", refused)
    dashboard_press(monkeypatch, "b")
    out = run(capsys, "basculer")[1]["data"]
    assert out["verification"]["ok"] is False and "Vérification impossible : jeton refusé (401)" in out["text"]
    assert store.load_meta()["derniere_bascule"]["verifie"] is False
    d = run(capsys, "etat")[1]["data"]
    assert [(x["nom"], x["raison"]) for x in d["a_reconnecter"]] == [("a", "quota illisible (jeton refusé (401))"),
                                                                    ("b", "quota illisible (jeton refusé (401))")]


def test_switch_needs_the_users_proof(env, capsys):
    register_both(env, capsys)
    code, out = run(capsys, "basculer")
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and "compte basculer" in out["error"]
    assert env["active"].read_bytes() == A1


def test_signed_switch_swaps_the_file_then_checks_the_usage(env, capsys, monkeypatch):
    register_both(env, capsys)
    login(env, A2)  # A renewed its tokens since it was registered
    cache = env["project"] / "var" / "cache" / "usage.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{}")
    dashboard_press(monkeypatch, "b")
    code, out = run(capsys, "basculer", "--vers=b")
    d = out["data"]
    assert code == 0 and (d["de"], d["vers"]) == ("a", "b") and d["by"]["by"] == "dashboard:telephone-test"
    assert env["active"].read_bytes() == B1 and mode(env["active"]) == 0o600
    assert (store.DIR / "a.json").read_bytes() == A2  # the renewed file was saved back first
    assert d["verification"]["ok"] and d["verification"]["session"]["percent"] == 3.0
    assert not cache.exists()  # /usage reads the new account at once
    assert "ultron relance" in d["text"] and "restent sur pro@exemple.fr" in d["text"]
    meta = json.loads((store.DIR / store.META).read_text())
    assert meta["derniere_bascule"]["vers"] == "b" and len(meta["preuves"]) == 1
    code, out = run(capsys, "basculer")  # the same token: signed for B, the destination is now A
    assert code == 1 and "invalide" in out["error"] and env["active"].read_bytes() == B1
    login(env, A2)  # back on A: the token's target is right again, but it was spent
    code, out = run(capsys, "basculer")
    assert code == 1 and "déjà utilisé" in out["error"] and env["active"].read_bytes() == A2


def test_switch_with_a_telegram_style_token_and_back(env, capsys, monkeypatch):
    register_both(env, capsys)
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("compte", "b"))
    assert run(capsys, "basculer")[0] == 0 and env["active"].read_bytes() == B1
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("compte", "a"))
    assert run(capsys, "basculer", "--vers", "a")[0] == 0 and env["active"].read_bytes() == A1


@pytest.mark.parametrize("action, target", [("done", "b"), ("compte", "a"), ("user", "b")])
def test_switch_refuses_a_token_for_another_action_or_target(env, capsys, monkeypatch, action, target):
    register_both(env, capsys)
    if action == "user":
        monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("user", "b"))
    else:
        dashboard_press(monkeypatch, target, action)
    code, out = run(capsys, "basculer")
    assert code == 1 and env["active"].read_bytes() == A1


def test_switch_refusals_change_nothing(env, capsys, monkeypatch):
    login(env, A1)
    dashboard_press(monkeypatch, "b")
    code, out = run(capsys, "basculer")
    assert code == 1 and "inconnu" in out["error"]  # nothing registered: the active file is nobody's
    run(capsys, "enregistrer", "a")
    code, out = run(capsys, "basculer")
    assert code == 1 and "B pas enregistré" in out["error"]
    login(env, B1)
    run(capsys, "enregistrer", "b")
    code, out = run(capsys, "basculer", "--vers", "b")
    assert code == 1 and "déjà B" in out["error"]
    login(env, creds("ACCESS-B1", "REFRESH-X"))  # a file nobody registered (profile refused)
    monkeypatch.setattr(store, "fetch_profile", lambda token: (_ for _ in ()).throw(store.UsageError("profil : 401")))
    code, out = run(capsys, "basculer")
    assert code == 1 and "fichier actif inconnu" in out["error"]
    assert (store.DIR / "a.json").read_bytes() == A1 and (store.DIR / "b.json").read_bytes() == B1


def test_switch_refuses_two_slots_of_the_same_account(env, capsys, monkeypatch):
    register_both(env, capsys)
    meta = json.loads((store.DIR / store.META).read_text())
    meta["comptes"]["b"]["compte"] = meta["comptes"]["a"]["compte"]
    (store.DIR / store.META).write_text(json.dumps(meta))
    dashboard_press(monkeypatch, "b")
    code, out = run(capsys, "basculer")
    assert code == 1 and "même compte" in out["error"] and env["active"].read_bytes() == A1


def test_a_renewed_file_is_still_recognised(env, capsys):
    register_both(env, capsys)
    renewed = json.loads(A1)
    renewed["claudeAiOauth"]["accessToken"] = "ACCESS-A2"  # same refresh token: renewed access token
    login(env, json.dumps(renewed).encode())
    d = run(capsys, "etat")[1]["data"]
    assert d["actif"] == "a" and "même connexion" in d["comment"]
    login(env, A2)  # refresh token rotated too: the profile says it is A
    d = run(capsys, "etat")[1]["data"]
    assert d["actif"] == "a" and "même compte" in d["comment"]
    calls = len(env["calls"]["profile"])
    run(capsys, "etat")
    assert len(env["calls"]["profile"]) == calls  # remembered per file digest


def test_state_detects_an_old_session_writing_the_old_account_back(env, capsys, monkeypatch):
    register_both(env, capsys)
    dashboard_press(monkeypatch, "b")
    assert run(capsys, "basculer")[0] == 0
    login(env, A2)  # a session opened on A renews its token after the switch
    d = run(capsys, "etat")[1]["data"]
    assert d["actif"] == "a" and "revenu à l'ancien compte pro@exemple.fr" in d["alerte"] and "ultron relance" in d["alerte"]
    login(env, creds("ACCESS-B1", "REFRESH-Y"))  # unknown and the profile is unreachable
    monkeypatch.setattr(store, "fetch_profile", lambda token: (_ for _ in ()).throw(store.UsageError("profil : réseau")))
    d = run(capsys, "etat")[1]["data"]
    assert d["actif"] is None and "fichier actif inconnu" in d["alerte"] and "bascule vers perso@exemple.fr" in d["alerte"]


def test_switch_aborts_if_the_active_file_changes_meanwhile(env, capsys, monkeypatch):
    register_both(env, capsys)
    dashboard_press(monkeypatch, "b")
    real = store.write_atomic

    def renew_during_save(path, data):
        real(path, data)
        if path.name == "a.json":
            env["active"].write_bytes(A2)  # a session renews its token right then
    login(env, creds("ACCESS-A1", "REFRESH-A1", hours=2))  # differs from the copy: the copy is saved again
    monkeypatch.setattr(store, "write_atomic", renew_during_save)
    code, out = run(capsys, "basculer")
    assert code == 1 and "vient de changer" in out["error"] and env["active"].read_bytes() == A2


def test_no_secret_in_the_logs(env, capsys, monkeypatch):
    register_both(env, capsys)
    dashboard_press(monkeypatch, "b")
    run(capsys, "basculer")
    run(capsys, "etat")
    monkeypatch.delenv(closing.TOKEN_ENV)
    run(capsys, "basculer")
    logs = "".join(p.read_text() for p in (env["project"] / "var" / "logs").glob("*.log*"))
    assert "compte basculer" in logs
    for secret in (*SECRETS, *EMAILS.values(), "uuid-a"):
        assert secret not in logs
    for path in store.DIR.iterdir():  # only the copies hold tokens; the state holds hashes
        if path.name not in ("a.json", "b.json"):
            assert not any(s in path.read_text() for s in SECRETS), path


def test_the_real_paths_come_from_pwd_not_home():
    import pwd
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert store.HOME == home
    src = (ROOT / "src/ultron_hub/compte/store.py").read_text(encoding="utf-8")
    assert 'HOME / ".claude" / ".credentials.json"' in src and 'HOME / ".config" / "ultron-hub" / "comptes"' in src
    assert "Path.home()" not in src and "print(" not in src


def test_closing_knows_the_signed_switch():
    assert "compte" in closing.ACTIONS and close_key.ACTIONS == closing.ACTIONS
    assert "compte" in closing.DASHBOARD_ACTIONS and close_key.DASHBOARD_ACTIONS == closing.DASHBOARD_ACTIONS
    assert closing.SIGNED_ACTORS["compte"] == "telegram-button"


def test_the_dashboard_card_goes_through_the_catalogue():
    from ultron_hub.dashboard import catalogue
    b = catalogue.PAR_ID["compte-basculer"]
    assert b.type == "signee" and b.signe == "compte" and b.cible == "{vers}" and b.risque == "high"
    assert catalogue.PAR_ID["comptes"].type == "lecture"
    assert catalogue.commande(b, {"vers": "b"}, None) == ["compte", "basculer", "--vers=b"]
    for s in "ab":
        assert catalogue.PAR_ID[f"compte-enregistrer-{s}"].argv == ("compte", "enregistrer", s)
    data = {"actif": "a", "comment": "x", "alerte": None, "autre": "b", "derniere_bascule": None, "jeton": "ACCESS-A1",
            "comptes": [{"nom": "a", "quota": {"session": {"percent": 1, "token": "ACCESS-A1"}}, "chemin": "/x"}]}
    assert "ACCESS-A1" not in json.dumps(catalogue.projeter(catalogue.COMPTES, data))
    js = (ROOT / "src/ultron_hub/dashboard/static/app.js").read_text(encoding="utf-8")
    assert 'lire("comptes")' in js and 'form("compte-basculer"' in js


def test_restart_after_merge_is_declared_for_263():
    plan = json.loads((ROOT / "deploy/user/apres-fusion/todo-263.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("user_unit_restart", ["ultron-hub-dashboard.service"])]


# -- todo #339: the e-mail address replaces « compte A/B » for the user ---------------------------------------------------------

def test_state_shows_the_addresses_and_text_uses_them(env, capsys):
    register_both(env, capsys)
    code, out = run(capsys, "etat")
    d = out["data"]
    assert [c["adresse"] for c in d["comptes"]] == ["pro@exemple.fr", "perso@exemple.fr"]
    assert d["actif_adresse"] == "pro@exemple.fr" and d["autre_adresse"] == "perso@exemple.fr"
    assert "Actif : pro@exemple.fr" in d["text"] and "• perso@exemple.fr" in d["text"] and "• A" not in d["text"]


def test_state_fills_a_missing_address_with_one_profile_call_per_slot(env, capsys):
    register_both(env, capsys)
    meta = store.load_meta()
    for s in ("a", "b"):
        del meta["comptes"][s]["adresse"]
    store.save_meta(meta)
    env["calls"]["profile"].clear()
    _, out = run(capsys, "etat")
    assert [c["adresse"] for c in out["data"]["comptes"]] == ["pro@exemple.fr", "perso@exemple.fr"]
    assert len(env["calls"]["profile"]) == 2
    assert json.loads((store.DIR / store.META).read_text())["comptes"]["b"]["adresse"] == "perso@exemple.fr"
    env["calls"]["profile"].clear()
    run(capsys, "etat")
    assert env["calls"]["profile"] == []  # remembered


def test_state_falls_back_to_the_letter_when_the_address_is_unknown(env, capsys, monkeypatch):
    register_both(env, capsys)
    meta = store.load_meta()
    for s in ("a", "b"):
        del meta["comptes"][s]["adresse"]
    store.save_meta(meta)
    monkeypatch.setattr(store, "fetch_profile", lambda token: (_ for _ in ()).throw(store.UsageError("profil : 401")))
    code, out = run(capsys, "etat")
    d = out["data"]
    assert code == 0 and [c["adresse"] for c in d["comptes"]] == [None, None] and d["actif_adresse"] is None
    assert "Actif : compte A" in d["text"] and "• B" in d["text"]


def test_a_profile_with_the_key_email_or_no_address(env, capsys, monkeypatch):
    assert store.email_of({"account": {"email": "x@exemple.fr"}}) == "x@exemple.fr"
    assert store.email_of({"account": {"email_address": "x@exemple.fr"}}) == "x@exemple.fr"
    assert store.email_of({"account": {"email_address": "pas une adresse"}}) is None
    assert store.email_of({"account": "x"}) is None and store.email_of({}) is None


def test_switch_message_names_the_accounts_by_address(env, capsys, monkeypatch):
    register_both(env, capsys)
    dashboard_press(monkeypatch, "b")
    code, out = run(capsys, "basculer")
    assert code == 0 and "pro@exemple.fr → perso@exemple.fr" in out["data"]["text"]
    assert out["data"]["vers_adresse"] == "perso@exemple.fr"


def test_a_failed_address_try_is_not_repeated_for_six_hours(env, capsys, monkeypatch):
    register_both(env, capsys)
    meta = store.load_meta()
    for s in ("a", "b"):
        del meta["comptes"][s]["adresse"]
    store.save_meta(meta)
    calls = []

    def down(token):
        calls.append(token)
        raise store.UsageError("profil : réseau indisponible")
    monkeypatch.setattr(store, "fetch_profile", down)
    run(capsys, "etat")
    run(capsys, "etat")
    assert len(calls) == 2  # one per slot, the second etat did not try again
    assert "adresse_essai" in json.loads((store.DIR / store.META).read_text())["comptes"]["a"]
    meta = store.load_meta()
    for s in ("a", "b"):
        meta["comptes"][s]["adresse_essai"] = store.now_iso(datetime.now(timezone.utc) - timedelta(hours=7))
    store.save_meta(meta)
    run(capsys, "etat")
    assert len(calls) == 4
    login(env, A1)
    assert run(capsys, "enregistrer", "a", "--remplacer")[0] == 0  # a successful register resets the try
    assert "adresse_essai" not in json.loads((store.DIR / store.META).read_text())["comptes"]["a"]
