"""Claude Code hooks of an autonomous squad session (todo #96).

Standard library only, run as a script (`python -I hooks.py <stop|post|pre> <squad dir>`): the hook receives the
event JSON on stdin and prints the decision JSON on stdout. Any internal error means "no decision" (exit 0, no
output): a broken hook must never block the session, the sandbox and the permission rules are the other nets.

Squad dir files: etat.json, drapeau-passation, drapeau-stop, stop-compteur.json, contexte.json, contexte-etat.json.
"""

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Anti-loop: after a block the model may answer the same thing again and again (stop_hook_active is true each time).
# The Stop hook lets the session end after MAX_SAME_BLOCKS blocks in a row with an identical last message, and
# whatever the messages, after MAX_TOTAL_BLOCKS blocks in a row (CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=0 removes
# Claude Code's own cap of 8, so this one is the only cap).
MAX_SAME_BLOCKS = 3
MAX_TOTAL_BLOCKS = 200

REASON_CONTINUE = "Continue : piste suivante de la section 4 ; vérifie le quota (section 2)."
REASON_RESUME = ("Reprendre : la question {qid} est encore ouverte ; si tu n'as plus rien d'autre à faire, "
                 'appelle demander(reprendre: "{qid}") pour attendre la réponse de l\'utilisateur.')
REASON_HANDOVER = "Fais la passation maintenant (section 7)."
CONTEXT_HANDOVER = "PASSATION MAINTENANT : arrête la piste en cours et fais la passation (section 7)."


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def open_question(squad: Path, etat: dict) -> str | None:
    """Id of a question of this squad still open (blocking or not), read from the box named in mcp.json (todo #118)."""
    try:
        args = _read_json(squad / "mcp.json")["mcpServers"]["ultron"]["args"]
        folder = Path(args[args.index("--boite") + 1])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    squad_id = str(etat.get("id", squad.name))
    for path in sorted((folder / "questions").glob("Q*.json")):
        q = _read_json(path)
        if q.get("statut") == "ouverte" and q.get("escouade") == squad_id:
            return str(q.get("id") or path.stem)
    return None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _touched(path: Path) -> float:
    """Last change of a file: a copy that keeps the original date (cp -p, cp -a) still has a new ctime."""
    st = path.stat()
    return max(st.st_mtime, st.st_ctime)


def handover_done(squad: Path, etat: dict) -> bool:
    """Subject file rewritten AND an archive created in archives/<subject>/, both after the start."""
    start = etat.get("session_debut_ts") or etat.get("debut_ts")  # chained sessions: start of the current one
    prompts, subject = etat.get("prompts_dir"), etat.get("sujet")
    if not (isinstance(start, (int, float)) and prompts and subject):
        return False
    folder = Path(prompts)
    try:
        if _touched(folder / f"{subject}.md") <= start:
            return False
        archives = folder / "archives" / subject
        return any(p.is_file() and _touched(p) > start for p in archives.iterdir())
    except OSError:
        return False


def stop_decision(squad: Path, event: dict) -> dict:
    etat = _read_json(squad / "etat.json")
    counter_path = squad / "stop-compteur.json"
    if handover_done(squad, etat):
        etat.update(statut="passation_ok", fin=time.strftime("%Y-%m-%dT%H:%M:%S"))
        _write_json(squad / "etat.json", etat)
        counter_path.unlink(missing_ok=True)
        return {}
    counter = _read_json(counter_path)
    digest = hashlib.sha256(str(event.get("last_assistant_message", "")).encode()).hexdigest()[:16]
    if event.get("stop_hook_active"):
        same = counter.get("same", 0) + 1 if counter.get("last") == digest else 0
        total = counter.get("total", 0) + 1
    else:  # a fresh stop after a real turn: the chain restarts
        same, total = 0, 1
    counter = {"last": digest, "same": same, "total": total}
    if same >= MAX_SAME_BLOCKS or total > MAX_TOTAL_BLOCKS:
        etat.update(statut="boucle_stoppee", fin=time.strftime("%Y-%m-%dT%H:%M:%S"))
        _write_json(squad / "etat.json", etat)
        return {}
    _write_json(counter_path, counter)
    reason = REASON_HANDOVER if (squad / "drapeau-passation").exists() else REASON_CONTINUE
    if reason == REASON_CONTINUE and (qid := open_question(squad, etat)):
        reason = REASON_RESUME.format(qid=qid)  # never let the session stop while the user has not answered
    return {"decision": "block", "reason": reason}


def user_mail(squad: Path, event: dict) -> str:
    """Remarks and `signaler` answers for the orchestrator (todo #117): never for a sub-agent (`agent_id`).
    The box code is the frozen copy of mcp_ultron.py named in mcp.json (written by the engine, not by the session)."""
    if event.get("agent_id"):
        return ""
    etat = _read_json(squad / "etat.json")
    try:
        args = _read_json(squad / "mcp.json")["mcpServers"]["ultron"]["args"]
        mcp_py, boite = Path(args[args.index("-S") + 1]), Path(args[args.index("--boite") + 1])
        cle = Path(args[args.index("--cle") + 1]) if "--cle" in args else None
    except (KeyError, TypeError, ValueError, IndexError):
        return ""
    spec = importlib.util.spec_from_file_location("mcp_ultron_frozen", mcp_py)
    mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp)
    jeton = str(etat.get("jeton") or "")
    if not mcp.frame(jeton, ["."]):
        return ""  # no session token: keep the mail for a session that can frame it
    key = mcp.load_key(cle or mcp.KEY_PATH)
    return mcp.frame(jeton, mcp.Box(boite).take_mail(key, str(etat.get("id", squad.name)), "hook"))


# -- Context gauge (todo #175) -------------------------------------------------------------------------------
# Shared by the interactive hook (`ultron-hub esprit contexte --claude-hook`, which imports this file) and the squad
# PostToolUse hook, hence standard library only. Config: config/contexte.yaml (squads: copy `contexte.json` written
# by the engine in the squad folder). A sub-agent (`agent_id` in the hook JSON) has no transcript of its own in a
# PostToolUse event (`transcript_path` is the main one): nothing is measured for it.

CONTEXT_DEFAULTS = {"fenetres": {"defaut": 200000}, "fenetre_etendue": 1000000, "seuil_info": 50,
                    "seuil_passation": 60}
CONTEXT_TAIL_BYTES = 2_000_000
CONTEXT_INFO = "Contexte à {n} % : prépare ta passation (Reprise, journal)."
CONTEXT_STRONG = "CONTEXTE {n} % : PASSATION OBLIGATOIRE MAINTENANT, rien d'autre."
CONTEXT_TELEGRAM = "Escouade {id} : contexte à {n} %, passation à préparer."
CONTEXT_FLAG = "contexte"


def context_config(raw: dict | None) -> dict:
    """Config with defaults; a wrong type falls back to the default."""
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CONTEXT_DEFAULTS.items()}
    for key, value in (raw or {}).items():
        if key == "fenetres" and isinstance(value, dict):
            cfg["fenetres"].update({str(k): v for k, v in value.items() if isinstance(v, (int, float)) and v > 0})
        elif key in ("fenetre_etendue", "seuil_info", "seuil_passation") and isinstance(value, (int, float)) \
                and not isinstance(value, bool) and value > 0:
            cfg[key] = value
    return cfg


def last_usage(transcript: str) -> tuple[str, int] | None:
    """(model, input + cache read + cache creation tokens) of the last main-thread assistant answer, or None."""
    try:
        path = Path(transcript)
        with open(path, "rb") as fh:
            size = path.stat().st_size
            fh.seek(max(0, size - CONTEXT_TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, ValueError, TypeError):
        return None
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        message = obj.get("message") if isinstance(obj, dict) else None
        if not isinstance(message, dict) or obj.get("type") != "assistant" or obj.get("isSidechain"):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict) or message.get("model") == "<synthetic>":
            continue
        total = 0
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = usage.get(key) or 0
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            total += int(value)
        return (str(message.get("model") or ""), total) if total > 0 else None
    return None


def context_percent(model: str, tokens: int, cfg: dict) -> float:
    """Share of the model's window. A window is looked up by name part (« opus » in « claude-opus-4-5 »); more tokens
    than that window means the extended one (the transcript does not say if the session runs with the 1M window)."""
    windows = cfg["fenetres"]
    window = windows.get("defaut", 200000)
    for name, size in windows.items():
        if name != "defaut" and name.lower() in model.lower():
            window = size
            break
    if tokens > window:
        window = max(window, cfg["fenetre_etendue"])
    return 100.0 * tokens / window


def send_telegram(text: str) -> bool:
    """One text line on Telegram through the common door `ultron-hub send` (todo #194; this file is stdlib-only and
    frozen, so it cannot import `ultron_hub.envoi`: the CLI chooses `hermes send` or the direct Bot API)."""
    for binary in (shutil.which("ultron-hub"), str(Path.home() / ".local" / "bin" / "ultron-hub")):
        if not binary:
            continue
        try:
            return subprocess.run([binary, "send", "-t", "telegram", text], capture_output=True, text=True,
                                  timeout=60, check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def context_check(event: dict, cfg: dict, state_file: Path, squad: Path | None = None) -> str:
    """Text for `additionalContext` ("" = nothing). 50 %: once per tier (state_file); 60 %: at every call. For a squad,
    50 % also sends a Telegram line and 60 % raises `drapeau-passation` (reason « contexte »)."""
    if event.get("agent_id"):
        return ""
    usage = last_usage(str(event.get("transcript_path") or ""))
    if usage is None:
        return ""
    pct = context_percent(usage[0], usage[1], cfg)
    n = int(pct)
    state = _read_json(state_file)
    tier = state.get("palier", 0) if isinstance(state.get("palier"), int) else 0
    low, high = cfg["seuil_info"], cfg["seuil_passation"]
    text = ""
    if pct >= high:
        text = CONTEXT_STRONG.format(n=n)
        if squad is not None:
            flag = squad / "drapeau-passation"
            if not flag.exists():
                flag.write_text(CONTEXT_FLAG + "\n", encoding="utf-8")
    elif pct >= low and tier < low:
        text = CONTEXT_INFO.format(n=n)
    new_tier = 0 if pct < low else max(tier, int(low))
    if pct >= low and tier < low and squad is not None:
        send_telegram(CONTEXT_TELEGRAM.format(id=_read_json(squad / "etat.json").get("id", squad.name), n=n))
    if new_tier != tier or not state_file.exists():
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            _write_json(state_file, {"palier": new_tier})
        except OSError:
            pass
    return text


def post_decision(squad: Path, event: dict) -> dict:
    parts = []
    try:
        parts.append(user_mail(squad, event))
    except Exception:  # noqa: BLE001 - a broken box never blocks the session
        pass
    try:
        gauge = context_check(event, context_config(_read_json(squad / "contexte.json")), squad / "contexte-etat.json", squad)
    except Exception:  # noqa: BLE001 - a broken gauge never blocks the session
        gauge = ""
    parts.append(gauge)
    if (squad / "drapeau-passation").exists() and not gauge.startswith("CONTEXTE"):
        parts.append(CONTEXT_HANDOVER)
    context = "\n".join(p for p in parts if p)
    if context:
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}
    return {}


# -- PreToolUse (Bash) ---------------------------------------------------------------------------------------

_WRAPPERS = {"env", "command", "nohup", "time", "exec", "builtin", "nice", "setsid", "stdbuf", "timeout"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_GIT_ON_MAIN = {"commit", "rebase", "reset", "cherry-pick", "revert", "am"}
_GIT_ARG_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_PROTECTED = {"main", "master"}


def split_commands(command: str) -> list[list[str]]:
    """Simple commands of a shell line: `&&`, `||`, `;`, `|`, `&`, newlines, `$( )`, backticks and parentheses
    all separate commands. Quoted text stays inside one word."""
    lexer = shlex.shlex(command.replace("`", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and all(c in "();<>|&" for c in token):
            if token[0] in "<>":  # redirections do not end a command
                continue
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _strip(words: list[str]) -> list[str]:
    """Drop env assignments and transparent wrappers in front of the real command."""
    i = 0
    while i < len(words):
        w = words[i]
        if "=" in w and not w.startswith("-") and w.split("=", 1)[0].replace("_", "").isalnum():
            i += 1
        elif Path(w).name in _WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or (Path(w).name == "timeout" and words[i][:1].isdigit())
                                      or ("=" in words[i] and Path(w).name == "env")):
                i += 1
        else:
            break
    return words[i:]


def current_branch(directory: str | None) -> str | None:
    try:
        out = subprocess.run(["git", "-C", directory or ".", "symbolic-ref", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _git_check(words: list[str], cwd: str | None, branch_of=current_branch) -> str | None:
    i, directory = 1, cwd
    while i < len(words) and words[i].startswith("-"):
        opt = words[i]
        if opt == "-C" and i + 1 < len(words):
            directory = str(Path(directory or ".", words[i + 1]))
        i += 2 if opt in _GIT_ARG_OPTS else 1
    sub = words[i] if i < len(words) else ""
    if sub == "push":
        return "git push est interdit dans une escouade"
    if sub == "merge":
        return "git merge est interdit dans une escouade (la fusion passe par le bouton de l'utilisateur)"
    if sub in _GIT_ON_MAIN and branch_of(directory) in _PROTECTED:
        return f"git {sub} est interdit sur main/master : travaille sur une branche todo-<id>/…"
    return None


def _positionals(words: list[str]) -> list[str]:
    return [w for w in words if not w.startswith("-")]


def check_command(command: str, cwd: str | None = None, branch_of=current_branch, _depth: int = 0) -> str | None:
    """Return the refusal reason of a Bash command line, None when it may run."""
    if _depth > 4:
        return "commande trop imbriquée"
    try:
        segments = split_commands(command)
    except ValueError:
        return "commande illisible (guillemets non fermés)"
    for raw in segments:
        words = _strip(raw)
        if not words:
            continue
        name = Path(words[0]).name
        if name in ("sudo", "doas", "su"):
            return "sudo est interdit : propose la commande à l'utilisateur"
        if name in _SHELLS and "-c" in words[1:]:
            j = words.index("-c")
            if j + 1 < len(words):
                reason = check_command(words[j + 1], cwd, branch_of, _depth + 1)
                if reason:
                    return reason
        if name == "xargs" or name == "eval":
            reason = check_command(" ".join(words[1:]), cwd, branch_of, _depth + 1)
            if reason:
                return reason
        if name == "cd" and len(words) > 1:
            cwd = str(Path(cwd or ".", words[1]).expanduser())
        elif name == "git":
            reason = _git_check(words, cwd, branch_of)
            if reason:
                return reason
        elif name == "ultron-hub":
            pos = _positionals(words[1:])
            first, second = (pos + ["", ""])[:2]
            if first == "todo" and second in ("done", "cancel"):
                return "ultron-hub todo done|cancel est interdit : la clôture se fait par le bouton de l'utilisateur"
            if first == "branch" and second == "merge":
                return "ultron-hub branch merge est interdit : la fusion se fait par le bouton de l'utilisateur"
            if first == "mission":
                return "ultron-hub mission est interdit dans une escouade"
            if first == "escouade" and second == "run":
                return "pas d'escouade dans l'escouade (ultron-hub escouade run interdit)"
            if first == "escouade" and second in ("repondre", "remarque", "question-envoyee"):
                return "seul l'utilisateur répond (ultron-hub escouade repondre|remarque|question-envoyee interdit)"
        elif name == "systemctl":
            pos = _positionals(words[1:])
            if "--user" not in words or not pos or pos[0] not in ("status", "is-active"):
                return "systemctl est interdit (seulement `systemctl --user status|is-active`)"
        elif name == "systemd-run":
            return "systemd-run est interdit dans une escouade"
    return None


def pre_decision(squad: Path, event: dict, branch_of=current_branch) -> dict:
    if event.get("tool_name") != "Bash":
        return {}
    command = (event.get("tool_input") or {}).get("command", "")
    reason = check_command(command, event.get("cwd"), branch_of)
    if not reason:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


HANDLERS = {"stop": stop_decision, "post": post_decision, "pre": pre_decision}


def main(argv: list[str]) -> int:
    try:
        kind, squad = argv[1], Path(argv[2])
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        decision = HANDLERS[kind](squad, event)
        if decision:
            sys.stdout.write(json.dumps(decision, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - never block the session because of a hook bug
        sys.stderr.write(f"escouade hook: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
