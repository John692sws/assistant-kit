"""Dashboard (todo #130): closed device list, 403 for anyone else, blocks, page without external resource. No network."""

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from ultron_hub.chantier import core as chantier_core
from ultron_hub.dashboard import auth, catalogue, server, views

SELF_ID, PHONE_ID, STRANGER_ID = "nSELF", "nPHONE", "nSTRANGER"
DEVICES = (auth.Device("telephone-test", PHONE_ID), auth.Device("serveur-test", SELF_ID))  # the server listed by mistake


@pytest.fixture(autouse=True)
def fresh_cache():
    views.clear_cache()
    yield
    views.clear_cache()


def start(whois):
    gate = auth.Gate(DEVICES, SELF_ID, "100.64.0.1", whois=whois)
    srv = server.make_server("127.0.0.1", 0, gate)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def get(srv, path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_gate_decisions():
    ids = {"100.1.1.1": PHONE_ID, "100.1.1.2": STRANGER_ID, "100.1.1.3": SELF_ID, "100.1.1.4": ""}
    gate = auth.Gate(DEVICES, SELF_ID, "100.64.0.1", whois=lambda ip: ids.get(ip, ""))
    assert gate.caller("100.1.1.1") == "telephone-test"
    assert gate.caller("100.1.1.2") is None      # not in the list
    assert gate.caller("100.1.1.3") is None      # the server's own node, even listed
    assert gate.caller("100.1.1.4") is None      # whois gave nothing
    assert gate.caller("100.64.0.1") is None  # the server calling itself


def test_unknown_caller_and_server_itself_get_403_and_nothing_runs(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a command ran for a refused caller")
    monkeypatch.setattr(views, "state", boom)
    monkeypatch.setattr(views, "block", boom)
    for node in (STRANGER_ID, SELF_ID, ""):
        srv = start(lambda ip, node=node: node)
        try:
            for path, method in (("/", "GET"), ("/api/state", "GET"), ("/api/quota", "GET"), ("/app.js", "GET"),
                                 ("/api/state", "POST"), ("/", "HEAD")):
                code, _ = get(srv, path, method)
                assert code == 403, (node, path, method)
        finally:
            srv.shutdown()


def test_allowed_device_reads_but_cannot_act(monkeypatch):
    monkeypatch.setattr(views, "state", lambda: {"ok": True})
    srv = start(lambda ip: PHONE_ID)
    try:
        assert get(srv, "/api/state") == (200, b'{"ok": true}')
        assert get(srv, "/api/state", "POST")[0] == 405
        assert get(srv, "/nope")[0] == 404
        assert get(srv, "/api/chantier/../../etc")[0] == 404
    finally:
        srv.shutdown()


INSTALL_FILES = ("manifest.webmanifest", "apple-touch-icon.png", "icon-192.png", "icon-512.png")


def test_manifest_and_icons_are_served_to_an_allowed_device():
    srv = start(lambda ip: PHONE_ID)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{url}/manifest.webmanifest", timeout=5) as r:
            assert r.headers["Content-Type"] == "application/manifest+json"
            manifest = json.loads(r.read())
        assert manifest["display"] == "standalone" and manifest["start_url"] == "/" and manifest["short_name"] == "Ultron"
        assert {i["sizes"] for i in manifest["icons"]} == {"192x192", "512x512"}
        for icon in manifest["icons"]:
            assert get(srv, icon["src"])[0] == 200
        for name in INSTALL_FILES[1:]:
            with urllib.request.urlopen(f"{url}/{name}", timeout=5) as r:
                assert r.headers["Content-Type"] == "image/png" and r.read(8) == b"\x89PNG\r\n\x1a\n"
    finally:
        srv.shutdown()


def test_manifest_and_icons_refuse_an_unknown_device():
    srv = start(lambda ip: STRANGER_ID)
    try:
        for name in INSTALL_FILES:
            assert get(srv, f"/{name}")[0] == 403, name
    finally:
        srv.shutdown()


def test_page_is_small_and_has_no_external_resource():
    total = 0
    for name in ("index.html", "style.css", "app.js", "manifest.webmanifest"):
        text = (server.STATIC / name).read_text(encoding="utf-8")
        total += len(text.encode())
        assert not re.search(r"https?://|//cdn|@import|url\(", text), name
        assert "innerHTML" not in text
    total += sum((server.STATIC / n).stat().st_size for n in INSTALL_FILES if n.endswith(".png"))
    assert total < 199_000  # todo #534 after main (#539, #535): was 198 kB, raised by 1 kB; todo #534: bascule auto de compte, planning (was 196.5 kB, raised by 1.5 kB); todo #531: ↩️ per line of the bilan du soir (was 195.5 kB, raised by 1 kB); todo #530: 🌙 bilan du soir card (was 194 kB, raised by 1.5 kB); todo #437: 🎬 montage en un geste (was 193 kB, raised by 1 kB); todo #364: session conseillée card (was 192 kB, raised by 1 kB); todo #466: refonte 3 onglets, file À toi unique, Fil, Demander à Ultron (was 177 kB, raised by 15 kB); todo #441: propositions after the analysis of a video (was 176 kB, raised by 1 kB); todo #439: 🔊 Lire à la voix Piper du serveur (was 175 kB, raised by 1 kB); todo #429: Nouvelle habitude proposée (was 173 kB, raised by 2 kB); todo #409: Fil en boîte de notifications (was 171 kB, raised by 2 kB); todo #422: 🔊 Lire (was 169 kB, raised by 2 kB: client-side TTS); todo #398: dictée de l'appareil (was 165 kB); todo #379: 🎙 du Pilote, vocal en 2 appuis (was 160 kB); todo #373: lecteur de pièces jointes (was 152 kB); todo #362: 🧹 Ménage proposé (was 150 kB); todo #301: Fil, sessions, joindre (was 140 kB); todo #281: glyphs instead of line icons, no flame (smaller); todo #243: ménage, ranger, ajout (was 130 kB); #270: briefing (flame, decisions); #269: line icons + Doom tokens (was 100 kB)
    srv = start(lambda ip: PHONE_ID)
    try:
        code, page = get(srv, "/")
        assert code == 200 and len(page) < 100_000
        assert b'name="viewport"' in page
        assert re.findall(rb'(?:src|href)="([^"]+)"', page) == [
            b"/manifest.webmanifest", b"/apple-touch-icon.png", b"/style.css", b"/app.js"]
    finally:
        srv.shutdown()


def test_blocks_json_from_the_commands(monkeypatch):
    monkeypatch.setattr(views.chantier_commands, "handle", lambda args, s=None: (
        {"status": "ok", "data": {"items": [{"kind": "repondre", "icone": "💬", "todo": 4, "texte": "Faire", "chantier": "-"}],
                                  "total": 1}} if args.chantier_command == "atoi" else
        {"status": "ok", "data": {"chantiers": [{"sujet": "a", "titre": "Alpha", "groupe": "🟢", "projet": "/home/exemple2",
                                                 "todos": [{"id": 4, "titre": "Faire"}], "a_toi": [{"todo": 4}],
                                                 "escouade": None}]}}))
    monkeypatch.setattr(views.chantier_core, "done_counts", lambda settings: {"a": 7, "b": 2})
    monkeypatch.setattr(views.usage_commands, "handle", lambda args, s: {"status": "ok", "data": {
        "session": {"percent": 93.0, "resets_at": "2026-10-19T10:00:00+00:00"}, "week": {"percent": 43.0, "resets_at": None},
        "token": "SECRET"}})
    monkeypatch.setattr(views.health_collect, "collect", lambda: {
        "system": {"cpus": 4, "load_1m": 0.2}, "memory": {"used_pct": 20.4}, "disks": [{"used_pct": 48.4}],
        "services": {"user": {"hermes-gateway": "active"}, "failed_user": [], "failed_system": []},
        "backup": {"last_success_age_h": 2.5}})
    monkeypatch.setattr(views, "load_settings", lambda: None)
    state = views.state()
    assert "escouades" not in state  # the autonomous squads are retired (todo #458)
    assert state["quota"]["session"]["percent"] == 93.0 and state["quota"]["semaine"]["percent"] == 43.0
    assert [i["ref"] for i in state["a_toi"]["items"]] == ["#4"]
    # todo #270: each item says why it waits and which catalogue action approves it (none for a question)
    assert [(i["approuver"], bool(i["pourquoi"])) for i in state["a_toi"]["items"]] == [(None, True)]
    assert state["a_toi"]["phrase"] == "Il me faut ton avis sur 1 autre point."  # todo #345
    assert state["sante"]["disque_pct"] == 48.4 and state["sante"]["hermes_gateway"] == "active"
    # todo #281: x/y todos of the home screen (done / done + open), what waits for you, a squad
    assert state["chantiers"]["chantiers"] == [{"sujet": "a", "titre": "Alpha", "groupe": "🟢", "hors": False, "ouvertes": 1,
                                                "faites": 7, "a_toi": 1}]
    dump = json.dumps(state)
    assert not any(s in dump for s in ("SECRET", "/secret/dir", "abc123", "/home/exemple2"))


def test_a_failing_block_stays_inside_its_block(monkeypatch):
    def boom():
        raise RuntimeError("token=SECRET")
    monkeypatch.setitem(views.BLOCKS, "quota", boom)
    quota = views.block("quota")
    assert quota == {"ok": False, "erreur": "RuntimeError"}


def test_fiche_refuses_a_bad_name():
    with pytest.raises(ValueError):
        views.fiche("../x")


def test_devices_file(tmp_path):
    from pathlib import Path
    real = auth.load_devices(Path(__file__).parents[1] / "config" / "dashboard.yaml")
    names = [d.name for d in real]
    assert len(names) == 3 and len(set(names)) == 3
    bad = tmp_path / "d.yaml"
    bad.write_text("appareils: []\n")
    with pytest.raises(auth.DashboardConfigError):
        auth.load_devices(bad)
    with pytest.raises(auth.DashboardConfigError):
        auth.load_devices(tmp_path / "absent.yaml")


@pytest.mark.parametrize("out,ok", [("100.64.0.1\n", True), ("127.0.0.1\n", False), ("0.0.0.0\n", False), ("", False)])
def test_listens_only_on_a_tailscale_address(monkeypatch, out, ok):
    import subprocess
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    if ok:
        assert auth.tailscale_ip() == "100.64.0.1"
    else:
        with pytest.raises(auth.DashboardConfigError):
            auth.tailscale_ip()


def test_service_and_after_merge_file():
    from pathlib import Path
    root = Path(__file__).parents[1]
    unit = (root / "deploy" / "systemd" / "ultron-hub-dashboard.service").read_text()
    assert "Restart=on-failure" in unit and "dashboard serve --port 8787" in unit and "WantedBy=default.target" in unit
    plan = json.loads((root / "deploy" / "user" / "apres-fusion" / "todo-130.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("user_unit_enable", ["ultron-hub-dashboard.service"])]


def test_app_shell_has_four_tabs_sheet_and_safe_areas():
    html = (server.STATIC / "index.html").read_text(encoding="utf-8")
    css = (server.STATIC / "style.css").read_text(encoding="utf-8")
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    # todo #466: three tabs (À toi = briefing, En cours = travail, Fil); Moi behind the top-right icon (#ouvrir-moi)
    assert re.findall(r'data-tab="(\w+)"', html) == ["briefing", "travail", "fil"]  # todo #270, #292, #458, #466
    assert 'id="ouvrir-moi"' in html and 'selectTab(document.getElementById("tab-moi").hidden ? "moi" : "briefing")' in js
    for tab in ("briefing", "travail", "fil", "moi"):
        assert f'id="tab-{tab}"' in html
    for ident in ("confirm", "scrim", "fiche", "toasts", "ptr"):
        assert f'id="{ident}"' in html, ident
    assert "position: fixed; inset: 0" in css and "--tabbar: 49px" in css  # todo #292: no empty band under the tab bar
    assert "viewport-fit=cover" in html and html.count('class="badge"') == 1
    for token in ("env(safe-area-inset-top)", "env(safe-area-inset-bottom)", "prefers-reduced-motion",
                  "min-height: 44px"):
        assert token in css, token
    assert "transition: transform" in css and "transition: width" not in css
    assert "prefers-color-scheme" not in css  # todo #281: dark only
    # the logic is unchanged: same endpoints, stop goes through prepare/confirm, never a direct call
    for call in ("/api/state", "/api/action/preparer", "/api/action/confirmer", "/api/chantier/"):
        assert call in js, call
    assert "serviceWorker.register(\"/sw.js\")" in js  # todo #240: push notifications


def test_pilote_is_full_screen_without_key_row():  # todo #292
    css = (server.STATIC / "style.css").read_text(encoding="utf-8")
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    assert "#fiche.plein" in css and "100dvh" in css and "60vh" in css.split("#fiche.plein")[0]  # the 60vh cap is the card's, not the Pilote's
    assert '"plein"' in js and "visualViewport" in js and "📎 joindre" in js
    pilote = js.split("function ouvrirSession(")[1].split("[[\"plus-sessions\"")[0]
    assert "touches" not in pilote  # no row of keys any more
    # todo #293: one thin top line, no « à jour » line, no « la session attend » label, no ↵ button: the keyboard's send key
    assert "pilote-haut" in pilote and "à jour" not in pilote and "la session attend" not in pilote and '"envoi"' not in pilote
    assert 'setAttribute("enterkeyhint", "send")' in pilote and '"submit"' in pilote and 'touche("entree")' in pilote
    assert "button.envoi" not in css and "#fiche.plein > .bar-top { display: none; }" in css and "body.pilote #tabs" in css
    # the bottom safe area is counted once (the Pilote's bottom bar), and dropped while the keyboard is open
    plein = css.split("#fiche.plein {")[1]
    assert plein.count("safe-area-inset-bottom") == 1 and "#fiche.clavier .pilote-bas" in css
    # todo #466: the Fil is still a tab, but without a badge (one badge: À toi)
    assert 'data-tab="fil"' in (server.STATIC / "index.html").read_text(encoding="utf-8") and 'setBadge(d.non_lues, "fil"' not in js


def test_app_is_alive_badges_toasts_pull_refresh_and_clear_errors():
    html = (server.STATIC / "index.html").read_text(encoding="utf-8")
    css = (server.STATIC / "style.css").read_text(encoding="utf-8")
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    # one badge (todos #270, #466): the cards of À toi, nothing else; no pip since the squads are gone (#458)
    assert html.count('class="badge"') == 1 and 'class="pip"' not in html
    assert "setBadge(cartes.length)" in js and "setPip(" not in js and "ESCOUADES" not in js
    # refresh: 30 s timer, back in the app, pull to refresh, never while a sheet is open
    assert "visibilitychange" in js and "REFRESH_MS = 30000" in js and "touchstart" in js and "touchend" in js
    assert "actif()" in js and "current === null" in js
    # clear errors and last update time
    assert "Serveur injoignable : Tailscale actif ?" in js
    assert 'id="erreur"' in html and 'id="maj"' in html and "dernière MAJ" in js and "mis à jour" in js
    # toast after an action, and empty states
    assert "toastEnAttente" in js and "function toast(" in js and ".toast" in css
    assert "Rien n'attend ton avis" in js and "Aucun chantier" in js
    # motion budget: transform/opacity only, reduced motion respected
    assert "transition: width" not in css and "transition: height" not in css and "prefers-reduced-motion" in css


def test_served_budget_under_100kb_with_the_live_app():
    total = sum(p.stat().st_size for p in server.STATIC.iterdir() if p.is_file())
    assert total < 201_500  # todo #534 after main (#539, #535): was 200.5 kB, raised by 1 kB; todo #534: bascule auto de compte, planning (was 199 kB, raised by 1.5 kB); todo #531: ↩️ per line of the bilan du soir (was 198 kB, raised by 1 kB); todo #530: 🌙 bilan du soir card (was 196.5 kB, raised by 1.5 kB); todo #437: 🎬 montage en un geste (was 195.5 kB, raised by 1 kB); todo #364: session conseillée card (was 194.5 kB, raised by 1 kB); todo #466: refonte 3 onglets (was 179 kB, raised by 15.5 kB); todo #441: propositions after a video analysis (was 178.5 kB, raised by 0.5 kB); todo #439: 🔊 Lire à la voix Piper du serveur (was 177.5 kB, raised by 1 kB); todo #429: Nouvelle habitude proposée (was 175.5 kB, raised by 2 kB); todo #409: Fil en boîte de notifications (was 173 kB, raised by 2.5 kB); todo #422: 🔊 Lire (was 170 kB, raised by 3 kB: client-side TTS); todo #398: dictée de l'appareil (was 166 kB); todo #379: 🎙 du Pilote (was 162 kB); todo #373: lecteur (was 155 kB); todo #346 (was 154 kB: Journal par jour, filtres); #362 (was 152 kB: 🧹 Ménage proposé); #355 (was 150 kB: envelope icon); #339 (was 145 kB: account addresses); #243 (was 130 kB); #270 (was 120 kB, #269: 100 kB); #297 (was 140 kB: close + chantier)
    assert "http://" not in (server.STATIC / "app.js").read_text(encoding="utf-8")


def test_three_tabs_a_toi_en_cours_fil_and_moi():
    """Todo #466 (replaces the home screen of #281): À toi = ONE queue of decisions, En cours = state band, sessions, done today and
    ONE « Demander à Ultron » field, Fil; Moi (cap, chantiers, every task, accounts, health) behind the top-right icon. No rule in
    the page: counts, chantiers and sessions are the server's; every button is a catalogue action through prepare()."""
    html = (server.STATIC / "index.html").read_text(encoding="utf-8")
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    atoi = html[html.index('id="tab-briefing"'):html.index('id="tab-travail"')]
    encours = html[html.index('id="tab-travail"'):html.index('id="tab-fil"')]
    moi = html[html.index('id="tab-moi"'):html.index("</main>")]
    assert 'id="atoi"' in atoi and 'id="etat"' not in atoi and 'id="toutes"' not in atoi
    for ident in ("etat", "sessions", "fait", "demander", "fil-joindre"):
        assert f'id="{ident}"' in encours, ident
    assert encours.index('id="etat"') < encours.index('id="sessions"') < encours.index('id="demander"')
    for ident in ("plus-profil", "plus-focus", "plus-cap", "chantiers", "toutes", "comptes", "sante"):
        assert f'id="{ident}"' in moi and f'id="{ident}"' not in atoi + encours, ident
    for ident in ("flamme", "decisions", "en-cours", "i-modele", "actions", "rapports", "habitudes", "travail-sessions", "quota"):
        assert f'id="{ident}"' not in html, ident
    for ident in ("plus-souvenirs", "plus-sessions", "analyser-ouvrir", "missions"):  # todo #410
        assert f'id="{ident}"' not in html, ident
    # À toi gathers every decision: the À toi items, waiting sessions, accounts, profile facts, habits, Sunday reports
    for source in ("aToiSources.comptes.forEach", 'sessionsLues.filter(function (x) { return x.attend; })', "aToiSources.profil.forEach",
                   "aToiSources.habitudes.forEach", "carteRapports(rap, c)", 'lire(p[0], {})', '["etat-fusions",'):
        assert source in js, source
    # Approuver = the server's catalogue action, through prepare (sheet + confirmation), never a direct call
    assert "{ action: it.approuver, id: String(it.todo) }" in js and 'form("rapports-lot"' in js and 'form("habitude"' in js
    assert "function () { openLot([]); }" in js
    # « Demander à Ultron »: two catalogue entries (open session, new session; 🚀 Demande retired, todo #455), one field, one button
    assert 'var CIBLES = { s: "session-ecrire", n: "session-nouvelle" }' in js and '"demande"' not in js
    assert 'lire("faits-du-jour")' in js and 'lire("sessions")' in js and "ouvrirSession(x.nom, null)" in js
    # the squads' tab is gone (todo #458): its links land on À toi; old links (before #270, #466) still land on a screen
    assert 'escouades: "briefing"' in js and 'equipes: "briefing"' in js and "ESCOUADES" not in js
    assert 'accueil: "briefing"' in js and 'accueil: "briefing"' in (server.STATIC / "sw.js").read_text(encoding="utf-8")
    # terminal style kept (dark, green, no gradient, shadow, glow, blur or image); todo #466: proportional text, mono for codes
    css = (server.STATIC / "style.css").read_text(encoding="utf-8")
    for banned in ("gradient", "box-shadow", "text-shadow", "drop-shadow", "backdrop-filter", "@font-face"):
        assert banned not in css + html, banned
    assert html.count("<svg") == 1 and "stroke=\"currentColor\"" in html  # todo #355: the one line icon (envelope), no emoji
    assert "--font-mono:" in css and "ui-monospace" in css and "--font-titre" not in css
    assert "var(--font-texte)" in css.split("\nbody {")[1].split("}")[0] and 'button.go::before { content: "> "; }' not in css
    assert "button.texte" in css and "button.confirm" in css


def test_fil_tap_opens_the_message_and_filters():  # todo #466
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    carte = js[js.index("function carteFil("):js.index("function familleFil(")]
    assert "openTodo" not in carte and "ouvrirLecteur" not in carte and "lienCible(x)" in carte
    assert 'var FIL_FILTRES = [["question", "Questions"], ["fichier", "Fichiers"], ["alerte", "Alertes"], ["tout", "Tout"]];' in js
    assert "if (lot.length >= 3) n.push(groupeFil(lot))" in js


def test_chantier_card_has_no_squad_remark_nor_close_in_list():  # todo #466
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    fiche = js[js.index("function openFiche("):js.index('document.getElementById("retour")')]
    assert 'form("remarque"' not in fiche and "todoButtons" not in js and 'form("done"' not in fiche
    assert 'plie("+ Ajouter une todo"' in fiche and 'form("chantier-archiver", { sujet: d.sujet }, null, "texte")' in fiche


def test_decisions_name_catalogue_actions_on_a_todo():
    for kind, (action, pourquoi) in catalogue.DECISIONS.items():
        assert pourquoi and kind in chantier_core.KINDS
        if action:
            champs = catalogue.PAR_ID[action].champs
            assert [(c.nom, c.type) for c in champs] == [("id", "contexte")], kind
    assert catalogue.decision("inconnu") == {"approuver": None, "pourquoi": "Ça attend ton avis."}
    assert catalogue.PAR_ID["faits-du-jour"].type == "lecture"


def test_fil_links_are_built_with_dom_calls_only() -> None:
    """Todo #341: https URLs of a fil entry become safe links; no innerHTML, http stays text."""
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    start = js.index("function texteLiens(")
    body = js[start:js.index("function ligneFil(")]
    assert "innerHTML" not in body
    assert 'a.rel = "noopener noreferrer"' in body and 'a.target = "_blank"' in body and "a.textContent = u" in body
    assert 'var URL_FIL = /https:\\/\\/' in js  # https only: other schemes stay text
    assert "texteLiens(texte)" in js


def test_phrase_a_toi_zero_one_and_several_items():  # todo #345: the sentence is the server's
    fus, q, autre = {"kind": "fusionner"}, {"kind": "question", "question": "Q1"}, {"kind": "terminer"}
    assert views.phrase_a_toi([]) == "Rien ne t'attend."
    assert views.phrase_a_toi([fus]) == "Il me faut ton feu vert sur 1 branche."
    assert views.phrase_a_toi([q]) == "Il me faut ta réponse à 1 question."
    assert views.phrase_a_toi([fus, fus, q]) == "Il me faut ton feu vert sur 2 branches et ta réponse à 1 question."
    assert views.phrase_a_toi([fus, fus, q, q, autre, autre]) == (
        "Il me faut ton feu vert sur 2 branches, ta réponse à 2 questions et ton avis sur 2 autres points.")


def test_voice_button_tries_device_dictation_then_falls_back():
    """Todo #398: SpeechRecognition first (fr-FR, live), the classic recording stays as the safe fallback."""
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    assert "webkitSpeechRecognition" in js and 'r.lang = "fr-FR"' in js and "r.interimResults = true" in js
    assert "not-allowed|service-not-allowed|audio-capture|network" in js
    assert "enregistrement classique" in js and "function demarrerEnregistrement" in js
    # stopping the dictation sends at once (todo #398, essai réel du 25/10), no second tap on the input
    assert "function envoyerDictee" in js and "if (recu) return envoyerDictee(saisie);" in js and "requestSubmit" in js


def test_lire_button_is_on_the_last_fil_answer_not_in_the_terminal_view():  # todo #423
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    assert "function dessinerFilBulles" in js and "lireBouton(e.texte)" in js and "!d.chantier" in js
    assert "ecranLu" not in js and "raccourci lire" not in js
