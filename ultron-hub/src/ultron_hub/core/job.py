"""Job request/result data model (the JSON contract with Hermes)."""

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ultron_hub.common.errors import InvalidRequestError

STATUSES = ("ok", "error", "timeout", "denied")

# job_id is used as a directory name under var/runs/: keep it strict.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ACTION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def new_job_id() -> str:
    """Timestamped id with a short uuid suffix, e.g. 20261010T120000Z-1a2b3c4d."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def validate_action(action: Any) -> str:
    if not isinstance(action, str) or not _ACTION_RE.fullmatch(action):
        raise InvalidRequestError(f"invalid action name: {action!r}")
    return action


@dataclass
class JobRequest:
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    job_id: str = ""
    requested_by: str | None = None

    def __post_init__(self) -> None:
        validate_action(self.action)
        if not isinstance(self.params, dict):
            raise InvalidRequestError("params must be a JSON object")
        if not self.job_id:
            self.job_id = new_job_id()
        elif not isinstance(self.job_id, str) or not _JOB_ID_RE.fullmatch(self.job_id):
            raise InvalidRequestError(f"invalid job_id: {self.job_id!r}")
        if self.requested_by is not None and not isinstance(self.requested_by, str):
            raise InvalidRequestError("requested_by must be a string")

    @classmethod
    def from_dict(cls, data: Any) -> "JobRequest":
        if not isinstance(data, dict):
            raise InvalidRequestError("request must be a JSON object")
        unknown = set(data) - {"job_id", "action", "params", "requested_by"}
        if unknown:
            raise InvalidRequestError(f"unknown request fields: {sorted(unknown)}")
        return cls(
            action=data.get("action"),
            params=data.get("params") if data.get("params") is not None else {},
            job_id=data.get("job_id") or "",
            requested_by=data.get("requested_by"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "params": self.params,
            "requested_by": self.requested_by,
        }


@dataclass
class JobResult:
    job_id: str
    action: str
    status: str
    summary: str = ""
    data: Any = None
    files: list[str] = field(default_factory=list)
    error: str | None = None
    model: str = ""
    duration_s: float = 0.0
    cost_usd: float | None = None
    category: str | None = None  # failure class: auth / quota / transient / other (None when ok)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"invalid status: {self.status!r}")

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "status": self.status,
            "summary": self.summary,
            "data": self.data,
            "files": list(self.files),
            "error": self.error,
            "model": self.model,
            "duration_s": round(float(self.duration_s), 3),
            "cost_usd": self.cost_usd,
            "category": self.category,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)
