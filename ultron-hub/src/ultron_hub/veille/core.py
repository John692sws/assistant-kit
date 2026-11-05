"""Veilles (todo #85): silent watchers.

A veille is a small script that looks at one thing. A timer runs `ultron-hub veille run`; ultron-hub writes to
Telegram only when the script reports something new. Pure script at run time: no Claude involved.

Script contract: stdin = {"state": <previous state or null>, "pages": [...] if urls}; stdout = {"new": bool,
"message": str, "state": any}. The script runs in a sandbox (todo #87, `veille/sandbox.py`).
"""

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ultron_hub.common.errors import InvalidRequestError, UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.veille import sandbox

log = get_logger("veille")

VEILLES_ENV = "ULTRON_VEILLES_DIR"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]")
EVERY_MIN, EVERY_MAX = 5, 1440
TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_DEFAULT = 5, 300, 60
DESCRIPTION_MAX = 80
SCRIPT_MAX = 256 * 1024
OUTPUT_MAX = 64 * 1024
MESSAGE_MAX = 1000
REASON_MAX = 200
MAX_FAILURES = 3
ENV_PATH = "/usr/local/bin:/usr/bin:/bin"

CANAUX = ("telegram", "fil")  # `fil`: the news reaches the Fil (push + history) only, never Telegram (todo #393)
CONFIG, SCRIPT, STATE, STATUS = "veille.json", "check", "state.json", "status.json"
LOCK, TRASH = ".lock", ".corbeille"


class CheckFailed(UltronHubError):
    """The script did not keep its contract (exit code, timeout, output, fingerprint)."""


def root() -> Path:
    override = os.environ.get(VEILLES_ENV)
    return Path(override) if override else Path.home() / "ultron" / "veilles"


def folder(name: str) -> Path:
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
        raise InvalidRequestError("nom de veille invalide (minuscules, chiffres, tirets, 3 à 40 caractères)")
    return root() / name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write_json(path: Path, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _one_line(text: Any, limit: int) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bounded(value: Any, low: int, high: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidRequestError(f"{what} : entier de {low} à {high} attendu")
    return value


def add(name: str, script: Path | bytes, every: int, description: str, timeout: int = TIMEOUT_DEFAULT,
        lecture: list[str] | None = None, urls: list[str] | None = None) -> dict[str, Any]:
    """`script` = a file to copy, or its bytes already read and checked (`veille creer`, todo #86)."""
    path = folder(name)
    lecture, urls = sandbox.check_lecture(lecture or []), sandbox.check_urls(urls or [])
    _bounded(every, EVERY_MIN, EVERY_MAX, "fréquence (minutes)")
    _bounded(timeout, TIMEOUT_MIN, TIMEOUT_MAX, "délai (secondes)")
    description = _one_line(description, DESCRIPTION_MAX + 1)
    if not description or len(description) > DESCRIPTION_MAX:
        raise InvalidRequestError(f"description obligatoire, {DESCRIPTION_MAX} caractères au plus")
    if isinstance(script, bytes):
        data = script
    else:
        script = Path(script)
        if script.is_symlink() or not script.is_file():
            raise InvalidRequestError("le script doit être un fichier ordinaire (pas un lien)")
        data = script.read_bytes()
    if len(data) > SCRIPT_MAX or not data.startswith(b"#!"):
        raise InvalidRequestError(f"le script doit commencer par #! et faire {SCRIPT_MAX // 1024} Kio au plus")
    if path.exists():
        raise InvalidRequestError(f"la veille {name} existe déjà")
    root().mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    target = path / SCRIPT
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    config = {"nom": name, "description": description, "every_min": every, "timeout_s": timeout,
              "sha256": fingerprint(data), "lecture": lecture, "urls": urls, "created_at": _now()}
    _write_json(path / CONFIG, config)
    _write_json(path / STATUS, {"failures": 0, "paused": False})
    return config


def seed_state(name: str, state: Any) -> None:
    """Starting state of a new veille (its trial run): the first pass of the timer is then a real comparison."""
    load(name)
    _write_json(folder(name) / STATE, state)


def load(name: str) -> dict[str, Any]:
    path = folder(name)
    config = _read_json(path / CONFIG, None)
    if not isinstance(config, dict):
        raise InvalidRequestError(f"veille inconnue : {name}")
    return config


def set_access(name: str, lecture: list[str] | None = None, urls: list[str] | None = None) -> dict[str, Any]:
    """Replaces the given lists (None = unchanged): what the script may read, the pages it receives.
    The script and its sha256 do not change."""
    config = load(name)
    config.update(lecture=sandbox.check_lecture(config.get("lecture") if lecture is None else lecture),
                  urls=sandbox.check_urls(config.get("urls") if urls is None else urls))
    _write_json(folder(name) / CONFIG, config)
    return config


def set_canal(name: str, canal: str) -> dict[str, Any]:
    """Where the news of a veille goes: `telegram` (default, key removed) or `fil` (Fil of the dashboard only)."""
    if canal not in CANAUX:
        raise InvalidRequestError(f"canal inconnu : {canal} (attendu : {', '.join(CANAUX)})")
    config = load(name)
    if canal == "fil":
        config["canal"] = "fil"
    else:
        config.pop("canal", None)
    _write_json(folder(name) / CONFIG, config)
    return config


def status(name: str) -> dict[str, Any]:
    data = _read_json(folder(name) / STATUS, {})
    return data if isinstance(data, dict) else {}


def names() -> list[str]:
    base = root()
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if NAME_RE.fullmatch(p.name) and not p.is_symlink() and (p / CONFIG).is_file())


def entries() -> list[dict[str, Any]]:
    rows = []
    for name in names():
        config, st = load(name), status(name)
        rows.append({"nom": name, "description": config.get("description", ""),
                     "every_min": config.get("every_min"), "last_run": st.get("last_run"),
                     "last_new": st.get("last_new"), "failures": st.get("failures", 0),
                     "paused": bool(st.get("paused")), "pause_reason": st.get("pause_reason")})
    return rows


def pause(name: str, reason: str = "mise en pause à la main") -> dict[str, Any]:
    load(name)
    st = status(name)
    st.update(paused=True, pause_reason=_one_line(reason, REASON_MAX))
    _write_json(folder(name) / STATUS, st)
    return st


def resume(name: str) -> dict[str, Any]:
    load(name)
    st = status(name)
    st.update(paused=False, pause_reason=None, failures=0, alerted=False, last_error=None)
    _write_json(folder(name) / STATUS, st)
    return st


def relancer(name: str) -> dict[str, Any]:
    """One trial run of a paused veille (todo #223): the state is not advanced and nothing is sent.
    Success puts it back in service (`resume`); a failure raises CheckFailed and leaves it paused."""
    load(name)
    if not status(name).get("paused"):
        raise InvalidRequestError(f"la veille {name} n'est pas en pause")
    check(name, _read_json(folder(name) / STATE, None))
    return resume(name)


def remove(name: str) -> Path:
    load(name)
    trash = root() / TRASH
    trash.mkdir(mode=0o700, exist_ok=True)
    target = trash / f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    os.rename(folder(name), target)
    return target


def check(name: str, state: Any) -> dict[str, Any]:
    """Run the script once. Returns {"new", "message", "state"} or raises CheckFailed."""
    path, config = folder(name), load(name)
    script = path / SCRIPT
    if script.is_symlink() or not script.is_file():
        raise CheckFailed("script absent")
    if fingerprint(script.read_bytes()) != config.get("sha256"):
        raise CheckFailed("script modifié depuis son ajout (empreinte différente)")
    timeout = config.get("timeout_s", TIMEOUT_DEFAULT)
    try:  # veille.json may have been edited by hand: its accesses are checked again before each run
        lecture = sandbox.check_lecture(config.get("lecture") or [])
        urls = sandbox.check_urls(config.get("urls") or [])
    except InvalidRequestError as exc:
        raise CheckFailed(f"accès refusé : {exc}") from None
    payload: dict[str, Any] = {"state": state}
    if urls:
        payload["pages"] = sandbox.fetch_pages(urls)
    return execute(script, path, lecture, name, payload, timeout)


def _last_line(handle, tail: int = 2048) -> str:
    """Last non-empty line of the last `tail` bytes of an open file ("" when there is none)."""
    handle.seek(0, os.SEEK_END)
    handle.seek(max(0, handle.tell() - tail))
    lines = handle.read().decode("utf-8", "replace").splitlines()
    return next((line.strip() for line in reversed(lines) if line.strip()), "")


def execute(script: Path, path: Path, lecture: list[str], name: str, payload: dict[str, Any],
            timeout: int) -> dict[str, Any]:
    """One sandboxed run of `script` (folder `path`, writable `path/travail`) with `payload` on stdin."""
    work = path / sandbox.WORK
    if work.is_symlink():
        raise CheckFailed("dossier de travail remplacé par un lien")
    work.mkdir(mode=0o700, exist_ok=True)
    env = {"HOME": str(Path.home()), "PATH": ENV_PATH, "LANG": "C.UTF-8", "ULTRON_VEILLE": name}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(sandbox.argv(script, path, lecture, name), stdin=subprocess.PIPE, stdout=out,
                                    stderr=err, cwd=work, env=env, start_new_session=True,
                                    preexec_fn=sandbox.limits(timeout))
        except OSError as exc:
            raise CheckFailed(f"bac à sable indisponible : {exc}") from None
        try:
            proc.communicate(json.dumps(payload).encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise CheckFailed(f"délai dépassé ({timeout} s)") from None
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        if proc.returncode != 0:
            detail = _one_line(_last_line(err), 120)  # a Python trace ends with the exception: that is the cause
            raise CheckFailed(f"code de sortie {proc.returncode}" + (f" : {detail}" if detail else ""))
        if out.tell() > OUTPUT_MAX:
            raise CheckFailed(f"sortie trop longue (plus de {OUTPUT_MAX // 1024} Kio)")
        out.seek(0)
        raw = out.read()
    return parse_output(raw)


def parse_output(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CheckFailed("sortie qui n'est pas du JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("new"), bool) or "state" not in data:
        raise CheckFailed("sortie JSON sans les champs new (booléen) et state")
    message = data.get("message") or ""
    if not isinstance(message, str):
        raise CheckFailed("message doit être un texte")
    message = message.strip()
    if data["new"] and not message:
        raise CheckFailed("new vaut true mais le message est vide")
    if len(message) > MESSAGE_MAX:
        raise CheckFailed(f"message de plus de {MESSAGE_MAX} caractères")
    return {"new": data["new"], "message": message, "state": data["state"]}


def is_due(config: dict[str, Any], st: dict[str, Any], now: float) -> bool:
    last = st.get("last_run")
    if not last:
        return True
    try:
        elapsed = now - datetime.fromisoformat(last).timestamp()
    except ValueError:
        return True
    return elapsed >= config.get("every_min", EVERY_MAX) * 60 - 30  # timer jitter must not skip a turn


@contextmanager
def _run_lock() -> Iterator[bool]:
    base = root()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(base / LOCK, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def run(send: Callable[[str], None], journal: Callable[[str], None], only: str | None = None,
        force: bool = False) -> dict[str, Any]:
    """One pass of the timer. `send` delivers a Telegram message (raises on failure); `journal` is best effort."""
    report: dict[str, Any] = {"ran": [], "new": [], "failed": [], "skipped": [], "busy": False}
    selected = [only] if only else names()
    if only:
        load(only)
    if not selected:
        return report
    with _run_lock() as locked:
        if not locked:
            report["busy"] = True
            return report
        now = time.time()
        for name in selected:
            config, st = load(name), status(name)
            if st.get("paused") or not (force or is_due(config, st, now)):
                report["skipped"].append(name)
                continue
            _run_one(name, config, st, send, journal, report)
    return report


def _run_one(name, config, st, send, journal, report) -> None:
    path = folder(name)
    first = not (path / STATE).exists()
    previous = _read_json(path / STATE, None)
    report["ran"].append(name)
    st["last_run"] = _now()
    try:
        result = check(name, previous)
    except CheckFailed as exc:
        _failed(name, config, st, str(exc), send, journal, report)
        return
    st.update(failures=0, last_error=None)
    if result["new"] and not first:
        text = f"🔔 {config['description']} : {result['message']}"
        try:
            if config.get("canal") == "fil":
                send(text, fil_seul=True)
            else:
                send(text)
        except Exception as exc:  # the state is not advanced: the news goes out next time
            log.warning("veille %s: message not sent (%s)", name, exc)
            st["last_error"] = "message Telegram non envoyé, nouvel essai au prochain passage"
            _write_json(path / STATUS, st)
            report["failed"].append({"nom": name, "raison": st["last_error"]})
            return
        st["last_new"] = st["last_run"]
        report["new"].append({"nom": name, "message": result["message"]})
        journal(f"Veille {name} : {_one_line(result['message'], 160)}")
    _write_json(path / STATE, result["state"])
    _write_json(path / STATUS, st)


def alerte_cle(name: str) -> str:
    """Stable key of the "paused after failures" alert (todo #219), settled by `resume`."""
    return f"veille:{name}:pause"


def _failed(name, config, st, reason, send, journal, report) -> None:
    st["failures"] = int(st.get("failures", 0)) + 1
    st["last_error"] = _one_line(reason, REASON_MAX)
    report["failed"].append({"nom": name, "raison": st["last_error"]})
    log.warning("veille %s failed (%d): %s", name, st["failures"], st["last_error"])
    if st["failures"] >= MAX_FAILURES:
        st.update(paused=True, pause_reason=f"{MAX_FAILURES} échecs de suite : {st['last_error']}")
        if not st.get("alerted"):
            try:
                send(f"⚠️ Veille « {config.get('description', name)} » en pause après {MAX_FAILURES} échecs : "
                     f"{st['last_error']}\nPour la relancer : ultron-hub veille resume {name}",
                     cle_alerte=alerte_cle(name))
                st["alerted"] = True
            except Exception as exc:
                log.warning("veille %s: pause alert not sent (%s)", name, exc)
            journal(f"Veille {name} en pause après {MAX_FAILURES} échecs")
    _write_json(folder(name) / STATUS, st)
