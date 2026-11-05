"""Settings loaded from config/settings.yaml; all paths are absolute."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ultron_hub.common.errors import ConfigError

# src/ultron_hub/common/settings.py -> project root is 3 levels above the package dir.
DEFAULT_ROOT = Path(__file__).resolve().parents[3]


def project_root() -> Path:
    override = os.environ.get("ULTRON_HUB_ROOT")
    return Path(override).resolve() if override else DEFAULT_ROOT


@dataclass(frozen=True)
class Settings:
    root: Path
    tasks_dir: Path
    var_dir: Path
    runs_dir: Path
    logs_dir: Path
    default_model: str = "sonnet"
    default_timeout_s: float = 300
    claude_binary: str | None = None
    claude_retry_delay_s: float = 30.0
    disallowed_tools: tuple[str, ...] = field(default_factory=tuple)
    log_level: str = "INFO"
    log_file: str = "ultron-hub.log"
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 5
    todo_db: Path | None = None
    todo_timezone: str = "Europe/Paris"
    todo_categories: tuple[str, ...] = ("perso", "travail", "serveur", "apprentissage", "admin")
    todo_notify_target: str = "telegram"
    hermes_binary: str | None = None
    envoi_mode: str = "hermes"
    envoi_chat_id: str | None = None
    missions_dir: Path | None = None
    repos_dir: Path | None = None
    repo_tests: dict[str, tuple[str, ...]] = field(default_factory=dict)
    esprit_dir: Path | None = None
    savoir_dir: Path | None = None
    video_skill_dir: Path = Path.home() / ".hermes" / "skills" / "hermes-video"


ESPRIT_ENV = "ULTRON_ESPRIT_DIR"


def default_esprit_dir() -> Path:
    """Shared mind of Ultron (profile, charter, journal), outside the project: Hermes and Claude Code read it."""
    override = os.environ.get(ESPRIT_ENV)
    return Path(override) if override else Path.home() / "ultron" / "esprit"


SAVOIR_ENV = "ULTRON_SAVOIR_DIR"


def default_savoir_dir() -> Path:
    """Ultron's knowledge notebook (todo #63), next to the shared mind: saved by the nightly backup."""
    override = os.environ.get(SAVOIR_ENV)
    return Path(override) if override else Path.home() / "ultron" / "savoir"


def load_settings(root: Path | None = None) -> Settings:
    root = (root or project_root()).resolve()
    path = root / "config" / "settings.yaml"
    raw: dict = {}
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a mapping")

    paths = raw.get("paths") or {}
    claude = raw.get("claude") or {}
    log = raw.get("logging") or {}
    todo = raw.get("todo") or {}
    repos = raw.get("repos") or {}
    video = raw.get("video") or {}
    envoi = raw.get("envoi") or {}
    envoi_mode = str(envoi.get("mode") or "hermes").strip().lower()
    if envoi_mode not in ("hermes", "direct"):
        raise ConfigError("envoi.mode must be 'hermes' or 'direct'")

    def resolve(key: str, default: str) -> Path:
        p = Path(paths.get(key) or default)
        return p if p.is_absolute() else root / p

    binary = os.environ.get("ULTRON_HUB_CLAUDE_BIN") or claude.get("binary") or None
    disallowed = claude.get("disallowed_tools") or []
    if not isinstance(disallowed, list) or not all(isinstance(t, str) for t in disallowed):
        raise ConfigError("claude.disallowed_tools must be a list of strings")

    categories = todo.get("categories") or list(Settings.todo_categories)
    if not isinstance(categories, list) or not all(isinstance(c, str) and c for c in categories):
        raise ConfigError("todo.categories must be a list of non-empty strings")

    tests = repos.get("tests") or {}
    if not isinstance(tests, dict) or not all(
            isinstance(cmd, list) and cmd and all(isinstance(a, str) and a for a in cmd) for cmd in tests.values()):
        raise ConfigError("repos.tests must map a repository name to a non-empty list of strings")
    repos_dir = Path(repos["dir"]) if repos.get("dir") else root.parent
    if not repos_dir.is_absolute():
        raise ConfigError("repos.dir must be an absolute path")

    return Settings(
        root=root,
        tasks_dir=resolve("tasks_dir", "tasks"),
        var_dir=resolve("var_dir", "var"),
        runs_dir=resolve("runs_dir", "var/runs"),
        logs_dir=resolve("logs_dir", "var/logs"),
        default_model=str(raw.get("default_model") or "sonnet"),
        default_timeout_s=float(raw.get("default_timeout_s") or 300),
        claude_binary=binary,
        claude_retry_delay_s=float(claude.get("retry_delay_s") if claude.get("retry_delay_s") is not None else 30),
        disallowed_tools=tuple(disallowed),
        log_level=os.environ.get("ULTRON_HUB_LOG_LEVEL") or str(log.get("level") or "INFO"),
        log_file=str(log.get("file") or "ultron-hub.log"),
        log_max_bytes=int(log.get("max_bytes") or 5_000_000),
        log_backup_count=int(log.get("backup_count") or 5),
        todo_db=resolve("todo_db", "var/ultron.db"),
        todo_timezone=str(todo.get("timezone") or "Europe/Paris"),
        todo_categories=tuple(categories),
        todo_notify_target=str(todo.get("notify_target") or "telegram"),
        hermes_binary=os.environ.get("ULTRON_HUB_HERMES_BIN") or todo.get("hermes_binary") or None,
        envoi_mode=envoi_mode,
        envoi_chat_id=str(envoi["chat_id"]) if envoi.get("chat_id") else None,
        missions_dir=resolve("missions_dir", "var/missions"),
        repos_dir=repos_dir,
        repo_tests={str(name): tuple(cmd) for name, cmd in tests.items()},
        esprit_dir=_esprit_dir(paths.get("esprit_dir")),
        savoir_dir=_savoir_dir(paths.get("savoir_dir")),
        video_skill_dir=_video_skill_dir(video.get("skill_dir")),
    )


def _esprit_dir(raw) -> Path:
    if os.environ.get(ESPRIT_ENV) or not raw:
        return default_esprit_dir()
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise ConfigError("paths.esprit_dir must be an absolute path (or start with ~)")
    return path


def _savoir_dir(raw) -> Path:
    if os.environ.get(SAVOIR_ENV) or not raw:
        return default_savoir_dir()
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise ConfigError("paths.savoir_dir must be an absolute path (or start with ~)")
    return path


def _video_skill_dir(raw) -> Path:
    if not raw:
        return Settings.video_skill_dir
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise ConfigError("video.skill_dir must be an absolute path (or start with ~)")
    return path
