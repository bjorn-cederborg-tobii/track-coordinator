from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re

SCHEMA_VERSION = 1
STATUS_VALUES = ("active", "waiting", "parked", "done")
STATUS_ORDER = {value: index for index, value in enumerate(STATUS_VALUES)}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned:
        raise ValueError("Track name must contain at least one letter or digit.")
    return cleaned


@dataclass
class Session:
    provider: str
    id: str
    alias: str | None = None
    track_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_touched_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Session":
        return cls(
            provider=str(data["provider"]),
            id=str(data["id"]),
            alias=_optional_str(data.get("alias")),
            track_id=_optional_str(data.get("track_id")),
            created_at=_string_or_now(data.get("created_at")),
            updated_at=_string_or_now(data.get("updated_at")),
            last_touched_at=_string_or_now(data.get("last_touched_at")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "id": self.id,
            "alias": self.alias,
            "track_id": self.track_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_touched_at": self.last_touched_at,
        }


@dataclass
class Track:
    id: str
    name: str
    status: str
    repo_path: str
    worktree_path: str
    branch: str
    workspace_path: str | None = None
    next_step: str = ""
    notes: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_touched_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Track":
        status = str(data.get("status", "active"))
        if status not in STATUS_VALUES:
            status = "active"
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            status=status,
            repo_path=str(data["repo_path"]),
            worktree_path=str(data["worktree_path"]),
            branch=str(data.get("branch", "HEAD")),
            workspace_path=_optional_str(data.get("workspace_path")),
            next_step=str(data.get("next_step", "")),
            notes=str(data.get("notes", "")),
            created_at=_string_or_now(data.get("created_at")),
            updated_at=_string_or_now(data.get("updated_at")),
            last_touched_at=_string_or_now(data.get("last_touched_at")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "repo_path": self.repo_path,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "workspace_path": self.workspace_path,
            "next_step": self.next_step,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_touched_at": self.last_touched_at,
        }


@dataclass
class State:
    schema_version: int = SCHEMA_VERSION
    tracks: list[Track] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "State":
        tracks = [Track.from_dict(item) for item in data.get("tracks", [])]
        sessions = [Session.from_dict(item) for item in data.get("sessions", [])]
        schema_version = int(data.get("schema_version", SCHEMA_VERSION))
        return cls(schema_version=schema_version, tracks=tracks, sessions=sessions)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "tracks": [track.to_dict() for track in self.tracks],
            "sessions": [session.to_dict() for session in self.sessions],
        }


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _string_or_now(value: object) -> str:
    if value is None:
        return utc_now()
    text = str(value)
    return text if text else utc_now()
