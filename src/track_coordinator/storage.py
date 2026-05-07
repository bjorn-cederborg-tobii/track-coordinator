from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Iterator, TypeVar

from .models import State

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

T = TypeVar("T")


@dataclass(frozen=True)
class AppPaths:
    state_dir: Path
    data_path: Path
    lock_path: Path


def default_paths() -> AppPaths:
    configured = os.environ.get("TRACK_COORDINATOR_HOME")
    if configured:
        state_dir = Path(configured).expanduser()
    else:
        xdg_state_home = os.environ.get("XDG_STATE_HOME")
        if xdg_state_home:
            state_dir = Path(xdg_state_home).expanduser() / "track-coordinator"
        else:
            state_dir = Path.home() / ".local" / "state" / "track-coordinator"
    return AppPaths(
        state_dir=state_dir,
        data_path=state_dir / "tracks.json",
        lock_path=state_dir / "tracks.lock",
    )


class Store:
    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or default_paths()

    def load(self) -> State:
        return self._load_unlocked()

    def update(self, mutator: Callable[[State], T]) -> T:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.paths.lock_path):
            state = self._load_unlocked()
            result = mutator(state)
            self._save_unlocked(state)
            return result

    def _load_unlocked(self) -> State:
        if not self.paths.data_path.exists():
            return State()
        with self.paths.data_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError(f"State file {self.paths.data_path} is not a JSON object.")
        return State.from_dict(raw)

    def _save_unlocked(self, state: State) -> None:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix="tracks.",
            suffix=".json.tmp",
            dir=self.paths.state_dir,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state.to_dict(), handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.paths.data_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        _acquire_lock(handle)
        try:
            yield
        finally:
            _release_lock(handle)


def _acquire_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:  # pragma: no cover
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    raise RuntimeError("No supported file locking implementation available.")


def _release_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    raise RuntimeError("No supported file locking implementation available.")

