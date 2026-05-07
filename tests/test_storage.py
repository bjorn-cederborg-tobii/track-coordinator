from pathlib import Path

from track_coordinator.models import Session, State, Track, slugify
from track_coordinator.storage import AppPaths, Store


def test_slugify_preserves_ticket_shape():
    assert slugify("XR5ML-482 Add Left Right") == "xr5ml-482-add-left-right"


def test_store_round_trip(tmp_path: Path):
    paths = AppPaths(
        state_dir=tmp_path,
        data_path=tmp_path / "tracks.json",
        lock_path=tmp_path / "tracks.lock",
    )
    store = Store(paths)
    state = State(
        tracks=[
            Track(
                id="alpha",
                name="Alpha",
                status="active",
                repo_path="/repo",
                worktree_path="/repo-alpha",
                branch="main",
            )
        ],
        sessions=[
            Session(
                provider="codex",
                id="session-1",
                alias="Session One",
                track_id="alpha",
            )
        ],
    )
    store.update(lambda current: current.tracks.extend(state.tracks) or current.sessions.extend(state.sessions))

    loaded = store.load()
    assert loaded.tracks[0].id == "alpha"
    assert loaded.sessions[0].alias == "Session One"

