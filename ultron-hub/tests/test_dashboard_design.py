"""Design system guard (todo #252): static/ may not hold a literal colour or spacing outside the `:root` token definitions."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "ultron_hub" / "dashboard" / "static"
# token blocks: `:root` (and `:root[...]` / `:root:not([...])` variants, should a token block ever need one)
ROOT_BLOCK = re.compile(r":root(?:\[[^\]]*\]|:not\(\[[^\]]*\]\))*\s*\{[^{}]*\}")
COMMENT = re.compile(r"/\*.*?\*/", re.S)
COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla|hwb|oklch|oklab)\(")
SPACING = re.compile(r"(?:^|[{;\s])((?:padding|margin)(?:-[a-z]+)?|gap|row-gap|column-gap|top|left|right|bottom|inset|font-size|font|border-radius)"
                     r"\s*:\s*([^;}]+)")
LITERAL_LENGTH = re.compile(r"(?<![\w-])-?(?:[1-9]\d*|\d*\.\d+)(?:px|rem|em)\b")


def violations(css: str) -> list[str]:
    """Literal colours and spacings left after removing the `:root` token blocks and comments."""
    css = ROOT_BLOCK.sub("", COMMENT.sub("", css))
    found = [f"couleur littérale {m.group(0)}" for m in COLOR.finditer(css)]
    for m in SPACING.finditer(css):
        # a literal inside calc(...) or beside var(...) is still a literal
        found += [f"{m.group(1)}: {v.group(0)} littéral" for v in LITERAL_LENGTH.finditer(m.group(2))]
    return found


def test_style_css_defines_tokens() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    roots = "".join(ROOT_BLOCK.findall(css))
    for token in ("--bg", "--accent", "--text", "--sp-8", "--sp-16", "--r-card", "--r-btn", "--fs-15", "--font-mono"):
        assert f"{token}:" in roots, token


def test_style_css_has_no_literal_colour_or_spacing() -> None:
    assert violations((STATIC / "style.css").read_text(encoding="utf-8")) == []


def test_html_and_js_have_no_inline_literal_colour_or_spacing() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    inline = re.findall(r'style="([^"]*)"', html) + re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    assert [v for css in inline for v in violations(css + ";")] == []
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    styled = [ln for ln in js.splitlines() if re.search(r"\.style\.(?!transform|opacity)\w+\s*=|cssText", ln)]
    assert [ln for ln in styled if COLOR.search(ln)] == []


def test_detector_catches_literals() -> None:
    assert violations(".a { color: #fff; }")
    assert violations(".a { background: rgba(0,0,0,.5); }")
    assert violations(".a { padding: 8px 0; }")
    assert violations(".a { margin: calc(var(--sp-4) + 3px); }")
    assert violations(".a { border-radius: 12px; font-size: 14px; }")
    assert violations(":root { --x: 1px; } .a { gap: 0 4px; }")
    assert violations(".a { padding: var(--sp-8) 0; color: var(--text); border-radius: var(--r-btn); }") == []
    assert violations(":root { --c: #fff; --sp-2: 2px; } @media (x) { :root { --c: #000; } } .a { margin: 0; }") == []
    assert violations(':root[data-theme="clair"] { --c: #fff; } @media (x) { :root:not([data-theme="sombre"]) { --c: #000; } }') == []
    assert violations('.a[data-theme="clair"] { color: #fff; }')


def test_dark_only_no_light_theme_nor_switch() -> None:
    """Todo #281: dark only. No light tokens, no theme switch in the page, browser bar and manifest dark."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "prefers-color-scheme" not in css + html + js
    assert "data-theme" not in css + html + js and "ultron-theme" not in js and "themeApply" not in js
    assert 'id="theme"' not in html and "Thème" not in html
    assert '<meta name="color-scheme" content="dark">' in html
    assert re.findall(r'<meta name="theme-color" content="([^"]+)"', html) == ["#0c0c0c"]
    manifest = (STATIC / "manifest.webmanifest").read_text(encoding="utf-8")
    assert '"theme_color": "#0c0c0c"' in manifest and '"background_color": "#0c0c0c"' in manifest
    bg = re.search(r":root\s*\{[^{}]*--bg:\s*(#[0-9a-fA-F]{6})", css).group(1)
    assert bg == "#0c0c0c" and len(re.findall(r"--bg\s*:", css)) == 1


def test_counter_keeps_sessions_and_sub_agents_apart() -> None:  # todo #400: 1 session + 3 sub-agents is not « 4 agents »
    import json
    import re
    import shutil
    import subprocess

    import pytest
    if not shutil.which("node"):
        pytest.skip("node missing")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    fn = re.search(r"function compterAgents\(\) \{.*?\n\}", js, re.S).group(0)
    prog = ("var etatLu = {}, agentsLus = null, sessionsLues = [{sous_agents: 3}, {sous_agents: null}];\n" + fn +
            "\ncompterAgents(); console.log(JSON.stringify(agentsLus));")
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == {"sessions": 2, "agents": 3}


def test_screen_lock_asked_at_tap_retaken_and_refusal_said() -> None:  # todo #332, panne du 30/10: the screen slept mid-vocal
    import json
    import re
    import shutil
    import subprocess

    import pytest
    if not shutil.which("node"):
        pytest.skip("node missing")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    start = js.index("var veille = null")
    fns = js[start:js.index("document.addEventListener(\"visibilitychange\"", start)]
    bascule = re.search(r"function basculerVocal\(session\) \{.*?\n\}", js, re.S).group(0)
    mic = re.search(r'mic\.addEventListener\("click", function \(\) \{(.*?)\n    \}\);', js, re.S).group(1)
    assert mic.strip().startswith("if (!vocal.rec) prendreVeille();")  # before chargerCatalogue, inside the tap
    prog = """
var toasts = [], demandes = 0, refuser = false, lacher = [];
var vocal = { rec: null }, document = { visibilityState: "visible" };
function toast(m) { toasts.push(m); }
var navigator = { wakeLock: { request: function () {
  demandes++;
  if (refuser) return Promise.reject({ name: "NotAllowedError" });
  var l = { h: [], addEventListener: function (n, f) { this.h.push(f); }, release: function () { return Promise.resolve(); } };
  lacher.push(l); return Promise.resolve(l);
} } };
var ordre = [];
function demarrerVocal() { ordre.push("demarrer:" + demandes); }
""" + fns + bascule + """
(async function () {
  basculerVocal("s"); await null; await null;
  var r = { auTap: ordre[0], tenu: !!veille };
  vocal.rec = {}; lacher[0].h.forEach(function (f) { f(); }); await null; await null;   // iOS drops it while visible
  r.repris = demandes === 2 && !!veille;
  relacherVeille(); vocal.rec = null;
  refuser = true; basculerVocal("s"); await null; await null; prendreVeille(); await null; await null;
  r.toasts = toasts.length; r.raison = /NotAllowedError/.test(toasts[0] || "");
  console.log(JSON.stringify(r));
})();
"""
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == {"auTap": "demarrer:1", "tenu": True, "repris": True, "toasts": 1, "raison": True}


def test_video_proposals_have_one_signed_tout_ranger() -> None:  # todo #477: the page button of the lot of #442
    import json
    import re
    import shutil
    import subprocess

    import pytest
    if not shutil.which("node"):
        pytest.skip("node missing")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "valider sur Telegram" not in js
    props = re.search(r"function propositions\(source\) \{.*?\n\}", js, re.S).group(0)
    assert 'p.source === "video" && p.id) out.push(rangerLot(p.id))' in props
    fn = re.search(r"function rangerLot\(id\) \{.*?\n\}", js, re.S).group(0)
    prog = """
var forms = [];
function el(tag, cls, txt) { return { cls: cls, textContent: txt || "", kids: [], appendChild: function (k) { this.kids.push(k); return k; } }; }
function form(id, ctx) { forms.push([id, ctx]); return { form: id }; }
var reponses = { a: { actions: [{ n: 1 }, { n: 2 }], empreinte: "0123456789abcdef" }, b: { actions: [], empreinte: null } };
function lire(action, ctx) { return Promise.resolve(reponses[ctx.id]); }
""" + fn + """
(async function () {
  var a = rangerLot("a"), b = rangerLot("b"); await null; await null;
  console.log(JSON.stringify({ forms: forms, a: a.kids.map(function (k) { return k.textContent || k.form; }), b: b.kids[0].textContent }));
})();
"""
    out = json.loads(subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout)
    assert out["forms"] == [["video-lot", {"id": "a", "empreinte": "0123456789abcdef"}]]
    assert out["a"][0].startswith("📥 2 proposition(s)") and out["a"][1] == "video-lot"
    assert "Rien d'autre" in out["b"]
