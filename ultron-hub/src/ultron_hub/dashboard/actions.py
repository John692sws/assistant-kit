"""Actions of the dashboard (todos #131, #132, #251): a confirmation sheet, then the same `ultron-hub` command as Telegram.

What each action is (argv, fields, sheet, signed or not) lives ONLY in catalogue.py; this module is the one generic path.
Two steps, each a POST checked by server.py (device, Host, Origin, session cookie):
- `prepare` validates the request against the catalogue and returns the sheet (Quoi / Sur quoi / Effet / Risque) with a
  one-time token (random, CSRF_TTL_S, bound to the device, the session cookie, the action and its values);
- `confirm` spends that token (even on refusal) and only then, if the user confirmed, signs the proof (`signee` actions
  only) and runs the CLI in a subprocess, exactly like the plugin's hub_client. No business rule here: the CLI decides.
A `lecture` action skips the sheet (`read`): validated the same way, never signed, never journaled; its `data` reaches
the page only through the entry's whitelist (`catalogue.projeter`, sortie "donnees").
An `ouvert` action (todo #283: writing and keys of a session's Pilote) also goes through `read`, without a sheet: same
checks of the request (server.py), validated by the catalogue, signed here for that one send when `signee` (a fresh
timestamp per target, so that two quick presses of the same key are two proofs), journaled like any action.
Session cookie (todo #267): signed (session.py), so it survives a restart of the dashboard; only the CSRF tokens (120 s)
live in memory. An entry with `fond=True` (merges) runs in its own unit (fond.py): the answer is `suivi`, the job to poll.
"""

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ultron_hub import envoi, habitudes
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import load_settings
from ultron_hub.dashboard import catalogue, fond
from ultron_hub.dashboard import session as session_cookie
from ultron_hub.todo import closing

CSRF_TTL_S = 120  # as long as a signed button token
SESSION_TTL_S = 12 * 3600
MAX_PENDING = 32
TEXT_MAX = 400
DETAIL_MAX = 30  # lines listed on a sheet (todo #417)
# Re-exported: views.py and the tests read them here.
RUN_TIMEOUT_S, MERGE_TIMEOUT_S = catalogue.RUN_TIMEOUT_S, catalogue.MERGE_TIMEOUT_S
Refused = catalogue.Refused
log = get_logger("dashboard")


def default_cli() -> list[str]:
    return [sys.executable, "-P", "-m", "ultron_hub.cli"]


def cli_env(extra: dict[str, str]) -> dict[str, str]:
    """Minimal env, as the plugin's hub_client (plus what systemd-run --user and the tests need)."""
    home = str(Path.home())
    env = {"HOME": home, "PATH": f"{home}/.local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")}
    for key in ("PYTHONPATH", "ULTRON_HUB_ROOT"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return {**env, **extra}


def _short(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1] + "…"


@dataclass
class Pending:
    device: str
    session: str
    action: str
    args: dict[str, str]
    expires: float
    stdin: str | None = None


@dataclass
class Actions:
    cli: list[str] = field(default_factory=default_cli)
    clock: Callable[[], float] = time.time
    key: Callable[[], bytes] = closing.load_key
    journal: Callable[[str], None] | None = None
    session_key: Callable[[], bytes] = session_cookie.load_key
    _skey: bytes | None = field(default=None, repr=False)
    pending: dict[str, Pending] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    signed_at: dict[tuple[str, str, str], set[int]] = field(default_factory=dict)  # timestamps already used per target

    # -- session cookie (SameSite=Strict, set by GET /): signed, survives a restart ---------------------------

    def _session_key(self) -> bytes | None:
        if self._skey is None:
            try:
                self._skey = self.session_key()
            except (session_cookie.SessionKeyError, OSError) as exc:
                log.warning("dashboard session key refused: %s", exc)
        return self._skey

    def new_session(self, device: str) -> str | None:
        key = self._session_key()
        return session_cookie.issue(key, device, int(self.clock()) + SESSION_TTL_S) if key else None

    def session_ok(self, sid: str | None, device: str) -> bool:
        key = self._session_key()
        return key is not None and session_cookie.valid(key, sid, device, self.clock())

    def _prune(self, now: float) -> None:
        for k in [k for k, v in self.pending.items() if v.expires <= now]:
            self.pending.pop(k)

    # -- step 1: what will be done ----------------------------------------------------------------------------

    def prepare(self, device: str, session: str, body: dict[str, Any]) -> dict[str, Any]:
        action, args, stdin = catalogue.valider(body.get("action"), body)
        if action.type == "lecture":
            raise Refused("action de lecture : sans confirmation")
        if action.ouvert:
            raise Refused("action ouverte : envoyée sans feuille")
        sheet = catalogue.feuille(action, args, stdin)
        if action.detail:
            sheet["detail"] = self._detail(action, args)
            sheet["detail_brut"] = catalogue.PAR_ID[action.detail].donnees.get("lignes") is not None  # a text: shown as is
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.lock:
            self._prune(now)
            while len(self.pending) >= MAX_PENDING:
                self.pending.pop(next(iter(self.pending)))
            self.pending[token] = Pending(device, session, action.id, args, now + CSRF_TTL_S, stdin)
        return {"jeton": token, **sheet, "sur_quoi": _short(sheet["sur_quoi"]), "expire_s": CSRF_TTL_S}

    def _detail(self, action: catalogue.Action, args: dict[str, str]) -> list[str]:
        """todo #417: the reading `action.detail` again; its lines go on the sheet only if its fingerprint is the signed one.
        todo #429: a reading with fields gets the action's values; its `lignes` (a text) are listed as is, never cut."""
        reading = catalogue.PAR_ID[action.detail]
        data = self._lire_detail(reading, {c.nom: args[c.nom] for c in reading.champs})
        if not isinstance(data, dict) or data.get("empreinte") != args.get("empreinte"):
            raise Refused("la liste a changé depuis son affichage : rouvre la carte, rien n'a été lancé")
        if "lignes" in data:
            lines = [str(x) for x in data.get("lignes") or []]
            if not lines or len(lines) > habitudes.LIGNES_MAX or any(len(x) > habitudes.LIGNE_MAX for x in lines):
                raise Refused(f"texte vide ou trop long pour la feuille ({habitudes.LIGNES_MAX} lignes de "
                              f"{habitudes.LIGNE_MAX} caractères au plus) : rien n'a été lancé")
            return lines
        lines = [_short(x.get("texte")) for x in data.get("actions") or [] if isinstance(x, dict)]
        if not lines or len(lines) > DETAIL_MAX:
            raise Refused(f"liste vide ou trop longue ({DETAIL_MAX} au plus) : rien n'a été lancé")
        return lines

    def _lire_detail(self, reading: catalogue.Action, args: dict[str, str]) -> Any:
        _, args, stdin = catalogue.valider_dans(reading, args)
        result = self._run(catalogue.commande(reading, args, stdin), {}, timeout=reading.delai)
        if result.get("status") != "ok":
            raise Refused(_short(result.get("error")) or "lecture impossible : rien n'a été lancé")
        return result.get("data")

    def read(self, device: str, session: str, body: dict[str, Any]) -> dict[str, Any]:
        """A `lecture` action (same checks, no sheet, never signed) or an `ouvert` one (no sheet, signed, journaled)."""
        action, args, stdin = catalogue.valider(body.get("action"), body)
        if action.type != "lecture" and not action.ouvert:
            raise Refused("action à confirmer : rien n'a été lancé")
        return self._execute(Pending(device, session, action.id, args, self.clock(), stdin))

    # -- step 2: the user confirmed (or not) ------------------------------------------------------------------

    def confirm(self, device: str, session: str, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:  # spent at once, whatever comes next: a token never serves twice
            p = self.pending.pop(str(body.get("jeton") or ""), None)
        if p is None:
            raise Refused("jeton inconnu ou déjà utilisé : rien n'a été lancé")
        if self.clock() >= p.expires:
            raise Refused("jeton périmé : rien n'a été lancé, recommence")
        if p.device != device or p.session != session:
            raise Refused("jeton d'un autre appareil : rien n'a été lancé")
        if body.get("confirme") is not True:
            return {"ok": False, "annule": True, "resume": "annulé : rien n'a été lancé"}
        return self._execute(p)

    def _sign(self, p: Pending, action: str, target: str) -> dict[str, str]:
        """Signed here only: device checked on this request, then CSRF token spent and user confirmed, or an `ouvert` action."""
        return {closing.TOKEN_ENV: closing.sign_dashboard(self.key(), p.device, action, target, self._timestamp(p.device, action, target))}

    def _timestamp(self, device: str, action: str, target: str) -> int:
        """Now, or the latest second not yet used for this target: the proof (one per token) is never the same twice."""
        now = int(self.clock())
        with self.lock:
            for k in [k for k, v in self.signed_at.items() if not any(now - t < CSRF_TTL_S for t in v)]:
                self.signed_at.pop(k)
            used = self.signed_at.setdefault((device, action, target), set())
            ts = now
            while ts in used:
                ts -= 1
            if now - ts >= closing.TOKEN_TTL_S // 2:  # a burst of the same key: refused rather than a stale token
                raise Refused("trop d'envois identiques d'affilée : attends un instant")
            used.add(ts)
            used.intersection_update({t for t in used if now - t < CSRF_TTL_S})
        return ts

    def _execute(self, p: Pending) -> dict[str, Any]:
        action = catalogue.PAR_ID[p.action]
        argv = catalogue.commande(action, p.args, p.stdin)
        extra = {}
        if action.type == "signee":  # the same signed target as the Telegram plugin
            extra = self._sign(p, action.signe or "", catalogue.cible(action, p.args, p.stdin))
        if action.fond:
            return self._background(p, action, extra)
        return self._finish(p, action, self._run(argv, extra, stdin=p.stdin, timeout=action.delai))

    def _background(self, p: Pending, action: catalogue.Action, extra: dict[str, str]) -> dict[str, Any]:
        """Launched in its own unit (a merge may restart this very dashboard); the page polls the `suivi` reading."""
        settings = load_settings()
        now = self.clock()
        job = fond.new_id(now)
        args = {k: v for k, v in p.args.items() if k != action.stdin}  # the stdin field is kept once, under "stdin"
        fond.write(settings, job, {"v": 1, "etat": "en_cours", "action": action.id, "args": args, "device": p.device,
                                   "lance": now, "delai": action.delai, "stdin": p.stdin})
        try:
            self._launch(settings, job, extra, action.delai)
        except Exception as exc:  # noqa: BLE001  the signed token dies with this refusal (120 s, never shown)
            log.warning("dashboard background launch failed action=%s: %s", action.id, type(exc).__name__)
            out = {"ok": False, "resume": "", "erreur": _short(f"lancement en fond impossible : {exc}")}
            fond.write(settings, job, {"v": 1, "etat": "fini", "action": action.id, "args": args, "device": p.device,
                                       "lance": now, "delai": action.delai, "fini": time.time(), "resultat": out})
            return out
        log.info("dashboard action=%s by=%s background job=%s", p.action, closing.DASHBOARD_ACTOR + p.device, job)
        return {"ok": True, "resume": "lancé en fond", "erreur": None, "suivi": job}

    def _launch(self, settings: Any, job: str, extra: dict[str, str], delay_s: int) -> None:
        fond.launch(settings, job, cli_env(extra), delay_s)

    def _finish(self, p: Pending, action: catalogue.Action, result: dict[str, Any]) -> dict[str, Any]:
        by = closing.DASHBOARD_ACTOR + p.device
        ok = result.get("status") == "ok"
        if action.type != "lecture":  # polling readings (session, session-ecran...) were 98 % of the log
            log.info("dashboard action=%s by=%s ok=%s", p.action, by, ok)
        elif not ok:
            log.warning("dashboard reading failed action=%s by=%s", p.action, by)
        if ok and action.type != "lecture":  # a reading changes nothing: never in the journal
            self._journal(f"{catalogue.ligne_journal(action, p.args, p.stdin)} depuis le dashboard (by={by})")
        out = {"ok": ok, "resume": _short(result.get("summary")), "erreur": _short(result.get("error")) or None}
        data = result.get("data") or {}
        if ok and "id" in action.sortie:
            out["id"] = _short(data.get("id"))
        if "texte" in action.sortie:  # the CLI's own text (cause, files, what to do)
            out["texte"] = str(data.get("text") or data.get("what") or "")[:3000] or None
        if "duree" in action.sortie:  # video probe: seconds, or null when unreadable
            value = data.get("duration")
            out["duree"] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None
        if "empreinte" in action.sortie:  # montage retouch: what `montage render --apply` takes
            out["empreinte"] = str(data.get("empreinte") or "")[:64] or None
        if ok and "donnees" in action.sortie:  # whitelisted (catalogue.projeter), whole or refused: never cut
            donnees = catalogue.projeter(action.donnees, data)
            if len(json.dumps(donnees, ensure_ascii=False)) > catalogue.DONNEES_MAX:
                out.update(ok=False, erreur="réponse trop longue pour la page")
            else:
                out["donnees"] = donnees
        if ok and action.livre:
            self._livrer(action, data)
        elif not ok and action.livre_echec:  # nobody is watching a long action: the failure is delivered too
            self._livrer(action, {"text": out["erreur"] or out["resume"] or "échec sans détail"}, action.livre_echec)
        if "apres_fusion" in action.sortie:  # the actions waiting for ✅ Exécuter
            out["apres_fusion"] = sum(1 for x in data.get("after_merge") or [] if x.get("status") == "pending")
        return out

    def _livrer(self, action: catalogue.Action, data: dict[str, Any], titre: str = "") -> None:
        """A long result is delivered like Telegram's flows do: text + attachment through `envoi` (push, Fil, Telegram).
        Never raises: the action itself succeeded."""
        body = str(data.get("text") or data.get("title") or "").strip()
        lines = [(titre or action.livre) + (f"\n\n{body}" if body else "")]
        path = data.get("path")
        if isinstance(path, str) and path.startswith("/"):
            lines.append(f"MEDIA:{path}")
        try:
            envoi.envoyer("\n".join(lines), settings=load_settings())
        except Exception as exc:  # noqa: BLE001
            log.warning("dashboard delivery failed action=%s: %s", action.id, type(exc).__name__)

    def _run(self, argv: list[str], extra: dict[str, str], *, stdin: str | None = None,
             timeout: int = RUN_TIMEOUT_S) -> dict[str, Any]:
        try:
            proc = subprocess.run([*self.cli, *argv], input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=timeout, env=cli_env(extra),
                                  cwd=str(load_settings().root), check=False)
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": f"délai dépassé ({timeout} s)"}
        except OSError as exc:
            return {"status": "error", "error": f"ultron-hub introuvable ({exc.strerror})"}
        try:
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            return {"status": "error", "error": f"réponse illisible d'ultron-hub (code {proc.returncode})"}
        return payload if isinstance(payload, dict) else {"status": "error", "error": "réponse inattendue d'ultron-hub"}

    def _journal(self, text: str) -> None:
        if self.journal is not None:
            self.journal(text)
            return
        from ultron_hub.esprit import store
        store.record(load_settings(), "dashboard", "action", text)
