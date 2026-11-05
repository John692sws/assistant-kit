"""Run deploy/navigateur/capturer.py against a fake Playwright (todo #253): catches call/signature drift without a browser."""

import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class FakeLocator:
    first = property(lambda self: self)

    def count(self):
        return 1

    def is_visible(self):
        return True

    def click(self, *a, **k):
        pass

    def fill(self, *a, **k):
        pass

    def evaluate(self, *a, **k):
        pass

    def locator(self, *a, **k):
        return self

    def nth(self, i):
        return self

    def scroll_into_view_if_needed(self):
        pass


class FakePage:
    def __init__(self):
        self.shots = []

    def goto(self, *a, **k):
        pass

    def wait_for_selector(self, *a, **k):
        pass

    def wait_for_timeout(self, *a, **k):
        pass

    def click(self, *a, **k):
        pass

    def locator(self, *a, **k):
        return FakeLocator()

    def eval_on_selector_all(self, *a, **k):
        return ["a_toi", "todos"]

    def screenshot(self, path):
        self.shots.append(path)

    def route(self, *a, **k):  # todo #293: the session without a question
        pass

    unroute = route

    def evaluate(self, js, *args):
        return [{"type": "cible", "element": "x"}] if "getBoundingClientRect" in js else None


class FakeContext:
    def new_page(self):
        return FakePage()

    def close(self):
        pass


class FakeBrowser:
    def new_context(self, **k):
        return FakeContext()

    def close(self):
        pass


class FakePlaywright:
    chromium = types.SimpleNamespace(launch=lambda: FakeBrowser())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_capturer_runs_with_fake_playwright(tmp_path, monkeypatch, capsys):
    pw = types.ModuleType("playwright")
    sync = types.ModuleType("playwright.sync_api")
    sync.Error = type("Error", (Exception,), {})
    sync.sync_playwright = lambda: FakePlaywright()
    monkeypatch.setitem(sys.modules, "playwright", pw)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync)
    spec = importlib.util.spec_from_file_location("capturer_fake", ROOT / "deploy/navigateur/capturer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "argv", ["capturer.py", "--url", "http://x", "--sortie", str(tmp_path)])
    assert mod.main() == 0
    res = json.loads(capsys.readouterr().out)
    assert res["ignores"] == []
    names = {Path(f).name for f in res["fichiers"]}
    for screen in ("todos-liste", "todo-fiche", "feuille-abandon", "apres-fusion", "lot", "feuille-fusion-lot", "analyser", "analyser-confirmation",
                   "fiche-chantier", "fiche-chantier-bas", "atoi-bas", "encours-bas", "moi", "moi-bas", "feuille-approuver",  # todo #466
                   "fil-message", "fil-fondue", "fil-groupe", "fil-questions",
                   "profil", "profil-confirmation", "focus", "cap",  # souvenirs: left Moi (todo #410)
                   "savoir-propositions", "savoir-verification", "savoir-confirmation"):
        assert f"{screen}-sombre.png" in names
    assert not [n for n in names if "-clair" in n]  # todo #288: dark only
    assert {c["ecran"] for c in res["controles"]} >= {"lot", "todos-liste"}
