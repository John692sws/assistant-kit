"""Two Claude Pro accounts used in turn (todo #263): ONE Claude Code config, the credentials file is swapped.

`~/.claude/.credentials.json` is read and copied here only (plain Python of ultron-hub, never a Claude session, the same
exception as usage/collect.py): its content is never printed, logged, cached elsewhere nor refreshed. Copies live in
~/.config/ultron-hub/comptes/<a|b>.json (folder 0700, files 0600, atomic writes); comptes.json keeps the dates, the last
switch, the spent proofs and, per account, a hash of its id and, since todo #339, its e-mail address (shown instead of
"account A/B"; this 0600 file only, never the shared journal nor the logs; never a token).

Which account is active: same bytes as a copy, else same refresh token (access token renewed), else the account behind the
token (`/api/oauth/profile`, undocumented, the endpoint Claude Code itself calls) compared to the one recorded at
registration. Limit (docs/compte.md): a Claude session opened before a switch keeps the old token in memory and, when it
renews it, writes the OLD account back into the file; `etat` reports it, only restarting the sessions prevents it.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.usage import collect
from ultron_hub.usage.collect import UsageError

SLOTS = ("a", "b")
SLOT_RE = re.compile(r"[ab]")
# From pwd, not $HOME (as closing.KEY_PATH): `HOME=/tmp/x` must not point the switch at files an agent wrote.
HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
ACTIVE = HOME / ".claude" / ".credentials.json"
DIR = HOME / ".config" / "ultron-hub" / "comptes"
META = "comptes.json"
LOCK = ".verrou"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
MAX_FILE = 64 * 1024
PROOFS_MAX = 200
AUTO_MAX = 50
IDS_MAX = 20
INCONNUS_MAX = 3  # rescue copies of an unknown active file kept (todo #336)
CONFIRM_MAX = timedelta(hours=12)  # todo #330: a switch still unconfirmed after this is flagged
EN_ATTENTE = "quota lisible après la prochaine session Claude sur ce compte"

# Network calls, looked up at call time: the tests replace them (tests/conftest.py blocks the real ones).
fetch_usage = collect.fetch


class CompteError(InvalidRequestError):
    """Refused or impossible; the message is safe to show (never a token, never the file's content)."""


def fetch_profile(token: str) -> dict[str, Any]:
    request = urllib.request.Request(PROFILE_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": collect.BETA_HEADER,
        "Content-Type": "application/json", "User-Agent": f"claude-code/{collect.claude_version()}",
    })
    try:
        with urllib.request.urlopen(request, timeout=collect.HTTP_TIMEOUT_S) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise UsageError(f"profil : erreur HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError):
        raise UsageError("profil : réseau indisponible") from None
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise UsageError("profil illisible") from None
    if not isinstance(data, dict):
        raise UsageError("profil inattendu")
    return data


ADDRESS_RETRY = timedelta(hours=6)


def email_of(profile: dict[str, Any]) -> str | None:
    """The account's e-mail address in a profile answer (key `email_address` or `email`), None if absent or odd."""
    account = profile.get("account")
    if not isinstance(account, dict):
        return None
    for key in ("email_address", "email"):
        value = account.get(key)
        if isinstance(value, str) and "@" in value and len(value) <= 254 and not any(c.isspace() for c in value):
            return value
    return None


def quel(slot: str, adresses: dict[str, str | None] | None = None) -> str:
    """How a user-facing sentence names an account: its address, else « compte A » / « compte B » (todo #339)."""
    return (adresses or {}).get(slot) or f"compte {slot.upper()}"


def adresses_of(meta: dict[str, Any]) -> dict[str, str | None]:
    return {s: (meta["comptes"].get(s) or {}).get("adresse") for s in SLOTS}


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def slot_path(slot: str) -> Path:
    if SLOT_RE.fullmatch(slot or "") is None:
        raise CompteError("compte inconnu : a ou b")
    return DIR / f"{slot}.json"


def other(slot: str) -> str:
    return "b" if slot == "a" else "a"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


# -- files ---------------------------------------------------------------------------------------------------------------

def read_file(path: Path) -> bytes | None:
    """Bytes of a regular file of ours (never through a symlink), None if absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CompteError(f"{path.name} illisible ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise CompteError(f"{path.name} refusé : doit être un fichier à nous")
        data = fh.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise CompteError(f"{path.name} trop gros pour un fichier de connexion")
    return data


def write_atomic(path: Path, data: bytes) -> None:
    """Temporary file 0600 in the same folder, fsync, rename: a reader sees the old file or the new one, never half."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".compte-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def write_verified(path: Path, data: bytes) -> bool:
    """write_atomic, then read the file back: True only if its SHA-256 is the one of `data` (todo #263, revue)."""
    write_atomic(path, data)
    try:
        back = read_file(path)
    except CompteError:
        return False
    return back is not None and hashlib.sha256(back).digest() == hashlib.sha256(data).digest()


def ensure_dir() -> None:
    DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(DIR)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise CompteError("dossier des comptes refusé : doit être un dossier à nous")
    if st.st_mode & 0o077:
        os.chmod(DIR, 0o700)


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """One registration or switch at a time (two taps in a row)."""
    ensure_dir()
    fd = os.open(DIR / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load_meta() -> dict[str, Any]:
    meta: dict[str, Any] = {}
    with contextlib.suppress(CompteError, ValueError, UnicodeDecodeError):
        raw = read_file(DIR / META)
        meta = json.loads(raw) if raw else {}
    meta = meta if isinstance(meta, dict) else {}
    comptes = meta.get("comptes") if isinstance(meta.get("comptes"), dict) else {}
    return {"comptes": {s: comptes[s] for s in SLOTS if isinstance(comptes.get(s), dict)},
            "derniere_bascule": meta.get("derniere_bascule") if isinstance(meta.get("derniere_bascule"), dict) else None,
            "preuves": [p for p in meta.get("preuves") or [] if isinstance(p, str)][-PROOFS_MAX:],
            "identites": {k: v for k, v in (meta.get("identites") or {}).items() if isinstance(v, str)},
            # todo #534: the automatic switches (proof of each, read by the bilan du soir), AUTO_MAX kept
            "bascules_auto": [b for b in meta.get("bascules_auto") or [] if isinstance(b, dict)][-AUTO_MAX:]}


def save_meta(meta: dict[str, Any]) -> None:
    ensure_dir()
    meta = {k: v for k, v in meta.items() if not k.startswith("_")}  # transient keys (e.g. _adresses) are never saved
    write_atomic(DIR / META, (json.dumps(meta, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


def oauth_of(data: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise CompteError("fichier de connexion illisible (pas du JSON)") from None
    oauth = obj.get("claudeAiOauth") if isinstance(obj, dict) else None
    if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str) or not oauth["accessToken"]:
        raise CompteError("fichier de connexion sans jeton OAuth (Claude Code est-il connecté ?)")
    return oauth


# -- which account ---------------------------------------------------------------------------------------------------------

def account_id(data: bytes, meta: dict[str, Any], now: datetime) -> str | None:
    """Hash of the account's uuid behind this file (profile endpoint), remembered per file digest; None if unknown."""
    key = digest(data)
    if key in meta["identites"]:
        return meta["identites"][key]
    try:
        oauth = oauth_of(data)
        expires = collect.parse_time(oauth.get("expiresAt"))
        if expires is not None and expires <= now:
            return None  # never refreshed here
        profile = fetch_profile(oauth["accessToken"])
    except (CompteError, UsageError):
        return None
    uuid = (profile.get("account") or {}).get("uuid") if isinstance(profile.get("account"), dict) else None
    if not isinstance(uuid, str) or not uuid:
        return None
    ident = hashlib.sha256(f"ultron-hub-compte:{uuid}".encode()).hexdigest()[:16]
    if email_of(profile):
        meta.setdefault("_adresses", {})[ident] = email_of(profile)
    meta["identites"] = {**dict(list(meta["identites"].items())[-(IDS_MAX - 1):]), key: ident}
    return ident


def identify(data: bytes, meta: dict[str, Any], now: datetime) -> tuple[str | None, str]:
    """(slot or None, how): same bytes, same refresh token, or same account (profile)."""
    copies = {s: read_file(slot_path(s)) for s in SLOTS if s in meta["comptes"]}
    copies = {s: c for s, c in copies.items() if c is not None}
    for s, c in copies.items():
        if c == data:
            return s, "identique à la copie"
    try:
        refresh = oauth_of(data).get("refreshToken")
    except CompteError:
        return None, "illisible"
    for s, c in copies.items():
        with contextlib.suppress(CompteError):
            if isinstance(refresh, str) and refresh and oauth_of(c).get("refreshToken") == refresh:
                return s, "même connexion, jeton renouvelé"
    ident = account_id(data, meta, now) if copies else None
    for s in copies:
        if ident and meta["comptes"][s].get("compte") == ident:
            return s, "même compte, jeton renouvelé"
    return None, "inconnu"


# -- commands ----------------------------------------------------------------------------------------------------------------

def _quota(slot: str, creds: Path, cache_dir: Path, now: datetime) -> tuple[dict[str, Any] | None, str | None, bool]:
    """(quota, error, waiting): `waiting` = only the access token has expired, which Claude Code renews (todo #330)."""
    try:
        q = collect.usage(cache_dir / f"usage-compte-{slot}.json", now, credentials=creds, fetcher=fetch_usage)
    except collect.TokenExpired:
        return None, EN_ATTENTE, True
    except UsageError as exc:
        return None, str(exc), False
    return {"session": q.get("session"), "week": q.get("week"), "stale": bool(q.get("stale"))}, None, False


def etat(cache_dir: Path, now: datetime | None = None) -> dict[str, Any]:
    """Read only (except the remembered account ids): active account, quota of each registered one, alert."""
    now = now or datetime.now(timezone.utc)
    meta = load_meta()
    known_ids = dict(meta["identites"])
    data = read_file(ACTIVE)
    slot, how = identify(data, meta, now) if data is not None else (None, "absent")
    if meta["identites"] != known_ids:
        with locked():
            fresh = load_meta()
            fresh["identites"] = {**fresh["identites"], **meta["identites"]}
            save_meta(fresh)
    _remplir_adresses(meta, now)
    adresses = adresses_of(meta)
    rows = []
    for s in SLOTS:
        info = meta["comptes"].get(s)
        registered = info is not None and slot_path(s).is_file()
        row: dict[str, Any] = {"nom": s, "enregistre": registered, "actif": s == slot, "adresse": adresses[s],
                               "enregistre_a": (info or {}).get("enregistre_a"), "quota": None, "quota_erreur": None,
                               "quota_attente": False}
        if registered:
            row["quota"], row["quota_erreur"], row["quota_attente"] = _quota(s, ACTIVE if s == slot else slot_path(s),
                                                                             cache_dir, now)
        rows.append(row)
    any_registered = any(r["enregistre"] for r in rows)
    last = _confirmer(meta["derniere_bascule"], slot, rows)
    alerte = None
    if data is None:
        alerte = "aucun fichier de connexion actif : Claude Code n'est pas connecté (/login)"
    elif slot is None and any_registered:
        alerte = "fichier actif inconnu (ni " + quel("a", adresses) + " ni " + quel("b", adresses) + ")"
        if last and last.get("vers") in SLOTS:
            v = last["vers"]
            alerte += (f" : il a changé depuis la bascule vers {quel(v, adresses)} (jeton renouvelé sans compte reconnu, ou "
                       f"réécrit par une session ouverte avant la bascule). Relance les sessions (`ultron relance`), vérifie, "
                       f"puis `compte enregistrer {v} --remplacer` si c'est bien {quel(v, adresses)}.")
        else:
            alerte += " : enregistre-le (`compte enregistrer a` ou `b`)."
    elif slot is not None and last and last.get("de") == slot and last.get("vers") != slot:
        alerte = (f"revenu à l'ancien compte {quel(slot, adresses)} après la bascule vers {quel(str(last.get('vers')), adresses)} : une session "
                  "Claude ouverte avant la bascule a renouvelé son jeton et réécrit le fichier. Relance les sessions "
                  "(`ultron relance`) puis rebascule.")
    dest = other(slot) if slot and any(r["nom"] == other(slot) and r["enregistre"] for r in rows) else None
    bascule = {k: last.get(k) for k in ("de", "vers", "a", "verifie")} if last else None
    out = {"actif": slot, "comment": how, "alerte": alerte, "autre": dest, "comptes": rows, "derniere_bascule": bascule,
           "a_reconnecter": a_reconnecter(rows, last, now),
           "actif_adresse": adresses.get(slot) if slot else None, "autre_adresse": adresses.get(dest) if dest else None}
    return {**out, "text": texte(out)}


def _confirmer(last: dict[str, Any] | None, slot: str | None, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Todo #330: a switch left « to confirm » (`verifie` None, expired token) is confirmed once its destination is the
    active account and its quota reads. Saved under the lock, only if comptes.json still holds that same switch."""
    if not (last and "verifie" in last and last["verifie"] is None and slot and last.get("vers") == slot):
        return last
    if not any(r["nom"] == slot and r["quota"] is not None for r in rows):
        return last
    with locked():
        fresh = load_meta()
        cur = fresh["derniere_bascule"]
        if cur and cur.get("a") == last.get("a") and cur.get("empreinte") == last.get("empreinte") and cur.get("verifie") is None:
            cur["verifie"] = True
            save_meta(fresh)
    return {**last, "verifie": True}


def _remplir_adresses(meta: dict[str, Any], now: datetime) -> None:
    """Todo #339: a registered account without a remembered address gets one profile call (copy's token not expired, never
    refreshed); any failure means no address, never an error. Fills `meta` and saves it under the lock."""
    found: dict[str, str] = {}
    failed: list[str] = []
    for s in SLOTS:
        info = meta["comptes"].get(s)
        if info is None or info.get("adresse"):
            continue
        tried = collect.parse_time(info.get("adresse_essai"))
        if tried is not None and now - tried < ADDRESS_RETRY:
            continue  # a failed try is not repeated at every `etat` (the dashboard reads every minute)
        try:
            data = read_file(slot_path(s))
            if data is None:
                continue
            oauth = oauth_of(data)
            expires = collect.parse_time(oauth.get("expiresAt"))
            if expires is not None and expires <= now:
                continue
            profile = fetch_profile(oauth["accessToken"])
        except (CompteError, UsageError):
            failed.append(s)
            continue
        email, account = email_of(profile), profile.get("account")
        uuid = account.get("uuid") if isinstance(account, dict) else None
        if email and info.get("compte") and isinstance(uuid, str):
            if hashlib.sha256(f"ultron-hub-compte:{uuid}".encode()).hexdigest()[:16] != info["compte"]:
                continue  # the copy is another account than the one recorded: no wrong address
        if email:
            found[s] = email
            info["adresse"] = email
        else:
            failed.append(s)
    if found or failed:
        with locked():
            fresh = load_meta()
            for s, email in found.items():
                if s in fresh["comptes"] and not fresh["comptes"][s].get("adresse"):
                    fresh["comptes"][s]["adresse"] = email
            for s in failed:
                if s in fresh["comptes"] and not fresh["comptes"][s].get("adresse"):
                    fresh["comptes"][s]["adresse_essai"] = now_iso(now)
            save_meta(fresh)


def a_reconnecter(rows: list[dict[str, Any]], last: dict[str, Any] | None,
                  now: datetime | None = None) -> list[dict[str, str]]:
    """Todo #336 (a failure = a check): the accounts to log into again from the dashboard (🔑), with why.
    A registered account whose quota cannot be read (refused or unreadable token; an expired access token is not a failure,
    todo #330), or the destination of the last switch, unless registered again since, when its check failed (`verifie`
    False) or when it is still to confirm (`verifie` None) CONFIRM_MAX after the switch. A switch saved without the key
    (before #336) is unknown, never flagged."""
    now = now or datetime.now(timezone.utc)
    out: list[dict[str, str]] = []
    for r in rows:
        if r["enregistre"] and r["quota"] is None and not r.get("quota_attente"):
            out.append({"nom": r["nom"], "adresse": r.get("adresse"), "raison": f"quota illisible ({r['quota_erreur']})"})
    raison = None
    if last and last.get("verifie") is False:
        raison = "la dernière bascule n'a pas pu être vérifiée"
    elif last and "verifie" in last and last["verifie"] is None:
        at = collect.parse_time(last.get("a"))
        if at is not None and now - at > CONFIRM_MAX:
            raison = "bascule jamais confirmée"
    if raison and last.get("vers") in SLOTS and all(x["nom"] != last["vers"] for x in out):
        row = next(r for r in rows if r["nom"] == last["vers"])
        if not (row["enregistre_a"] and str(row["enregistre_a"]) > str(last.get("a") or "")):
            out.append({"nom": last["vers"], "adresse": row.get("adresse"), "raison": raison})
    return sorted(out, key=lambda x: x["nom"])


def _pct(window: Any) -> str:
    return f"{window.get('percent')} %" if isinstance(window, dict) else "?"


def texte(d: dict[str, Any]) -> str:
    lines = ["👥 Comptes Claude", ""]
    names = {r["nom"]: r.get("adresse") for r in d["comptes"]}
    lines.append(f"Actif : {quel(d['actif'], names)} ({d['comment']})" if d["actif"] else f"Actif : inconnu ({d['comment']})")
    for r in d["comptes"]:
        head = f"• {r.get('adresse') or r['nom'].upper()}" + (" (actif)" if r["actif"] else "")
        if not r["enregistre"]:
            lines.append(f"{head} : pas enregistré")
        elif r["quota"]:
            lines.append(f"{head} : session {_pct(r['quota']['session'])}, semaine {_pct(r['quota']['week'])}")
        elif r.get("quota_attente"):
            lines.append(f"{head} : {r['quota_erreur']}")
        else:
            lines.append(f"{head} : quota illisible ({r['quota_erreur']})")
    for x in d.get("a_reconnecter") or []:
        lines.append(f"🔑 {x.get('adresse') or x['nom'].upper()} à reconnecter ({x['raison']}) : `ultron-hub compte connexion demarrer --compte {x['nom']}`"
                     " ou bouton 🔑 du dashboard")
    if d["alerte"]:
        lines += ["", f"⚠️ {d['alerte']}"]
    return "\n".join(lines)


def enregistrer(slot: str, cache_dir: Path, *, remplacer: bool = False, now: datetime | None = None,
                identite: tuple[str, str | None] | tuple[str, str | None, str | None] | None = None) -> dict[str, Any]:
    """Copy the active credentials into slot `slot` (0600, atomic). Refused if they are already the other slot's account.

    `identite` = (digest of a file, account id found for it, None = profile unreachable): when it is still the active file,
    the profile is not asked again (todo #336, `connexion.adopter` has just asked it)."""
    now = now or datetime.now(timezone.utc)
    path = slot_path(slot)
    with locked():
        data = read_file(ACTIVE)
        if data is None:
            raise CompteError("aucun fichier de connexion actif : connecte Claude Code (/login) puis recommence")
        oauth_of(data)
        meta = load_meta()
        if identite is not None and identite[0] == digest(data):
            meta["identites"][identite[0]] = identite[1]  # a None is read back by account_id, never saved (below)
        cur, how = identify(data, meta, now)
        if cur is not None and cur != slot:
            raise CompteError(f"ce compte est déjà enregistré comme {cur.upper()} ({how}) : rien n'a changé")
        ident = account_id(data, meta, now)
        old = meta["comptes"].get(slot) or {}
        adresse = meta.get("_adresses", {}).get(ident) if ident else None
        if identite is not None and len(identite) > 2 and identite[0] == digest(data):
            adresse = adresse or identite[2]
        if not adresse and ident and old.get("compte") == ident:
            adresse = old.get("adresse")  # same account as before: keep what is known
        occupied = slot in meta["comptes"] or read_file(path) is not None
        # The active file is none of the known ones and the slot is taken: unless the profile proves it is the slot's own
        # account, overwriting could lose the only copy of an account (profile unreachable, or no id recorded) → explicit.
        if cur is None and occupied and not remplacer and not (ident and old.get("compte") == ident):
            why = ("contient déjà un autre compte" if ident and old.get("compte")
                   else "est déjà pris et le compte actif n'a pas pu être reconnu (profil injoignable ou jeton expiré)")
            raise CompteError(f"{slot.upper()} {why} : `ultron-hub compte enregistrer {slot} --remplacer` pour l'écraser "
                              "(rien n'a changé)")
        if not write_verified(path, data):
            raise CompteError(f"copie {slot.upper()} mal écrite (empreinte différente) : réessaie ; l'ancienne copie est perdue "
                              "si elle existait, le fichier actif n'a pas changé")
        meta["comptes"][slot] = {"enregistre_a": now_iso(now), "compte": ident or (old.get("compte") if cur == slot else None)}
        if adresse:
            meta["comptes"][slot]["adresse"] = adresse
        elif cur == slot and not ident and old.get("adresse"):
            meta["comptes"][slot]["adresse"] = old["adresse"]
        names = adresses_of(meta)
        meta["identites"] = {k: v for k, v in meta["identites"].items() if isinstance(v, str)}
        save_meta(meta)
    with contextlib.suppress(OSError):
        (cache_dir / f"usage-compte-{slot}.json").unlink()
    return {"nom": slot, "deja": cur == slot, "compte_reconnu": bool(meta["comptes"][slot]["compte"]),
            "adresse": meta["comptes"][slot].get("adresse"),
            "text": (f"💾 Compte actif enregistré comme {quel(slot, names)}." if cur != slot
                     else f"💾 Copie {quel(slot, names)} mise à jour (même compte).")}


def basculer(cache_dir: Path, *, vers: str | None, authorize, now: datetime | None = None) -> dict[str, Any]:
    """Save the active file back into its slot, put the other slot's copy in its place, then read the usage to check.

    `authorize(target, used)` is closing.authorize bound to the CLI's flags (target = destination slot)."""
    now = now or datetime.now(timezone.utc)
    if vers is not None and SLOT_RE.fullmatch(vers) is None:
        raise CompteError("compte inconnu : a ou b")
    with locked():
        meta = load_meta()
        data = read_file(ACTIVE)
        if data is None:
            raise CompteError("aucun fichier de connexion actif : rien n'a changé")
        cur, how = identify(data, meta, now)
        if cur is None:
            raise CompteError("fichier actif inconnu (ni A ni B) : enregistre-le d'abord (`compte enregistrer a` ou `b`) ; "
                              "rien n'a changé")
        dest = other(cur)
        if vers is not None and vers != dest:
            raise CompteError(f"le compte actif est déjà {vers.upper()} : rien n'a changé")
        target = read_file(slot_path(dest)) if dest in meta["comptes"] else None
        if target is None:
            raise CompteError(f"compte {dest.upper()} pas enregistré : connecte-le (/login) puis `compte enregistrer {dest}` ; "
                              "rien n'a changé")
        oauth_of(target)
        ids = meta["comptes"][cur].get("compte"), meta["comptes"][dest].get("compte")
        if ids[0] and ids[0] == ids[1]:
            raise CompteError("A et B sont le même compte : enregistre l'autre compte d'abord ; rien n'a changé")
        by = authorize(dest, lambda proof: proof in meta["preuves"])
        if by.get("proof"):  # spent at once, whatever comes next
            meta["preuves"] = (meta["preuves"] + [by["proof"]])[-PROOFS_MAX:]
            save_meta(meta)
        if read_file(slot_path(cur)) != data:  # renewed since it was saved: keep the newest tokens
            if not write_verified(slot_path(cur), data):
                save_meta(meta)
                raise CompteError(f"copie {cur.upper()} mal écrite (empreinte différente) : rien n'a basculé, réessaie")
            meta["comptes"][cur]["renouvele_a"] = now_iso(now)
        if read_file(ACTIVE) != data:
            save_meta(meta)
            raise CompteError("le fichier actif vient de changer (une session Claude renouvelle son jeton) : rien n'a basculé, "
                              "réessaie")
        if not write_verified(ACTIVE, target):  # never call it switched on a file we did not read back
            restored = write_verified(ACTIVE, data)
            save_meta(meta)
            raise CompteError(f"fichier actif mal écrit (empreinte différente de la copie {dest.upper()}) : "
                              + (f"{cur.upper()} remis en place, rien n'a basculé" if restored else
                                 f"et la remise de {cur.upper()} a échoué aussi : `compte etat`, puis /login si besoin"))
        meta["derniere_bascule"] = {"de": cur, "vers": dest, "a": now_iso(now), "empreinte": digest(target),
                                    "by": by.get("by")}
        save_meta(meta)
    for name in ("usage.json", f"usage-compte-{dest}.json"):  # /usage must not show the old account's cached quota
        with contextlib.suppress(OSError):
            (cache_dir / name).unlink()
    verification = verifier(now)
    with locked():  # todo #336: a failed check is shown as « to log into again » by `etat`; None = to confirm (todo #330)
        meta = load_meta()
        if meta["derniere_bascule"] and meta["derniere_bascule"].get("empreinte") == digest(target):
            meta["derniere_bascule"]["verifie"] = verification["ok"]
            save_meta(meta)
    names = adresses_of(meta)
    if verification["ok"]:
        check = f"Quota {quel(dest, names)} : session {_pct(verification['session'])}, semaine {_pct(verification['week'])}.\n"
    elif verification["ok"] is None:
        check = "Quota lisible dès la première session Claude sur ce compte (jeton à renouveler par Claude Code).\n"
    else:
        check = f"Vérification impossible : {verification['erreur']}.\n"
    text = (f"🔁 Bascule {quel(cur, names)} → {quel(dest, names)} faite.\n" + check
            + f"Les sessions Claude déjà ouvertes restent sur {quel(cur, names)} (et peuvent réécrire le fichier en renouvelant leur "
              "jeton) : relance-les avec `ultron relance`, puis regarde `compte etat`.")
    return {"de": cur, "vers": dest, "de_adresse": names.get(cur), "vers_adresse": names.get(dest), "comment": how, "by": by, "verification": verification, "text": text}


def sauver_actif(now: datetime | None = None) -> str | None:
    """Todo #336, before a new login replaces the active file: if it is a known account whose tokens were renewed since its
    copy, copy it back (as `basculer` does), so the login never loses the newest tokens of the account in use.
    An active file that is neither A nor B gets a rescue copy `inconnu-<time>.json` (`garder_inconnu`).
    Returns the slot saved or the rescue copy's name, None when nothing needed saving (absent file, copy up to date)."""
    now = now or datetime.now(timezone.utc)
    with locked():
        meta = load_meta()
        data = read_file(ACTIVE)
        if data is None:
            return None
        cur, _ = identify(data, meta, now)
        if cur is None:  # neither A nor B: a dated rescue copy, so the login never loses an account nobody registered
            return garder_inconnu(data, now)
        if read_file(slot_path(cur)) == data:
            return None
        if not write_verified(slot_path(cur), data):
            raise CompteError(f"copie {cur.upper()} mal écrite (empreinte différente) : rien n'a été lancé, réessaie")
        meta["comptes"][cur]["renouvele_a"] = now_iso(now)
        save_meta(meta)
    return cur


def garder_inconnu(data: bytes, now: datetime) -> str | None:
    """Rescue copy 0600 `inconnu-<UTC time>.json` of an active file that is neither A nor B (the newest INCONNUS_MAX kept);
    its name, None when the newest rescue copy already holds these bytes. Called under `locked()`."""
    ensure_dir()
    olds = sorted(DIR.glob("inconnu-*.json"))
    with contextlib.suppress(CompteError):  # a newest copy that is not a plain file of ours: a new one is written
        if olds and read_file(olds[-1]) == data:
            return None
    name = f"inconnu-{now.astimezone(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    if not write_verified(DIR / name, data):
        raise CompteError("copie de secours du fichier actif inconnu mal écrite : rien n'a été lancé, réessaie")
    for extra in sorted(DIR.glob("inconnu-*.json"))[:-INCONNUS_MAX]:
        with contextlib.suppress(OSError):
            extra.unlink()
    return name


def garder_avant_connexion(slot: str) -> str | None:
    """Todo #336: before a login overwrites the copy of `slot` without proof it is the same account, keep it as
    `<slot>.avant-connexion.json` (0600, atomic, replaces the previous one). Its name, None when there was no copy."""
    with locked():
        old = read_file(slot_path(slot))
        if old is None:
            return None
        name = f"{slot}.avant-connexion.json"
        if not write_verified(DIR / name, old):
            raise CompteError(f"sauvegarde de la copie {slot.upper()} mal écrite : rien n'a été enregistré, réessaie")
    return name


def verifier(now: datetime) -> dict[str, Any]:
    """Usage read with the token now in the active file (never refreshed: an expired one is renewed by the next session).
    `ok`: True read, False failed, None to confirm (expired access token, the usual case for an hours-old copy, todo #330)."""
    token = ""
    try:
        token = collect.read_token(ACTIVE, now)
        q = collect.parse(fetch_usage(token))
    except collect.TokenExpired as exc:
        return {"ok": None, "erreur": str(exc), "session": None, "week": None}
    except UsageError as exc:
        message = str(exc).replace(token, "***") if token else str(exc)
        return {"ok": False, "erreur": message, "session": None, "week": None}
    return {"ok": True, "erreur": None, "session": q.get("session"), "week": q.get("week")}
