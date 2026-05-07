from __future__ import annotations

from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap

from .git_tools import GitContext, GitError, add_worktree, current_context, list_worktrees, remove_worktree
from .models import STATUS_ORDER, Session, State, Track, slugify, utc_now
from .storage import Store


class CliError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexSessionMetadata:
    session_id: str
    name: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class InitHereResult:
    track_id: str
    created: bool
    session_id: str | None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Store()
    try:
        return dispatch(args, store)
    except (CliError, GitError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="track")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List tracks.")
    list_parser.add_argument("--all", action="store_true", help="Include done tracks.")
    subparsers.add_parser("paused", help="List parked tracks.")

    show_parser = subparsers.add_parser("show", help="Show a track.")
    show_parser.add_argument("track")

    subparsers.add_parser("here", help="Show the current track.")

    init_here_parser = subparsers.add_parser(
        "init-here",
        help="Create or refresh the track for the current worktree and attach the current Codex session.",
    )
    init_here_parser.add_argument("name", nargs="?", help="Track name. Defaults to a name derived from the current worktree.")
    init_here_parser.add_argument("--workspace", help="Workspace file to store for the track.")

    new_parser = subparsers.add_parser("new", help="Create a new track, or use --here to create one for the current worktree.")
    new_parser.add_argument("name")
    new_parser.add_argument("--here", action="store_true", help="Create a track for the current worktree instead of creating a new worktree.")
    new_parser.add_argument("--base", help="Base ref for the new branch when creating a new worktree track.")
    new_parser.add_argument("--purpose", help="Short purpose or brief for the new track.")
    new_parser.add_argument("--workspace", help="Workspace file to store for the track.")

    open_parser = subparsers.add_parser("open", help="Open a track in VS Code.")
    open_parser.add_argument("track")

    command_help = {
        "pause": "Mark a track as parked.",
        "park": "Mark a track as parked.",
        "wait": "Mark a track as waiting.",
        "wake": "Mark a track as active.",
        "activate": "Mark a track as active.",
        "done": "Mark a track as done.",
    }
    for command_name in ("pause", "park", "wait", "wake", "activate", "done"):
        state_parser = subparsers.add_parser(command_name, help=command_help[command_name])
        state_parser.add_argument("track")

    cleanup_parser = subparsers.add_parser("cleanup", help="Clean up a done track.")
    cleanup_parser.add_argument("track")
    cleanup_parser.add_argument("--remove-worktree", action="store_true", help="Remove the linked git worktree from disk.")

    next_parser = subparsers.add_parser("next", help="Set the next step for a track.")
    next_parser.add_argument("track")
    next_parser.add_argument("text", nargs="+")

    note_parser = subparsers.add_parser("note", help="Set or edit track notes.")
    note_parser.add_argument("parts", nargs="+")
    note_parser.add_argument("--append", action="store_true", help="Append to the existing note.")

    subparsers.add_parser("scan", help="List untracked worktrees in the current repo.")

    codex_parser = subparsers.add_parser("codex", help="Codex session commands.")
    codex_subparsers = codex_parser.add_subparsers(dest="codex_command", required=True)

    codex_attach = codex_subparsers.add_parser("attach", help="Attach a Codex session to a track.")
    codex_attach.add_argument("track")
    codex_attach.add_argument("session_id")

    codex_attach_current = codex_subparsers.add_parser(
        "attach-current",
        help="Attach the current Codex session to a track or the current track.",
    )
    codex_attach_current.add_argument("track", nargs="?")

    codex_name = codex_subparsers.add_parser("name", help="Set a human-readable alias for a session.")
    codex_name.add_argument("session_id")
    codex_name.add_argument("alias")

    codex_list = codex_subparsers.add_parser("list", help="List Codex sessions for a track or the current track.")
    codex_list.add_argument("track", nargs="?")

    codex_unlabeled = codex_subparsers.add_parser("unlabeled", help="List discoverable unlabeled Codex sessions.")
    codex_unlabeled.set_defaults(no_args=True)

    codex_resume = codex_subparsers.add_parser("resume", help="Show which Codex session to reopen in the VS Code extension.")
    codex_resume.add_argument("track")
    codex_resume.add_argument("session", nargs="?")

    interactive_parser = subparsers.add_parser("i", help="Interactive fzf-backed commands.")
    interactive_subparsers = interactive_parser.add_subparsers(dest="interactive_command", required=True)

    for interactive_name in ("open", "park", "wake", "done", "show", "scan"):
        interactive_subparsers.add_parser(interactive_name)

    interactive_cleanup = interactive_subparsers.add_parser("cleanup")
    interactive_cleanup.add_argument("--remove-worktree", action="store_true")

    interactive_codex = interactive_subparsers.add_parser("codex")
    interactive_codex_subparsers = interactive_codex.add_subparsers(dest="interactive_codex_command", required=True)
    interactive_codex_subparsers.add_parser("resume")

    return parser


def dispatch(args: Namespace, store: Store) -> int:
    if args.command == "list":
        return command_list(store, include_done=args.all)
    if args.command == "paused":
        return command_paused(store)
    if args.command == "show":
        return command_show(store, args.track)
    if args.command == "here":
        return command_here(store)
    if args.command == "init-here":
        return command_init_here(store, args)
    if args.command == "new":
        return command_new(store, args)
    if args.command == "open":
        return command_open(store, args.track)
    if args.command in {"pause", "park", "wait", "wake", "activate", "done"}:
        status = {
            "pause": "parked",
            "park": "parked",
            "wait": "waiting",
            "wake": "active",
            "activate": "active",
            "done": "done",
        }[args.command]
        return command_status(store, args.track, status)
    if args.command == "cleanup":
        return command_cleanup(store, args.track, remove_worktree_flag=args.remove_worktree)
    if args.command == "next":
        return command_next(store, args.track, " ".join(args.text))
    if args.command == "note":
        return command_note(store, args)
    if args.command == "scan":
        return command_scan(store)
    if args.command == "codex":
        return command_codex(store, args)
    if args.command == "i":
        return command_interactive(store, args)
    raise CliError(f"Unsupported command: {args.command}")


def command_list(store: Store, include_done: bool) -> int:
    return command_list_filtered(store, include_done=include_done)


def command_paused(store: Store) -> int:
    return command_list_filtered(store, statuses={"parked"})


def command_list_filtered(store: Store, include_done: bool = False, statuses: set[str] | None = None) -> int:
    state = store.load()
    tracks = filter_tracks(state.tracks, include_done=include_done, statuses=statuses)
    session_count = session_counts(state)
    rows = [
        [
            track.status,
            track.id,
            track.branch,
            str(session_count.get(track.id, 0)),
            track.worktree_path,
            shorten(track.next_step, 36),
        ]
        for track in tracks
    ]
    if not rows:
        print("No tracks found.")
        return 0
    print(render_table(["Status", "Track", "Branch", "Sessions", "Worktree", "Next"], rows))
    return 0


def command_show(store: Store, track_ref: str) -> int:
    state = store.load()
    track = resolve_track(state, track_ref)
    session_metadata = codex_session_metadata_map(state.sessions)
    attached_sessions = sorted(
        [session for session in state.sessions if session.track_id == track.id and session.provider == "codex"],
        key=lambda item: session_sort_key(item, session_metadata),
        reverse=True,
    )
    print(f"Track: {track.name} ({track.id})")
    print(f"Status: {track.status}")
    print(f"Repo: {track.repo_path}")
    print(f"Worktree: {track.worktree_path}")
    print(f"Branch: {track.branch}")
    if track.parent_track_id:
        print(f"Parent: {track.parent_track_id}")
    if track.purpose:
        print(f"Purpose: {track.purpose}")
    if track.cleaned_at:
        print(f"Cleaned: {track.cleaned_at}")
    if track.worktree_removed_at:
        print(f"Worktree removed: {track.worktree_removed_at}")
    if track.workspace_path:
        print(f"Workspace: {track.workspace_path}")
    print(f"Last touched: {track.last_touched_at}")
    print(f"Next step: {track.next_step or '-'}")
    if track.notes:
        print("Notes:")
        print(track.notes)
    if attached_sessions:
        print("Codex sessions:")
        for session in attached_sessions:
            label = session.alias or "-"
            name = session_metadata.get(session.id, CodexSessionMetadata(session.id)).name or "-"
            print(f"  {session.id}  alias={label}  name={name}")
    return 0


def command_here(store: Store) -> int:
    context = current_context(Path.cwd())
    state = store.load()
    track = match_track_for_context(state, context)
    if track is None:
        derived_name = derive_track_name(context.repo_path, context.worktree_path)
        print("No matching track for the current git context.")
        print(f"Repo: {context.repo_path}")
        print(f"Worktree: {context.worktree_path}")
        print(f"Branch: {context.branch}")
        print(f'Create one with: track init-here "{derived_name}"')
        return 1
    return command_show(store, track.id)


def command_init_here(store: Store, args: Namespace) -> int:
    context = current_context(Path.cwd())
    now = utc_now()
    requested_name = args.name or derive_track_name(context.repo_path, context.worktree_path)
    workspace_path = normalize_optional_path(args.workspace)
    if workspace_path is None:
        workspace_path = autodetect_workspace(context.worktree_path)
    session_id = current_codex_session_id(context.worktree_path, Path.cwd())

    def mutate(state: State) -> InitHereResult:
        track = match_track_for_context(state, context)
        created = False
        if track is None:
            track = create_track(state, requested_name, context, workspace_path, now)
            created = True
        else:
            validate_requested_track_name(track, args.name)
            maybe_update_track_workspace(track, workspace_path, now)

        if session_id:
            attach_session_to_track(state, track, session_id, now)
        return InitHereResult(track_id=track.id, created=created, session_id=session_id)

    result = store.update(mutate)
    command_show(store, result.track_id)
    if result.created:
        print("Initialized current worktree.")
    if result.session_id:
        print(f"Attached current Codex session: {result.session_id}")
    else:
        print("Current Codex session: not found")
    return 0


def command_new(store: Store, args: Namespace) -> int:
    context = current_context(Path.cwd())
    now = utc_now()
    track_id = slugify(args.name)

    def mutate(state: State) -> None:
        if any(track.id == track_id for track in state.tracks):
            raise CliError(f"Track '{track_id}' already exists.")

        if args.here:
            if args.base:
                raise CliError("--base can only be used when creating a new worktree track.")
            if match_track_for_context(state, context) is not None:
                raise CliError("A track already exists for the current worktree.")
            new_context = context
            parent_track_id = None
        else:
            parent_track = match_track_for_context(state, context)
            if parent_track is None:
                raise CliError("No matching track for the current git context. Use 'track init-here' or 'track new <name> --here' first.")
            branch = f"p/bcg/{track_id}"
            base_ref = args.base or "HEAD"
            worktree_path = context.repo_path.parent / f"{context.repo_path.name}-{track_id}"
            if worktree_path.exists():
                raise CliError(f"Worktree path already exists: {worktree_path}")
            add_worktree(context.worktree_path, branch, worktree_path, base_ref)
            new_context = current_context(worktree_path)
            parent_track_id = parent_track.id

        workspace_path = normalize_optional_path(args.workspace)
        if workspace_path is None:
            workspace_path = autodetect_workspace(new_context.worktree_path)
        create_track(
            state,
            args.name,
            new_context,
            workspace_path,
            now,
            parent_track_id=parent_track_id,
            purpose=args.purpose,
        )

    store.update(mutate)
    return command_show(store, track_id)


def command_open(store: Store, track_ref: str) -> int:
    state = store.load()
    track = resolve_track(state, track_ref)
    target = open_target(track)
    command = ["code", "-n", target]
    if not launch_command(command):
        print(" ".join(shlex.quote(part) for part in command))
    return 0


def command_status(store: Store, track_ref: str, status: str) -> int:
    now = utc_now()

    def mutate(state: State) -> Track:
        track = resolve_track(state, track_ref)
        track.status = status
        track.updated_at = now
        track.last_touched_at = now
        return track

    track = store.update(mutate)
    print(f"{track.id}: {track.status}")
    return 0


def command_cleanup(store: Store, track_ref: str, remove_worktree_flag: bool) -> int:
    state = store.load()
    track = resolve_track(state, track_ref)
    ensure_cleanup_allowed(track, remove_worktree_flag)

    worktree_path = Path(track.worktree_path)
    removed_now = False
    if remove_worktree_flag and track.worktree_removed_at is None:
        if not worktree_path.exists():
            removed_now = True
        else:
            remove_worktree(Path(track.repo_path), worktree_path)
            removed_now = True

    now = utc_now()

    def mutate(current_state: State) -> Track:
        current_track = resolve_track(current_state, track_ref)
        ensure_cleanup_allowed(current_track, remove_worktree_flag)
        if current_track.cleaned_at is None:
            current_track.cleaned_at = now
        if remove_worktree_flag and removed_now and current_track.worktree_removed_at is None:
            current_track.worktree_removed_at = now
        touch_track(current_track, now)
        return current_track

    cleaned_track = store.update(mutate)
    print(f"{cleaned_track.id}: cleaned")
    if remove_worktree_flag and cleaned_track.worktree_removed_at:
        print(f"{cleaned_track.id}: worktree removed")
    return 0


def command_next(store: Store, track_ref: str, text: str) -> int:
    now = utc_now()

    def mutate(state: State) -> Track:
        track = resolve_track(state, track_ref)
        track.next_step = text
        track.updated_at = now
        track.last_touched_at = now
        return track

    track = store.update(mutate)
    print(f"{track.id}: {track.next_step}")
    return 0


def command_note(store: Store, args: Namespace) -> int:
    if args.parts[0] == "edit":
        if len(args.parts) != 2:
            raise CliError("Usage: track note edit <track>")
        return command_note_edit(store, args.parts[1])

    if len(args.parts) < 2:
        raise CliError("Usage: track note <track> <text>")
    track_ref = args.parts[0]
    text = " ".join(args.parts[1:])
    now = utc_now()

    def mutate(state: State) -> Track:
        track = resolve_track(state, track_ref)
        track.notes = f"{track.notes.rstrip()}\n{text}".strip() if args.append and track.notes else text
        track.updated_at = now
        track.last_touched_at = now
        return track

    track = store.update(mutate)
    print(f"{track.id} note updated.")
    return 0


def command_note_edit(store: Store, track_ref: str) -> int:
    state = store.load()
    track = resolve_track(state, track_ref)
    editor = editor_command()
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".md", delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(track.notes)
        handle.flush()
    try:
        completed = subprocess.run(editor + [str(temp_path)], check=False)
        if completed.returncode != 0:
            raise CliError(f"Editor exited with code {completed.returncode}")
        new_text = temp_path.read_text(encoding="utf-8").rstrip()
    finally:
        temp_path.unlink(missing_ok=True)

    now = utc_now()

    def mutate(state: State) -> Track:
        refreshed = resolve_track(state, track_ref)
        refreshed.notes = new_text
        refreshed.updated_at = now
        refreshed.last_touched_at = now
        return refreshed

    track = store.update(mutate)
    print(f"{track.id} note updated.")
    return 0


def command_scan(store: Store) -> int:
    context = current_context(Path.cwd())
    candidates = unmatched_worktrees(store.load(), context.repo_path)
    if not candidates:
        print("No untracked worktrees found.")
        return 0
    rows = [[item.branch, str(item.path)] for item in candidates]
    print(render_table(["Branch", "Worktree"], rows))
    return 0


def command_codex(store: Store, args: Namespace) -> int:
    if args.codex_command == "attach":
        return command_codex_attach(store, args.track, args.session_id)
    if args.codex_command == "attach-current":
        return command_codex_attach_current(store, args.track)
    if args.codex_command == "name":
        return command_codex_name(store, args.session_id, args.alias)
    if args.codex_command == "list":
        return command_codex_list(store, args.track)
    if args.codex_command == "unlabeled":
        return command_codex_unlabeled(store)
    if args.codex_command == "resume":
        return command_codex_resume(store, args.track, args.session)
    raise CliError(f"Unsupported codex command: {args.codex_command}")


def command_codex_attach(store: Store, track_ref: str, session_id: str) -> int:
    now = utc_now()

    def mutate(state: State) -> Session:
        track = resolve_track(state, track_ref)
        session = find_session(state, "codex", session_id)
        if session is None:
            session = Session(provider="codex", id=session_id, track_id=track.id, created_at=now, updated_at=now, last_touched_at=now)
            state.sessions.append(session)
        else:
            session.track_id = track.id
            session.updated_at = now
            session.last_touched_at = now
        touch_track(track, now)
        return session

    session = store.update(mutate)
    print(f"{session.id}: attached to {session.track_id}")
    return 0


def command_codex_attach_current(store: Store, track_ref: str | None) -> int:
    context = current_context(Path.cwd())
    session_id = current_codex_session_id(context.worktree_path, Path.cwd())
    if not session_id:
        raise CliError("Current Codex session could not be discovered from the environment or Codex session metadata.")

    if track_ref:
        return command_codex_attach(store, track_ref, session_id)

    state = store.load()
    track = match_track_for_context(state, context)
    if track is None:
        raise CliError("No matching track for the current git context. Specify a track explicitly.")
    return command_codex_attach(store, track.id, session_id)


def command_codex_name(store: Store, session_id: str, alias: str) -> int:
    now = utc_now()

    def mutate(state: State) -> Session:
        session = find_session(state, "codex", session_id)
        if session is None:
            session = Session(provider="codex", id=session_id, alias=alias, created_at=now, updated_at=now, last_touched_at=now)
            state.sessions.append(session)
        else:
            session.alias = alias
            session.updated_at = now
            session.last_touched_at = now
        return session

    session = store.update(mutate)
    print(f"{session.id}: {session.alias}")
    return 0


def command_codex_list(store: Store, track_ref: str | None) -> int:
    state = store.load()
    track = resolve_track_or_current(state, track_ref)
    session_metadata = codex_session_metadata_map(state.sessions)
    sessions = sorted(
        [session for session in state.sessions if session.track_id == track.id and session.provider == "codex"],
        key=lambda item: session_sort_key(item, session_metadata),
        reverse=True,
    )
    if not sessions:
        print(f"No Codex sessions attached to {track.id}.")
        return 0
    rows = [
        [
            session.id,
            session.alias or "-",
            session_metadata.get(session.id, CodexSessionMetadata(session.id)).name or "-",
            session_activity_at(session, session_metadata),
        ]
        for session in sessions
    ]
    print(render_table(["Session", "Alias", "Name", "Activity"], rows))
    return 0


def command_codex_unlabeled(store: Store) -> int:
    state = store.load()
    attached_ids = {
        session.id
        for session in state.sessions
        if session.provider == "codex" and session.track_id
    }
    discovered = []
    session_index_path = Path.home() / ".codex" / "session_index.jsonl"
    if not session_index_path.exists():
        print("No Codex session index found.")
        return 0
    with session_index_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            session_id = str(record.get("id", "")).strip()
            if not session_id or session_id in attached_ids:
                continue
            discovered.append(
                [
                    session_id,
                    str(record.get("thread_name", "")) or "-",
                    str(record.get("updated_at", "")) or "-",
                ]
            )
    if not discovered:
        print("No unlabeled Codex sessions found.")
        return 0
    print(render_table(["Session", "Name", "Updated"], discovered))
    return 0


def command_codex_resume(store: Store, track_ref: str, session_ref: str | None) -> int:
    state = store.load()
    track = resolve_track(state, track_ref)
    metadata = codex_session_metadata_map(state.sessions)
    session = select_track_session(state, track.id, session_ref, metadata)
    if session is None:
        raise CliError(f"No Codex session available for track '{track.id}'.")

    session_metadata = metadata.get(session.id, CodexSessionMetadata(session.id))
    session_name = session_metadata.name or session.alias or session.id
    raise CliError(
        "VS Code extension resume is not supported yet. "
        f"Open the track with 'track open {track.id}' and reopen the Codex session named '{session_name}'."
    )


def command_interactive(store: Store, args: Namespace) -> int:
    if args.interactive_command == "scan":
        return command_interactive_scan(store)
    if args.interactive_command == "codex":
        if args.interactive_codex_command == "resume":
            return command_interactive_codex_resume(store)
        raise CliError(f"Unsupported interactive codex command: {args.interactive_codex_command}")
    if args.interactive_command == "wake":
        track_ref = pick_track(store, statuses={"parked"})
        if track_ref is None:
            return 1
        return command_status(store, track_ref, "active")
    if args.interactive_command == "cleanup":
        track_ref = pick_track(store, statuses={"done"})
        if track_ref is None:
            return 1
        return command_cleanup(store, track_ref, remove_worktree_flag=args.remove_worktree)

    track_ref = pick_track(store, include_done=args.interactive_command == "done")
    if track_ref is None:
        return 1
    if args.interactive_command == "open":
        return command_open(store, track_ref)
    if args.interactive_command == "park":
        return command_status(store, track_ref, "parked")
    if args.interactive_command == "done":
        return command_status(store, track_ref, "done")
    if args.interactive_command == "show":
        return command_show(store, track_ref)
    raise CliError(f"Unsupported interactive command: {args.interactive_command}")


def command_interactive_scan(store: Store) -> int:
    context = current_context(Path.cwd())
    state = store.load()
    candidates = unmatched_worktrees(state, context.repo_path)
    if not candidates:
        print("No untracked worktrees found.")
        return 0

    options = [f"{candidate.path}\t{candidate.branch}" for candidate in candidates]
    selected = run_fzf(options, prompt="worktrees> ", multi=True)
    if not selected:
        return 1

    created: list[str] = []
    for item in selected:
        path_text = item.split("\t", 1)[0]
        worktree_path = Path(path_text)
        worktree_context = current_context(worktree_path)
        proposed_name = derive_track_name(worktree_context.repo_path, worktree_context.worktree_path)
        track_id = slugify(proposed_name)
        now = utc_now()

        def mutate(current_state: State) -> None:
            if any(track.id == track_id for track in current_state.tracks):
                return
            current_state.tracks.append(
                Track(
                    id=track_id,
                    name=proposed_name,
                    status="active",
                    repo_path=str(worktree_context.repo_path),
                    worktree_path=str(worktree_context.worktree_path),
                    branch=worktree_context.branch,
                    workspace_path=str(autodetect_workspace(worktree_context.worktree_path) or "") or None,
                    created_at=now,
                    updated_at=now,
                    last_touched_at=now,
                )
            )

        store.update(mutate)
        created.append(track_id)

    if created:
        print("\n".join(created))
    return 0


def command_interactive_codex_resume(store: Store) -> int:
    state = store.load()
    session_metadata = codex_session_metadata_map(state.sessions)
    eligible_tracks = [
        track
        for track in state.tracks
        if any(session.provider == "codex" and session.track_id == track.id for session in state.sessions)
    ]
    if not eligible_tracks:
        raise CliError("No tracks with attached Codex sessions.")
    options = [f"{track.id}\t{track.name}\t{track.branch}" for track in eligible_tracks]
    selection = run_fzf(options, prompt="track> ")
    if not selection:
        return 1
    track_ref = selection[0].split("\t", 1)[0]
    track = resolve_track(state, track_ref)
    sessions = sorted(
        [session for session in state.sessions if session.provider == "codex" and session.track_id == track.id],
        key=lambda item: session_sort_key(item, session_metadata),
        reverse=True,
    )
    if len(sessions) == 1:
        return command_codex_resume(store, track.id, sessions[0].id)
    session_options = [
        "\t".join(
            [
                session.id,
                session.alias or "-",
                session_metadata.get(session.id, CodexSessionMetadata(session.id)).name or "-",
                session_activity_at(session, session_metadata),
            ]
        )
        for session in sessions
    ]
    session_selection = run_fzf(session_options, prompt="session> ")
    if not session_selection:
        return 1
    session_ref = session_selection[0].split("\t", 1)[0]
    return command_codex_resume(store, track.id, session_ref)


def pick_track(store: Store, include_done: bool = False, statuses: set[str] | None = None) -> str | None:
    state = store.load()
    tracks = filter_tracks(state.tracks, include_done=include_done, statuses=statuses)
    if not tracks:
        raise CliError("No tracks available.")
    options = [f"{track.id}\t{track.status}\t{track.branch}\t{track.name}" for track in tracks]
    selection = run_fzf(options, prompt="track> ")
    if not selection:
        return None
    return selection[0].split("\t", 1)[0]


def resolve_track(state: State, track_ref: str) -> Track:
    normalized = track_ref.strip().casefold()
    if not normalized:
        raise CliError("Track reference cannot be empty.")

    exact_id_matches = [track for track in state.tracks if track.id.casefold() == normalized]
    if len(exact_id_matches) == 1:
        return exact_id_matches[0]

    exact_name_matches = [track for track in state.tracks if track.name.casefold() == normalized]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0]

    prefix_matches = [track for track in state.tracks if track.id.casefold().startswith(normalized)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        raise CliError(f"Track reference '{track_ref}' is ambiguous.")

    name_prefix_matches = [track for track in state.tracks if track.name.casefold().startswith(normalized)]
    if len(name_prefix_matches) == 1:
        return name_prefix_matches[0]
    if len(name_prefix_matches) > 1:
        raise CliError(f"Track reference '{track_ref}' is ambiguous.")

    raise CliError(f"Track '{track_ref}' was not found.")


def resolve_track_or_current(state: State, track_ref: str | None) -> Track:
    if track_ref:
        return resolve_track(state, track_ref)

    context = current_context(Path.cwd())
    track = match_track_for_context(state, context)
    if track is None:
        raise CliError("No matching track for the current git context. Specify a track explicitly.")
    return track


def match_track_for_context(state: State, context: GitContext) -> Track | None:
    exact = [track for track in state.tracks if Path(track.worktree_path).resolve() == context.worktree_path.resolve()]
    if len(exact) == 1:
        return exact[0]

    branch_matches = [
        track
        for track in state.tracks
        if Path(track.repo_path).resolve() == context.repo_path.resolve() and track.branch == context.branch
    ]
    if len(branch_matches) == 1:
        return branch_matches[0]
    return None


def create_track(
    state: State,
    name: str,
    context: GitContext,
    workspace_path: Path | None,
    now: str,
    parent_track_id: str | None = None,
    purpose: str | None = None,
) -> Track:
    track_id = slugify(name)
    if any(track.id == track_id for track in state.tracks):
        raise CliError(f"Track '{track_id}' already exists.")
    track = Track(
        id=track_id,
        name=name,
        status="active",
        repo_path=str(context.repo_path),
        worktree_path=str(context.worktree_path),
        branch=context.branch,
        parent_track_id=parent_track_id,
        purpose=purpose,
        workspace_path=str(workspace_path) if workspace_path else None,
        created_at=now,
        updated_at=now,
        last_touched_at=now,
    )
    state.tracks.append(track)
    return track


def validate_requested_track_name(track: Track, requested_name: str | None) -> None:
    if not requested_name:
        return
    requested_id = slugify(requested_name)
    if track.id == requested_id:
        return
    if track.name.casefold() == requested_name.casefold():
        return
    raise CliError(f"Current worktree already belongs to track '{track.id}'.")


def maybe_update_track_workspace(track: Track, workspace_path: Path | None, now: str) -> None:
    if workspace_path is None:
        return
    workspace_text = str(workspace_path)
    if track.workspace_path == workspace_text:
        return
    track.workspace_path = workspace_text
    touch_track(track, now)


def attach_session_to_track(state: State, track: Track, session_id: str, now: str) -> Session:
    session = find_session(state, "codex", session_id)
    if session is None:
        session = Session(
            provider="codex",
            id=session_id,
            track_id=track.id,
            created_at=now,
            updated_at=now,
            last_touched_at=now,
        )
        state.sessions.append(session)
    else:
        session.track_id = track.id
        session.updated_at = now
        session.last_touched_at = now
    touch_track(track, now)
    return session


def find_session(state: State, provider: str, session_id: str) -> Session | None:
    for session in state.sessions:
        if session.provider == provider and session.id == session_id:
            return session
    return None


def select_track_session(
    state: State,
    track_id: str,
    session_ref: str | None,
    metadata: dict[str, CodexSessionMetadata] | None = None,
) -> Session | None:
    metadata = metadata or codex_session_metadata_map(state.sessions)
    sessions = [
        session
        for session in state.sessions
        if session.provider == "codex" and session.track_id == track_id
    ]
    sessions.sort(key=lambda item: session_sort_key(item, metadata), reverse=True)
    if session_ref is None:
        return sessions[0] if sessions else None

    normalized = session_ref.casefold()
    exact = [session for session in sessions if session.id.casefold() == normalized]
    if len(exact) == 1:
        return exact[0]
    alias_matches = [session for session in sessions if (session.alias or "").casefold() == normalized]
    if len(alias_matches) == 1:
        return alias_matches[0]
    name_matches = [
        session
        for session in sessions
        if (metadata.get(session.id, CodexSessionMetadata(session.id)).name or "").casefold() == normalized
    ]
    if len(name_matches) == 1:
        return name_matches[0]
    prefix_matches = [session for session in sessions if session.id.casefold().startswith(normalized)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        raise CliError(f"Session reference '{session_ref}' is ambiguous.")
    name_prefix_matches = [
        session
        for session in sessions
        if (metadata.get(session.id, CodexSessionMetadata(session.id)).name or "").casefold().startswith(normalized)
    ]
    if len(name_prefix_matches) == 1:
        return name_prefix_matches[0]
    if len(name_prefix_matches) > 1:
        raise CliError(f"Session reference '{session_ref}' is ambiguous.")
    raise CliError(f"Session '{session_ref}' was not found on track '{track_id}'.")


def session_counts(state: State) -> dict[str, int]:
    counts: dict[str, int] = {}
    for session in state.sessions:
        if not session.track_id:
            continue
        counts[session.track_id] = counts.get(session.track_id, 0) + 1
    return counts


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    string_rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in string_rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render_row(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()

    lines = [render_row(headers), render_row(["-" * width for width in widths])]
    lines.extend(render_row(row) for row in string_rows)
    return "\n".join(lines)


def shorten(value: str, width: int) -> str:
    if not value:
        return "-"
    return textwrap.shorten(value, width=width, placeholder="...")


def sort_tracks(tracks: list[Track]) -> list[Track]:
    ordered = sorted(tracks, key=lambda track: track.last_touched_at, reverse=True)
    return sorted(ordered, key=lambda track: STATUS_ORDER.get(track.status, 99))


def filter_tracks(tracks: list[Track], include_done: bool = False, statuses: set[str] | None = None) -> list[Track]:
    filtered = sort_tracks(tracks)
    if statuses is not None:
        return [track for track in filtered if track.status in statuses]
    if not include_done:
        return [track for track in filtered if track.status != "done"]
    return filtered


def touch_track(track: Track, when: str) -> None:
    track.updated_at = when
    track.last_touched_at = when


def ensure_cleanup_allowed(track: Track, remove_worktree_flag: bool) -> None:
    if track.status != "done":
        raise CliError(f"Cleanup requires track '{track.id}' to already be done.")
    if not remove_worktree_flag:
        return
    if Path(track.worktree_path).resolve() == Path(track.repo_path).resolve():
        raise CliError(f"Cannot remove the main checkout for track '{track.id}'.")


def open_target(track: Track) -> str:
    worktree_path = Path(track.worktree_path)
    if track.worktree_removed_at or not worktree_path.exists():
        raise CliError(f"Worktree for track '{track.id}' no longer exists: {track.worktree_path}")
    if track.workspace_path:
        workspace_path = Path(track.workspace_path)
        if workspace_path.exists():
            return str(workspace_path)
    return track.worktree_path


def normalize_optional_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser().resolve()


def autodetect_workspace(worktree_path: Path) -> Path | None:
    workspaces = sorted(worktree_path.glob("*.code-workspace"))
    if len(workspaces) == 1:
        return workspaces[0].resolve()
    return None


def derive_track_name(repo_path: Path, worktree_path: Path) -> str:
    name = worktree_path.name
    repo_prefix = f"{repo_path.name}-"
    if name.startswith(repo_prefix):
        name = name[len(repo_prefix) :]
    return name.replace("_", " ")


def current_codex_session_id(worktree_path: Path, current_cwd: Path) -> str | None:
    session_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if session_id:
        return session_id
    return discover_codex_session_id(worktree_path, current_cwd)


def codex_session_metadata_map(sessions: list[Session]) -> dict[str, CodexSessionMetadata]:
    session_ids = {session.id for session in sessions if session.provider == "codex"}
    if not session_ids:
        return {}

    metadata_by_id = read_codex_session_index()
    missing_ids = session_ids.difference(metadata_by_id)
    for session_id in missing_ids:
        fallback = read_codex_session_metadata_from_rollout(session_id)
        if fallback is not None:
            metadata_by_id[session_id] = fallback
    return {session_id: metadata_by_id[session_id] for session_id in session_ids if session_id in metadata_by_id}


def read_codex_session_index() -> dict[str, CodexSessionMetadata]:
    session_index_path = Path.home() / ".codex" / "session_index.jsonl"
    if not session_index_path.exists():
        return {}

    metadata_by_id: dict[str, CodexSessionMetadata] = {}
    with session_index_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            session_id = str(record.get("id", "")).strip()
            if not session_id:
                continue

            name = optional_text(record.get("thread_name"))
            updated_at = optional_text(record.get("updated_at"))
            metadata_by_id[session_id] = CodexSessionMetadata(session_id=session_id, name=name, updated_at=updated_at)
    return metadata_by_id


def read_codex_session_metadata_from_rollout(session_id: str) -> CodexSessionMetadata | None:
    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.exists():
        return None

    candidates = sorted(sessions_root.rglob(f"rollout-*{session_id}.jsonl"), reverse=True)
    for path in candidates:
        metadata = read_codex_session_metadata_file(path, session_id)
        if metadata is not None:
            return metadata
    return None


def read_codex_session_metadata_file(path: Path, expected_session_id: str) -> CodexSessionMetadata | None:
    updated_at: str | None = None
    name: str | None = None
    discovered_id: str | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                record_type = record.get("type")
                if record_type == "session_meta":
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    session_id = str(payload.get("id", "")).strip()
                    if session_id != expected_session_id:
                        continue
                    discovered_id = session_id
                    updated_at = optional_text(record.get("timestamp")) or updated_at
                    continue

                if record_type == "event_msg":
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") != "thread_name_updated":
                        continue
                    session_id = str(payload.get("thread_id", "")).strip()
                    if session_id != expected_session_id:
                        continue
                    discovered_id = session_id
                    name = optional_text(payload.get("thread_name")) or name
                    updated_at = optional_text(record.get("timestamp")) or updated_at
    except OSError:
        return None

    if discovered_id is None:
        return None
    return CodexSessionMetadata(session_id=discovered_id, name=name, updated_at=updated_at)


def session_activity_at(session: Session, metadata_by_id: dict[str, CodexSessionMetadata]) -> str:
    return metadata_by_id.get(session.id, CodexSessionMetadata(session.id)).updated_at or session.updated_at


def session_sort_key(session: Session, metadata_by_id: dict[str, CodexSessionMetadata]) -> str:
    return session_activity_at(session, metadata_by_id)


def discover_codex_session_id(worktree_path: Path, current_cwd: Path) -> str | None:
    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.exists():
        return None

    worktree_path = worktree_path.resolve()
    current_cwd = current_cwd.resolve()
    candidates = sorted(sessions_root.rglob("rollout-*.jsonl"), reverse=True)
    for candidate in candidates:
        session = read_session_meta(candidate)
        if session is None:
            continue
        session_id, session_cwd = session
        if path_matches_context(session_cwd, worktree_path, current_cwd):
            return session_id

    for candidate in candidates[:50]:
        session_id = read_session_id_for_activity(candidate, worktree_path, current_cwd)
        if session_id:
            return session_id
    return None


def path_matches_context(session_cwd: Path | None, worktree_path: Path, current_cwd: Path) -> bool:
    if session_cwd is None:
        return False
    resolved_session_cwd = session_cwd.resolve()
    return (
        resolved_session_cwd == worktree_path
        or current_cwd == resolved_session_cwd
        or current_cwd.is_relative_to(resolved_session_cwd)
        or worktree_path.is_relative_to(resolved_session_cwd)
    )


def read_session_id_for_activity(path: Path, worktree_path: Path, current_cwd: Path) -> str | None:
    session_id: str | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                record_type = record.get("type")
                payload = record.get("payload")
                if record_type == "session_meta":
                    if isinstance(payload, dict):
                        discovered_id = optional_text(payload.get("id"))
                        if discovered_id:
                            session_id = discovered_id
                        session_cwd = path_from_payload(payload.get("cwd"))
                        if path_matches_context(session_cwd, worktree_path, current_cwd):
                            return session_id
                    continue

                if record_type == "turn_context":
                    if isinstance(payload, dict):
                        session_cwd = path_from_payload(payload.get("cwd"))
                        if path_matches_context(session_cwd, worktree_path, current_cwd):
                            return session_id
                    continue

                if record_type == "event_msg" and isinstance(payload, dict):
                    session_cwd = path_from_payload(payload.get("cwd"))
                    if path_matches_context(session_cwd, worktree_path, current_cwd):
                        return session_id
    except OSError:
        return None
    return None


def read_session_meta(path: Path) -> tuple[str, Path | None] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            line = handle.readline().strip()
    except OSError:
        return None

    if not line:
        return None

    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None

    if record.get("type") != "session_meta":
        return None

    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None

    session_id = str(payload.get("id", "")).strip()
    if not session_id:
        return None

    session_cwd = path_from_payload(payload.get("cwd"))
    return session_id, session_cwd


def optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def path_from_payload(value: object) -> Path | None:
    text = optional_text(value)
    if text is None:
        return None
    return Path(text).expanduser()


def unmatched_worktrees(state: State, repo_path: Path) -> list:
    tracked_paths = {Path(track.worktree_path).resolve() for track in state.tracks}
    candidates = []
    for worktree in list_worktrees(repo_path):
        if worktree.path.resolve() == repo_path.resolve():
            continue
        if worktree.path.resolve() in tracked_paths:
            continue
        candidates.append(worktree)
    return candidates


def editor_command() -> list[str]:
    configured = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if configured:
        return shlex.split(configured)
    if shutil_which("code"):
        return ["code", "--wait"]
    return ["vi"]


def launch_command(command: list[str]) -> bool:
    try:
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            print("Launch failed, command:", " ".join(shlex.quote(part) for part in command), file=sys.stderr)
            return False
    except FileNotFoundError:
        print("Launch command not found, command:", " ".join(shlex.quote(part) for part in command), file=sys.stderr)
        return False
    return True


def run_fzf(options: list[str], prompt: str, multi: bool = False) -> list[str]:
    if not options:
        return []
    command = ["fzf", "--prompt", prompt]
    if multi:
        command.append("--multi")
    try:
        completed = subprocess.run(
            command,
            input="\n".join(options),
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CliError("fzf is not installed or not on PATH.") from exc
    if completed.returncode == 130:
        return []
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise CliError(stderr or "fzf selection failed")
    return [line for line in completed.stdout.splitlines() if line.strip()]


def shutil_which(binary: str) -> str | None:
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        path = Path(entry) / binary
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None
