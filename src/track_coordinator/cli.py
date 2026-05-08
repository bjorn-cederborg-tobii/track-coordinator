from __future__ import annotations

from argparse import SUPPRESS, ArgumentParser, Namespace
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
from urllib.parse import unquote, urlparse

from .git_tools import GitContext, GitError, add_worktree, current_context, list_worktrees, remove_worktree
from .models import STATUS_ORDER, Session, State, Track, slugify, utc_now
from .storage import Store


class CliError(RuntimeError):
    pass


BASH_COMPLETION_SCRIPT = """\
_track_complete_tracks() {
    track _complete tracks "$@" 2>/dev/null
}

_track_complete() {
    local cur prev words cword
    if declare -F _init_completion >/dev/null 2>&1; then
        _init_completion -n : || return
    else
        words=("${COMP_WORDS[@]}")
        cword="${COMP_CWORD}"
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev=""
        if (( COMP_CWORD > 0 )); then
            prev="${COMP_WORDS[COMP_CWORD-1]}"
        fi
    fi

    local commands="list paused show here prompt init-here new open resume rename purpose workspace parent remove pause park wait wake activate done cleanup next note scan sessions completion codex i"
    local state_commands="pause park wait wake activate done"
    local interactive_commands="open park wake done show scan cleanup remove codex"
    local codex_commands="attach attach-current detach name list status unlabeled resume"

    if (( cword == 1 )); then
        COMPREPLY=( $(compgen -W "${commands}" -- "${cur}") )
        return
    fi

    case "${words[1]}" in
        show|rename|remove)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "$(_track_complete_tracks --all)" -- "${cur}") )
            fi
            return
            ;;
        resume)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "$(_track_complete_tracks)" -- "${cur}") )
            fi
            return
            ;;
        open)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "$(_track_complete_tracks --all)" -- "${cur}") )
            fi
            return
            ;;
        purpose|workspace|parent)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "$(_track_complete_tracks --all)" -- "${cur}") )
                return
            fi
            if [[ "${words[1]}" == "purpose" && cword == 3 ]]; then
                COMPREPLY=( $(compgen -W "--clear" -- "${cur}") )
                return
            fi
            if [[ "${words[1]}" == "workspace" && cword == 3 ]]; then
                COMPREPLY=( $(compgen -W "--clear" -- "${cur}") )
                return
            fi
            if [[ "${words[1]}" == "parent" && cword == 3 ]]; then
                COMPREPLY=( $(compgen -W "--clear $(_track_complete_tracks --all)" -- "${cur}") )
                return
            fi
            return
            ;;
        cleanup)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "$(_track_complete_tracks --statuses done)" -- "${cur}") )
                return
            fi
            COMPREPLY=( $(compgen -W "--remove-worktree" -- "${cur}") )
            return
            ;;
        note)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "edit $(_track_complete_tracks --all)" -- "${cur}") )
                return
            fi
            if [[ "${words[2]}" == "edit" && cword == 3 ]]; then
                COMPREPLY=( $(compgen -W "$(_track_complete_tracks --all)" -- "${cur}") )
                return
            fi
            return
            ;;
        next)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "$(_track_complete_tracks --all)" -- "${cur}") )
            fi
            return
            ;;
        pause|park)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "$(_track_complete_tracks)" -- "${cur}") )
            fi
            return
            ;;
        wait)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "$(_track_complete_tracks)" -- "${cur}") )
            fi
            return
            ;;
        wake|activate)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "$(_track_complete_tracks --statuses parked)" -- "${cur}") )
            fi
            return
            ;;
        done)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "$(_track_complete_tracks)" -- "${cur}") )
            fi
            return
            ;;
        completion)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "bash" -- "${cur}") )
            fi
            return
            ;;
        codex)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "${codex_commands}" -- "${cur}") )
                return
            fi
            case "${words[2]}" in
                attach|attach-current|list|status|resume)
                    if (( cword == 3 )); then
                        COMPREPLY=( $(compgen -W "$(_track_complete_tracks --all)" -- "${cur}") )
                    fi
                    return
                    ;;
                detach)
                    return
                    ;;
            esac
            return
            ;;
        i)
            if (( cword == 2 )); then
                COMPREPLY=( $(compgen -W "${interactive_commands}" -- "${cur}") )
                return
            fi
            if [[ "${words[2]}" == "cleanup" || "${words[2]}" == "remove" ]]; then
                COMPREPLY=( $(compgen -W "--remove-worktree" -- "${cur}") )
            elif [[ "${words[2]}" == "codex" && cword == 3 ]]; then
                COMPREPLY=( $(compgen -W "attach detach resume" -- "${cur}") )
            fi
            return
            ;;
    esac
}

complete -F _track_complete track
"""


@dataclass(frozen=True)
class CodexSessionMetadata:
    session_id: str
    name: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class CodexSessionStatus:
    session_id: str
    state: str
    detail: str | None = None
    activity_at: str | None = None


@dataclass(frozen=True)
class SessionDisplayMetadata:
    provider: str
    session_id: str
    name: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class SessionDisplayStatus:
    provider: str
    session_id: str
    state: str = "unknown"
    detail: str | None = None
    activity_at: str | None = None


@dataclass(frozen=True)
class InitHereResult:
    track_id: str
    created: bool
    session_id: str | None


@dataclass(frozen=True)
class NewTrackResult:
    track_id: str
    session_id: str | None


@dataclass(frozen=True)
class DetectedWorkspace:
    source_path: Path | None
    document: dict[str, object]
    folders: list[Path]
    modified_at: float


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

    prompt_parser = subparsers.add_parser("prompt", help="Print a compact prompt label for the current track.")
    prompt_parser.add_argument("--status", action="store_true", help="Include the track status in the prompt label.")

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

    open_parser = subparsers.add_parser("open", help="Open a track in VS Code, or pick one interactively when omitted.")
    open_parser.add_argument("track", nargs="?")

    resume_parser = subparsers.add_parser("resume", help="Resume work on a track.")
    resume_parser.add_argument("track", nargs="?")

    rename_parser = subparsers.add_parser("rename", help="Rename a track display name.")
    rename_parser.add_argument("track")
    rename_parser.add_argument("name")

    purpose_parser = subparsers.add_parser("purpose", help="Set or clear a track purpose.")
    purpose_parser.add_argument("track")
    purpose_parser.add_argument("text", nargs="*")
    purpose_parser.add_argument("--clear", action="store_true", help="Clear the stored purpose.")

    workspace_parser = subparsers.add_parser("workspace", help="Set or clear a track workspace path.")
    workspace_parser.add_argument("track")
    workspace_parser.add_argument("path", nargs="?")
    workspace_parser.add_argument("--clear", action="store_true", help="Clear the stored workspace path.")

    parent_parser = subparsers.add_parser("parent", help="Set or clear a track parent.")
    parent_parser.add_argument("track")
    parent_parser.add_argument("parent", nargs="?")
    parent_parser.add_argument("--clear", action="store_true", help="Clear the stored parent track.")

    remove_parser = subparsers.add_parser("remove", help="Remove a track record.")
    remove_parser.add_argument("track")
    remove_parser.add_argument("--remove-worktree", action="store_true", help="Remove the linked git worktree from disk.")

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

    next_parser = subparsers.add_parser("next", help="Set the next step for a track or the current track.")
    next_parser.add_argument("parts", nargs="+")

    note_parser = subparsers.add_parser("note", help="Set or edit track notes.")
    note_parser.add_argument("parts", nargs="+")
    note_parser.add_argument("--append", action="store_true", help="Append to the existing note.")

    subparsers.add_parser("scan", help="List untracked worktrees in the current repo.")
    subparsers.add_parser("sessions", help="Show attached agent sessions grouped by track.")

    completion_parser = subparsers.add_parser("completion", help="Print shell completion setup.")
    completion_parser.add_argument("shell", choices=("bash",))

    internal_complete = subparsers.add_parser("_complete", help=SUPPRESS)
    internal_complete.add_argument("category", choices=("tracks",), help=SUPPRESS)
    internal_complete.add_argument("--all", action="store_true", help=SUPPRESS)
    internal_complete.add_argument("--statuses", nargs="*", help=SUPPRESS)

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

    codex_detach = codex_subparsers.add_parser("detach", help="Detach a Codex session from its track.")
    codex_detach.add_argument("session_id")

    codex_name = codex_subparsers.add_parser("name", help="Set a human-readable alias for a session.")
    codex_name.add_argument("session_id")
    codex_name.add_argument("alias")

    codex_list = codex_subparsers.add_parser("list", help="List Codex sessions for a track or the current track.")
    codex_list.add_argument("track", nargs="?")

    codex_status = codex_subparsers.add_parser("status", help="Show inferred live status for Codex sessions on a track or the current track.")
    codex_status.add_argument("track", nargs="?")

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

    interactive_remove = interactive_subparsers.add_parser("remove")
    interactive_remove.add_argument("--remove-worktree", action="store_true")

    interactive_codex = interactive_subparsers.add_parser("codex")
    interactive_codex_subparsers = interactive_codex.add_subparsers(dest="interactive_codex_command", required=True)
    interactive_codex_subparsers.add_parser("attach")
    interactive_codex_subparsers.add_parser("detach")
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
    if args.command == "prompt":
        return command_prompt(store, include_status=args.status)
    if args.command == "init-here":
        return command_init_here(store, args)
    if args.command == "new":
        return command_new(store, args)
    if args.command == "open":
        return command_open(store, args.track)
    if args.command == "resume":
        return command_resume(store, args.track)
    if args.command == "rename":
        return command_rename(store, args.track, args.name)
    if args.command == "purpose":
        return command_purpose(store, args)
    if args.command == "workspace":
        return command_workspace(store, args)
    if args.command == "parent":
        return command_parent(store, args)
    if args.command == "remove":
        return command_remove(store, args.track, remove_worktree_flag=args.remove_worktree)
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
        return command_next(store, args)
    if args.command == "note":
        return command_note(store, args)
    if args.command == "scan":
        return command_scan(store)
    if args.command == "sessions":
        return command_sessions(store)
    if args.command == "completion":
        return command_completion(args.shell)
    if args.command == "_complete":
        return command_internal_complete(store, args)
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
    attached_sessions = [session for session in state.sessions if session.track_id]
    session_metadata = session_display_metadata_map(attached_sessions)
    session_status = session_display_status_map(attached_sessions, session_metadata)
    live_rollups = session_rollups_by_track(attached_sessions, session_status)
    rows = [
        [
            track.status,
            track.id,
            track.branch,
            str(session_count.get(track.id, 0)),
            live_rollups.get(track.id, "-"),
            track.worktree_path,
            shorten(track.next_step, 36),
        ]
        for track in tracks
    ]
    if not rows:
        print("No tracks found.")
        return 0
    print(render_table(["Status", "Track", "Branch", "Sessions", "Live", "Worktree", "Next"], rows))
    return 0


def command_show(store: Store, track_ref: str) -> int:
    state = store.load()
    track = resolve_track(state, track_ref)
    attached_sessions = attached_sessions_for_track(state, track.id)
    session_metadata = session_display_metadata_map(attached_sessions)
    session_status = session_display_status_map(attached_sessions, session_metadata)
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
        print("Sessions:")
        for session in attached_sessions:
            label = session.alias or "-"
            metadata = session_metadata.get(session_ref_key(session), SessionDisplayMetadata(session.provider, session.id))
            status = session_status.get(session_ref_key(session), SessionDisplayStatus(session.provider, session.id))
            print(
                f"  {session.provider}  {session.id}  alias={label}  name={metadata.name or '-'}  status={status.state}"
            )
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


def command_prompt(store: Store, include_status: bool) -> int:
    try:
        context = current_context(Path.cwd())
    except GitError:
        return 0

    state = store.load()
    track = match_track_for_context(state, context)
    if track is None:
        return 0

    label = track.id
    if include_status:
        label = f"{label}|{track.status}"
    print(f"[{label}]")
    return 0


def command_init_here(store: Store, args: Namespace) -> int:
    context = current_context(Path.cwd())
    now = utc_now()
    requested_name = args.name or derive_track_name(context.repo_path, context.worktree_path)
    session_id = current_codex_session_id(context.worktree_path, Path.cwd())

    def mutate(state: State) -> InitHereResult:
        track = match_track_for_context(state, context)
        created = False
        if track is None:
            workspace_path = resolve_workspace_for_track(
                store,
                slugify(requested_name),
                source_worktree_path=context.worktree_path,
                target_worktree_path=context.worktree_path,
                explicit_workspace=args.workspace,
            )
            track = create_track(state, requested_name, context, workspace_path, now)
            created = True
        else:
            validate_requested_track_name(track, args.name)
            workspace_path = resolve_workspace_for_track(
                store,
                track.id,
                source_worktree_path=context.worktree_path,
                target_worktree_path=context.worktree_path,
                explicit_workspace=args.workspace,
            )
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
    session_id = current_codex_session_id(context.worktree_path, Path.cwd()) if args.here else None

    def mutate(state: State) -> NewTrackResult:
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

        workspace_path = resolve_workspace_for_track(
            store,
            track_id,
            source_worktree_path=context.worktree_path,
            target_worktree_path=new_context.worktree_path,
            explicit_workspace=args.workspace,
        )
        create_track(
            state,
            args.name,
            new_context,
            workspace_path,
            now,
            parent_track_id=parent_track_id,
            purpose=args.purpose,
        )
        if args.here and session_id:
            track = resolve_track(state, track_id)
            attach_session_to_track(state, track, session_id, now)
        return NewTrackResult(track_id=track_id, session_id=session_id if args.here else None)

    result = store.update(mutate)
    command_show(store, result.track_id)
    if args.here:
        if result.session_id:
            print(f"Attached current Codex session: {result.session_id}")
        else:
            print("Current Codex session: not found")
    return 0


def command_open(store: Store, track_ref: str | None) -> int:
    if track_ref is None:
        track_ref = pick_track(store, include_done=True)
        if track_ref is None:
            return 1

    state = store.load()
    track = resolve_track(state, track_ref)
    target = open_target(track)
    command = ["code", "-n", target]
    if not launch_command(command):
        print(" ".join(shlex.quote(part) for part in command))
    return 0


def command_resume(store: Store, track_ref: str | None) -> int:
    if track_ref is None:
        track_ref = pick_track(store)
        if track_ref is None:
            return 1

    now = utc_now()

    def mutate(state: State) -> Track:
        track = resolve_track(state, track_ref)
        if track.status == "done":
            raise CliError(f"Track '{track.id}' is done. Reopen it explicitly with 'track open {track.id}' if needed.")
        if track.status != "active":
            track.status = "active"
        touch_track(track, now)
        return track

    track = store.update(mutate)
    command_open(store, track.id)
    command_show(store, track.id)

    state = store.load()
    metadata = codex_session_metadata_map(state.sessions)
    sessions = attached_codex_sessions(state, track.id, metadata)
    if not sessions:
        print("No Codex sessions attached.")
        return 0

    if len(sessions) == 1:
        session = sessions[0]
        label = metadata.get(session.id, CodexSessionMetadata(session.id)).name or session.alias or session.id
        print(f"Reopen Codex session in the VS Code extension: {label}")
        return 0

    print("Reopen one of the attached Codex sessions listed above in the VS Code extension.")
    return 0


def command_rename(store: Store, track_ref: str, name: str) -> int:
    now = utc_now()

    def mutate(state: State) -> Track:
        track = resolve_track(state, track_ref)
        validate_track_display_name(state, track, name)
        track.name = name
        touch_track(track, now)
        return track

    track = store.update(mutate)
    print(f"{track.id}: {track.name}")
    return 0


def command_purpose(store: Store, args: Namespace) -> int:
    if args.clear:
        if args.text:
            raise CliError("Do not pass purpose text with --clear.")
        purpose: str | None = None
    else:
        if not args.text:
            raise CliError("Usage: track purpose <track> <text> or track purpose <track> --clear")
        purpose = " ".join(args.text)

    now = utc_now()

    def mutate(state: State) -> Track:
        track = resolve_track(state, args.track)
        track.purpose = purpose
        touch_track(track, now)
        return track

    track = store.update(mutate)
    print(f"{track.id}: purpose={track.purpose or '-'}")
    return 0


def command_workspace(store: Store, args: Namespace) -> int:
    if args.clear:
        if args.path:
            raise CliError("Do not pass a workspace path with --clear.")
        workspace_path: Path | None = None
    else:
        if not args.path:
            raise CliError("Usage: track workspace <track> <path> or track workspace <track> --clear")
        workspace_path = normalize_optional_path(args.path)
        if workspace_path is None:
            raise CliError("Workspace path cannot be empty.")
        if not workspace_path.exists():
            raise CliError(f"Workspace path does not exist: {workspace_path}")

    state = store.load()
    track = resolve_track(state, args.track)
    previous_managed_workspace = managed_workspace_path(store, track)
    now = utc_now()

    def mutate(current_state: State) -> Track:
        current_track = resolve_track(current_state, args.track)
        current_track.workspace_path = str(workspace_path) if workspace_path else None
        touch_track(current_track, now)
        return current_track

    updated_track = store.update(mutate)
    current_managed_workspace = managed_workspace_path(store, updated_track)
    if previous_managed_workspace is not None and previous_managed_workspace != current_managed_workspace:
        previous_managed_workspace.unlink(missing_ok=True)

    print(f"{updated_track.id}: workspace={updated_track.workspace_path or '-'}")
    return 0


def command_parent(store: Store, args: Namespace) -> int:
    if args.clear:
        if args.parent:
            raise CliError("Do not pass a parent track with --clear.")
        parent_track_id: str | None = None
    else:
        if not args.parent:
            raise CliError("Usage: track parent <track> <parent> or track parent <track> --clear")
        state = store.load()
        track = resolve_track(state, args.track)
        parent = resolve_track(state, args.parent)
        if track.id == parent.id:
            raise CliError("A track cannot be its own parent.")
        parent_track_id = parent.id

    now = utc_now()

    def mutate(state: State) -> Track:
        track = resolve_track(state, args.track)
        track.parent_track_id = parent_track_id
        touch_track(track, now)
        return track

    track = store.update(mutate)
    print(f"{track.id}: parent={track.parent_track_id or '-'}")
    return 0


def command_remove(store: Store, track_ref: str, remove_worktree_flag: bool) -> int:
    state = store.load()
    track = resolve_track(state, track_ref)
    worktree_path = Path(track.worktree_path)
    workspace_path = managed_workspace_path(store, track)
    removed_worktree = False

    if remove_worktree_flag:
        ensure_remove_worktree_allowed(track)
        if not track.worktree_removed_at and worktree_path.exists():
            remove_worktree(Path(track.repo_path), worktree_path)
            removed_worktree = True

    def mutate(current_state: State) -> tuple[int, int]:
        current_track = resolve_track(current_state, track_ref)
        current_state.tracks = [item for item in current_state.tracks if item.id != current_track.id]

        detached = 0
        for session in current_state.sessions:
            if session.track_id == current_track.id:
                session.track_id = None
                detached += 1

        cleared = 0
        for child in current_state.tracks:
            if child.parent_track_id == current_track.id:
                child.parent_track_id = None
                cleared += 1

        return detached, cleared

    detached_sessions, cleared_children = store.update(mutate)

    if workspace_path is not None:
        workspace_path.unlink(missing_ok=True)

    print(f"{track.id}: removed")
    if remove_worktree_flag and (removed_worktree or track.worktree_removed_at):
        print(f"{track.id}: worktree removed")
    if detached_sessions:
        print(f"{track.id}: detached {detached_sessions} session(s)")
    if cleared_children:
        print(f"{track.id}: cleared parent on {cleared_children} child track(s)")
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


def command_next(store: Store, args: Namespace) -> int:
    track_ref, text = resolve_next_target(store, args.parts)
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


def command_sessions(store: Store) -> int:
    state = store.load()
    attached_sessions = [session for session in state.sessions if session.track_id]
    if not attached_sessions:
        print("No attached sessions found.")
        return 0

    session_metadata = session_display_metadata_map(attached_sessions)
    session_status = session_display_status_map(attached_sessions, session_metadata)
    sessions_by_track: dict[str, list[Session]] = {}
    for session in attached_sessions:
        if session.track_id is None:
            continue
        sessions_by_track.setdefault(session.track_id, []).append(session)

    tracks = sort_tracks([track for track in state.tracks if track.id in sessions_by_track])
    blocks: list[str] = []
    for track in tracks:
        track_sessions = sorted(
            sessions_by_track.get(track.id, []),
            key=lambda item: session_sort_key(item, session_metadata),
            reverse=True,
        )
        if not track_sessions:
            continue
        rows = [
            [
                session.provider,
                session.id,
                session.alias or "-",
                session_metadata.get(session_ref_key(session), SessionDisplayMetadata(session.provider, session.id)).name or "-",
                session_status.get(session_ref_key(session), SessionDisplayStatus(session.provider, session.id)).state,
                session_status.get(session_ref_key(session), SessionDisplayStatus(session.provider, session.id)).detail or "-",
                session_activity_at(session, session_metadata),
            ]
            for session in track_sessions
        ]
        header = (
            f"Track: {track.name} ({track.id})  "
            f"status={track.status}  "
            f"branch={track.branch}  "
            f"live={session_rollup(track_sessions, session_status)}"
        )
        blocks.append("\n".join([header, render_table(["Provider", "Session", "Alias", "Name", "Status", "Detail", "Activity"], rows)]))
    print("\n\n".join(blocks))
    return 0


def command_completion(shell: str) -> int:
    if shell != "bash":
        raise CliError(f"Unsupported shell: {shell}")
    print(BASH_COMPLETION_SCRIPT.rstrip())
    return 0


def command_internal_complete(store: Store, args: Namespace) -> int:
    if args.category != "tracks":
        raise CliError(f"Unsupported completion category: {args.category}")

    statuses = set(args.statuses) if args.statuses else None
    state = store.load()
    tracks = filter_tracks(state.tracks, include_done=args.all, statuses=statuses)
    for track in tracks:
        print(track.id)
    return 0


def command_codex(store: Store, args: Namespace) -> int:
    if args.codex_command == "attach":
        return command_codex_attach(store, args.track, args.session_id)
    if args.codex_command == "attach-current":
        return command_codex_attach_current(store, args.track)
    if args.codex_command == "detach":
        return command_codex_detach(store, args.session_id)
    if args.codex_command == "name":
        return command_codex_name(store, args.session_id, args.alias)
    if args.codex_command == "list":
        return command_codex_list(store, args.track)
    if args.codex_command == "status":
        return command_codex_status(store, args.track)
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


def command_codex_detach(store: Store, session_id: str) -> int:
    now = utc_now()

    def mutate(state: State) -> Session:
        session = find_session(state, "codex", session_id)
        if session is None:
            raise CliError(f"Codex session '{session_id}' was not found.")
        if session.track_id is None:
            raise CliError(f"Codex session '{session_id}' is not attached to any track.")

        track = resolve_track(state, session.track_id)
        session.track_id = None
        session.updated_at = now
        session.last_touched_at = now
        touch_track(track, now)
        return session

    session = store.update(mutate)
    print(f"{session.id}: detached")
    return 0


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
    sessions = attached_codex_sessions(state, track.id, session_metadata)
    if not sessions:
        print(f"No Codex sessions attached to {track.id}.")
        return 0
    display_metadata = session_display_metadata_map(sessions)
    session_status = session_display_status_map(sessions, display_metadata)
    rows = [
        [
            session.id,
            session.alias or "-",
            session_metadata.get(session.id, CodexSessionMetadata(session.id)).name or "-",
            session_status.get(session_ref_key(session), SessionDisplayStatus(session.provider, session.id)).state,
            session_activity_at(session, display_metadata),
        ]
        for session in sessions
    ]
    print(render_table(["Session", "Alias", "Name", "Status", "Activity"], rows))
    return 0


def command_codex_status(store: Store, track_ref: str | None) -> int:
    state = store.load()
    track = resolve_track_or_current(state, track_ref)
    session_metadata = codex_session_metadata_map(state.sessions)
    sessions = attached_codex_sessions(state, track.id, session_metadata)
    if not sessions:
        print(f"No Codex sessions attached to {track.id}.")
        return 0

    session_status = session_display_status_map(sessions)
    rows = [
        [
            session.id,
            session.alias or "-",
            session_metadata.get(session.id, CodexSessionMetadata(session.id)).name or "-",
            session_status.get(session_ref_key(session), SessionDisplayStatus(session.provider, session.id)).state,
            session_status.get(session_ref_key(session), SessionDisplayStatus(session.provider, session.id)).detail or "-",
            session_status.get(session_ref_key(session), SessionDisplayStatus(session.provider, session.id)).activity_at or "-",
        ]
        for session in sessions
    ]
    print(render_table(["Session", "Alias", "Name", "Status", "Detail", "Activity"], rows))
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
        if args.interactive_codex_command == "attach":
            return command_interactive_codex_attach(store)
        if args.interactive_codex_command == "detach":
            return command_interactive_codex_detach(store)
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
    if args.interactive_command == "remove":
        track_ref = pick_track(store, include_done=True)
        if track_ref is None:
            return 1
        return command_remove(store, track_ref, remove_worktree_flag=args.remove_worktree)

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
    sessions = attached_codex_sessions(state, track.id, session_metadata)
    if len(sessions) == 1:
        return command_codex_resume(store, track.id, sessions[0].id)
    display_metadata = session_display_metadata_map(sessions)
    session_options = [
        "\t".join(
            [
                session.id,
                session.alias or "-",
                session_metadata.get(session.id, CodexSessionMetadata(session.id)).name or "-",
                session_activity_at(session, display_metadata),
            ]
        )
        for session in sessions
    ]
    session_selection = run_fzf(session_options, prompt="session> ")
    if not session_selection:
        return 1
    session_ref = session_selection[0].split("\t", 1)[0]
    return command_codex_resume(store, track.id, session_ref)


def command_interactive_codex_attach(store: Store) -> int:
    state = store.load()
    target_track = current_track_for_cwd(state)
    if target_track is None:
        track_ref = pick_track(store, include_done=True)
        if track_ref is None:
            return 1
        state = store.load()
        target_track = resolve_track(state, track_ref)

    candidates = discover_unattached_codex_sessions(state)
    if not candidates:
        raise CliError("No unattached Codex sessions available.")

    options = [
        "\t".join(
            [
                session_id,
                alias or "-",
                name or "-",
                updated_at or "-",
            ]
        )
        for session_id, alias, name, updated_at in candidates
    ]
    selection = run_fzf(options, prompt="session> ")
    if not selection:
        return 1
    session_id = selection[0].split("\t", 1)[0]
    return command_codex_attach(store, target_track.id, session_id)


def command_interactive_codex_detach(store: Store) -> int:
    state = store.load()
    target_track = current_track_for_cwd(state)
    if target_track is not None:
        sessions = attached_codex_sessions(state, target_track.id)
    else:
        sessions = []

    if not sessions:
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
        target_track = resolve_track(state, track_ref)
        sessions = attached_codex_sessions(state, target_track.id, session_metadata)

    if not sessions:
        raise CliError(f"No Codex sessions attached to {target_track.id}.")

    session_metadata = codex_session_metadata_map(state.sessions)
    display_metadata = session_display_metadata_map(sessions)
    options = [
        "\t".join(
            [
                session.id,
                session.alias or "-",
                session_metadata.get(session.id, CodexSessionMetadata(session.id)).name or "-",
                session_activity_at(session, display_metadata),
            ]
        )
        for session in sessions
    ]
    selection = run_fzf(options, prompt="session> ")
    if not selection:
        return 1
    session_id = selection[0].split("\t", 1)[0]
    return command_codex_detach(store, session_id)


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


def resolve_next_target(store: Store, parts: list[str]) -> tuple[str, str]:
    if not parts:
        raise CliError("Usage: track next [<track>] <text>")

    state = store.load()
    explicit_track_ref = parts[0]
    explicit_text = " ".join(parts[1:])
    if explicit_text:
        try:
            track = resolve_track(state, explicit_track_ref)
        except CliError:
            pass
        else:
            return track.id, explicit_text

    current_track = resolve_track_or_current(state, None)
    return current_track.id, " ".join(parts)


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


def validate_track_display_name(state: State, track: Track, name: str) -> None:
    normalized = name.strip()
    if not normalized:
        raise CliError("Track name cannot be empty.")
    for other in state.tracks:
        if other.id == track.id:
            continue
        if other.name.casefold() == normalized.casefold():
            raise CliError(f"Track name '{normalized}' is already in use.")


def maybe_update_track_workspace(track: Track, workspace_path: Path | None, now: str) -> None:
    if workspace_path is None:
        return
    workspace_text = str(workspace_path)
    if track.workspace_path == workspace_text:
        return
    track.workspace_path = workspace_text
    touch_track(track, now)


def managed_workspace_path(store: Store, track: Track) -> Path | None:
    if not track.workspace_path:
        return None
    workspace_path = Path(track.workspace_path)
    managed_root = (store.paths.state_dir / "workspaces").resolve()
    try:
        workspace_path.resolve().relative_to(managed_root)
    except ValueError:
        return None
    return workspace_path


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


def session_ref_key(session: Session) -> tuple[str, str]:
    return session.provider, session.id


def attached_sessions_for_track(
    state: State,
    track_id: str,
    metadata: dict[tuple[str, str], SessionDisplayMetadata] | None = None,
) -> list[Session]:
    metadata = metadata or session_display_metadata_map(state.sessions)
    sessions = [session for session in state.sessions if session.track_id == track_id]
    return sorted(sessions, key=lambda item: session_sort_key(item, metadata), reverse=True)


def attached_codex_sessions(
    state: State,
    track_id: str,
    metadata: dict[str, CodexSessionMetadata] | None = None,
) -> list[Session]:
    sessions = [
        session
        for session in state.sessions
        if session.provider == "codex" and session.track_id == track_id
    ]
    display_metadata = session_display_metadata_map(sessions)
    return sorted(sessions, key=lambda item: session_sort_key(item, display_metadata), reverse=True)


def discover_unattached_codex_sessions(state: State) -> list[tuple[str, str | None, str | None, str | None]]:
    attached_ids = {
        session.id
        for session in state.sessions
        if session.provider == "codex" and session.track_id
    }
    metadata_by_id = read_codex_session_index()
    stored_unattached = {
        session.id: session
        for session in state.sessions
        if session.provider == "codex" and session.track_id is None
    }
    for session_id in stored_unattached:
        if session_id not in metadata_by_id:
            fallback = read_codex_session_metadata_from_rollout(session_id)
            if fallback is not None:
                metadata_by_id[session_id] = fallback

    candidates: dict[str, tuple[str | None, str | None, str | None]] = {}
    for session_id, session in stored_unattached.items():
        metadata = metadata_by_id.get(session_id, CodexSessionMetadata(session_id))
        candidates[session_id] = (session.alias, metadata.name, metadata.updated_at or session.updated_at)

    for session_id, metadata in metadata_by_id.items():
        if session_id in attached_ids:
            continue
        session = stored_unattached.get(session_id)
        alias = session.alias if session else None
        updated_at = metadata.updated_at or (session.updated_at if session else None)
        candidates.setdefault(session_id, (alias, metadata.name, updated_at))

    return sorted(
        [(session_id, alias, name, updated_at) for session_id, (alias, name, updated_at) in candidates.items()],
        key=lambda item: item[3] or "",
        reverse=True,
    )


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
    display_metadata = session_display_metadata_map(sessions)
    sessions.sort(key=lambda item: session_sort_key(item, display_metadata), reverse=True)
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


def session_rollups_by_track(
    sessions: list[Session],
    status_by_session: dict[tuple[str, str], SessionDisplayStatus],
) -> dict[str, str]:
    sessions_by_track: dict[str, list[Session]] = {}
    for session in sessions:
        if session.track_id is None:
            continue
        sessions_by_track.setdefault(session.track_id, []).append(session)
    return {
        track_id: session_rollup(track_sessions, status_by_session)
        for track_id, track_sessions in sessions_by_track.items()
    }


def session_rollup(
    sessions: list[Session],
    status_by_session: dict[tuple[str, str], SessionDisplayStatus],
) -> str:
    counts: dict[str, int] = {"running": 0, "waiting": 0, "idle": 0, "unknown": 0}
    for session in sessions:
        status = status_by_session.get(session_ref_key(session), SessionDisplayStatus(session.provider, session.id)).state
        counts[status if status in counts else "unknown"] += 1

    parts: list[str] = []
    labels = {
        "running": "run",
        "waiting": "wait",
        "idle": "idle",
        "unknown": "unk",
    }
    for state in ("running", "waiting", "idle", "unknown"):
        if counts[state]:
            parts.append(f"{labels[state]}:{counts[state]}")
    return " ".join(parts) if parts else "-"


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
    ensure_remove_worktree_allowed(track)


def ensure_remove_worktree_allowed(track: Track) -> None:
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


def resolve_workspace_for_track(
    store: Store,
    track_id: str,
    source_worktree_path: Path,
    target_worktree_path: Path,
    explicit_workspace: str | None,
) -> Path | None:
    workspace_path = normalize_optional_path(explicit_workspace)
    if workspace_path is not None:
        return workspace_path

    detected = detect_current_vscode_workspace(Path.cwd(), source_worktree_path)
    if detected and should_capture_workspace(detected):
        return write_workspace_snapshot(
            store,
            track_id,
            detected,
            source_worktree_path=source_worktree_path,
            target_worktree_path=target_worktree_path,
        )

    return autodetect_workspace(target_worktree_path)


def detect_current_vscode_workspace(current_cwd: Path, worktree_path: Path) -> DetectedWorkspace | None:
    if not os.environ.get("VSCODE_CLI"):
        return None

    workspace_storage_dir = Path.home() / ".config" / "Code" / "User" / "workspaceStorage"
    if not workspace_storage_dir.exists():
        return None

    candidates: list[tuple[int, float, DetectedWorkspace]] = []
    for storage_file in workspace_storage_dir.glob("*/workspace.json"):
        detected = read_workspace_storage_entry(storage_file)
        if detected is None:
            continue
        score = workspace_match_score(detected.folders, current_cwd, worktree_path)
        if score < 0:
            continue
        candidates.append((score, detected.modified_at, detected))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def read_workspace_storage_entry(storage_file: Path) -> DetectedWorkspace | None:
    try:
        data = json.loads(storage_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    folder_uri = data.get("folder")
    if isinstance(folder_uri, str):
        folder_path = file_uri_to_path(folder_uri)
        if folder_path is None:
            return None
        return DetectedWorkspace(
            source_path=None,
            document={"folders": [{"path": str(folder_path)}]},
            folders=[folder_path],
            modified_at=storage_file.stat().st_mtime,
        )

    workspace_uri = data.get("workspace")
    if not isinstance(workspace_uri, str):
        return None
    workspace_path = file_uri_to_path(workspace_uri)
    if workspace_path is None or not workspace_path.exists():
        return None

    document = read_workspace_document(workspace_path)
    if document is None:
        return None
    folders = extract_workspace_folders(document, workspace_path)
    if not folders:
        return None
    return DetectedWorkspace(
        source_path=workspace_path,
        document=document,
        folders=folders,
        modified_at=storage_file.stat().st_mtime,
    )


def should_capture_workspace(detected: DetectedWorkspace) -> bool:
    return detected.source_path is not None or len(detected.folders) > 1


def read_workspace_document(workspace_path: Path) -> dict[str, object] | None:
    try:
        raw = workspace_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def extract_workspace_folders(document: dict[str, object], workspace_path: Path | None) -> list[Path]:
    raw_folders = document.get("folders")
    if not isinstance(raw_folders, list):
        return []

    folders: list[Path] = []
    for item in raw_folders:
        if not isinstance(item, dict):
            continue
        resolved = resolve_workspace_folder_entry(item, workspace_path)
        if resolved is not None:
            folders.append(resolved)
    return folders


def resolve_workspace_folder_entry(entry: dict[str, object], workspace_path: Path | None) -> Path | None:
    path_value = entry.get("path")
    if isinstance(path_value, str) and path_value:
        folder_path = Path(path_value).expanduser()
        if not folder_path.is_absolute():
            if workspace_path is None:
                return None
            folder_path = (workspace_path.parent / folder_path).resolve()
        else:
            folder_path = folder_path.resolve()
        return folder_path

    uri_value = entry.get("uri")
    if isinstance(uri_value, str) and uri_value:
        return file_uri_to_path(uri_value)
    return None


def file_uri_to_path(value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":
        path = f"//{parsed.netloc}{path}"
    return Path(path).resolve()


def workspace_match_score(folders: list[Path], current_cwd: Path, worktree_path: Path) -> int:
    best = -1
    for folder in folders:
        if folder == worktree_path:
            return 4
        if path_contains(folder, current_cwd):
            best = max(best, 3)
        elif path_contains(folder, worktree_path):
            best = max(best, 2)
        elif path_contains(worktree_path, folder):
            best = max(best, 1)
    return best


def path_contains(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def write_workspace_snapshot(
    store: Store,
    track_id: str,
    detected: DetectedWorkspace,
    source_worktree_path: Path,
    target_worktree_path: Path,
) -> Path:
    workspace_dir = store.paths.state_dir / "workspaces"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    workspace_path = workspace_dir / f"{track_id}.code-workspace"
    document = rewrite_workspace_document(detected.document, detected.source_path, source_worktree_path, target_worktree_path)
    write_json_file(workspace_path, document)
    return workspace_path.resolve()


def rewrite_workspace_document(
    document: dict[str, object],
    source_workspace_path: Path | None,
    source_worktree_path: Path,
    target_worktree_path: Path,
) -> dict[str, object]:
    rewritten = json.loads(json.dumps(document))
    raw_folders = rewritten.get("folders")
    if not isinstance(raw_folders, list):
        rewritten["folders"] = []
        return rewritten

    folders: list[dict[str, object]] = []
    for item in raw_folders:
        if not isinstance(item, dict):
            continue
        resolved = resolve_workspace_folder_entry(item, source_workspace_path)
        if resolved is None:
            continue
        mapped = map_workspace_folder(resolved, source_worktree_path, target_worktree_path)
        folder_entry = {key: value for key, value in item.items() if key not in {"path", "uri"}}
        folder_entry["path"] = str(mapped)
        folders.append(folder_entry)
    rewritten["folders"] = folders
    return rewritten


def map_workspace_folder(folder_path: Path, source_worktree_path: Path, target_worktree_path: Path) -> Path:
    source_worktree_resolved = source_worktree_path.resolve()
    folder_resolved = folder_path.resolve()
    try:
        relative = folder_resolved.relative_to(source_worktree_resolved)
    except ValueError:
        return folder_resolved
    return (target_worktree_path.resolve() / relative).resolve()


def write_json_file(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{path.stem}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


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


def current_track_for_cwd(state: State) -> Track | None:
    try:
        context = current_context(Path.cwd())
    except GitError:
        return None
    return match_track_for_context(state, context)


def session_display_metadata_map(sessions: list[Session]) -> dict[tuple[str, str], SessionDisplayMetadata]:
    metadata_by_session: dict[tuple[str, str], SessionDisplayMetadata] = {}
    codex_sessions = [session for session in sessions if session.provider == "codex"]
    codex_metadata = codex_session_metadata_map(codex_sessions) if codex_sessions else {}

    for session in sessions:
        key = session_ref_key(session)
        if session.provider == "codex":
            metadata = codex_metadata.get(session.id)
            metadata_by_session[key] = SessionDisplayMetadata(
                provider=session.provider,
                session_id=session.id,
                name=metadata.name if metadata is not None else None,
                updated_at=(metadata.updated_at if metadata is not None else None) or session.updated_at,
            )
            continue
        metadata_by_session[key] = SessionDisplayMetadata(
            provider=session.provider,
            session_id=session.id,
            updated_at=session.updated_at,
        )
    return metadata_by_session


def session_display_status_map(
    sessions: list[Session],
    metadata_by_session: dict[tuple[str, str], SessionDisplayMetadata] | None = None,
) -> dict[tuple[str, str], SessionDisplayStatus]:
    metadata_by_session = metadata_by_session or session_display_metadata_map(sessions)
    statuses: dict[tuple[str, str], SessionDisplayStatus] = {}
    codex_sessions = [session for session in sessions if session.provider == "codex"]
    codex_status = codex_session_status_map(codex_sessions) if codex_sessions else {}

    for session in sessions:
        key = session_ref_key(session)
        if session.provider == "codex" and session.id in codex_status:
            status = codex_status[session.id]
            statuses[key] = SessionDisplayStatus(
                provider=session.provider,
                session_id=session.id,
                state=status.state,
                detail=status.detail,
                activity_at=status.activity_at or metadata_by_session.get(key, SessionDisplayMetadata(session.provider, session.id)).updated_at or session.updated_at,
            )
            continue
        statuses[key] = SessionDisplayStatus(
            provider=session.provider,
            session_id=session.id,
            state="unknown",
            activity_at=metadata_by_session.get(key, SessionDisplayMetadata(session.provider, session.id)).updated_at or session.updated_at,
        )
    return statuses


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


def codex_session_status_map(sessions: list[Session]) -> dict[str, CodexSessionStatus]:
    statuses: dict[str, CodexSessionStatus] = {}
    for session in sessions:
        if session.provider != "codex":
            continue
        statuses[session.id] = read_codex_session_status_from_rollout(session.id) or CodexSessionStatus(session.id, "unknown")
    return statuses


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


def read_codex_session_status_from_rollout(session_id: str) -> CodexSessionStatus | None:
    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.exists():
        return None

    candidates = sorted(sessions_root.rglob(f"rollout-*{session_id}.jsonl"), reverse=True)
    for path in candidates:
        status = read_codex_session_status_file(path, session_id)
        if status is not None:
            return status
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


def read_codex_session_status_file(path: Path, expected_session_id: str) -> CodexSessionStatus | None:
    discovered_id: str | None = None
    last_timestamp: str | None = None
    saw_non_meta = False
    task_active = False
    pending_calls: dict[str, str] = {}
    explicit_wait_call_id: str | None = None
    last_assistant_question_at: str | None = None
    last_user_message_at: str | None = None
    last_completed_at: str | None = None

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

                timestamp = optional_text(record.get("timestamp"))
                if timestamp is not None:
                    last_timestamp = timestamp

                record_type = record.get("type")
                payload = record.get("payload")
                if record_type == "session_meta":
                    if not isinstance(payload, dict):
                        continue
                    session_id = optional_text(payload.get("id"))
                    if session_id != expected_session_id:
                        continue
                    discovered_id = session_id
                    continue

                if discovered_id is None:
                    continue

                if record_type == "event_msg":
                    if not isinstance(payload, dict):
                        continue
                    event_type = payload.get("type")
                    if event_type == "task_started":
                        task_active = True
                        pending_calls = {}
                        explicit_wait_call_id = None
                        last_assistant_question_at = None
                        last_user_message_at = None
                        saw_non_meta = True
                        continue
                    if event_type == "task_complete":
                        task_active = False
                        pending_calls = {}
                        explicit_wait_call_id = None
                        last_completed_at = timestamp or last_completed_at
                        saw_non_meta = True
                        continue
                    if event_type in {"thread_rolled_back", "turn_aborted"}:
                        task_active = False
                        pending_calls = {}
                        explicit_wait_call_id = None
                        saw_non_meta = True
                        continue
                    if event_type == "user_message":
                        last_user_message_at = timestamp or last_user_message_at
                        saw_non_meta = True
                        continue
                    continue

                if record_type != "response_item" or not isinstance(payload, dict):
                    continue

                item_type = payload.get("type")
                if item_type in {"function_call", "custom_tool_call"}:
                    call_id = optional_text(payload.get("call_id"))
                    name = optional_text(payload.get("name")) or item_type
                    if call_id:
                        pending_calls[call_id] = name
                        if name == "request_user_input":
                            explicit_wait_call_id = call_id
                    saw_non_meta = True
                    continue

                if item_type in {"function_call_output", "custom_tool_call_output"}:
                    call_id = optional_text(payload.get("call_id"))
                    if call_id:
                        if explicit_wait_call_id == call_id:
                            explicit_wait_call_id = None
                        pending_calls.pop(call_id, None)
                    saw_non_meta = True
                    continue

                if item_type == "message":
                    saw_non_meta = True
                    role = optional_text(payload.get("role"))
                    if role == "assistant":
                        message_text = extract_message_text(payload)
                        if message_text and looks_like_question(message_text):
                            last_assistant_question_at = timestamp or last_assistant_question_at
                        else:
                            last_assistant_question_at = None
                    elif role == "user":
                        last_user_message_at = timestamp or last_user_message_at

    except OSError:
        return None

    if discovered_id is None:
        return None
    if explicit_wait_call_id is not None:
        return CodexSessionStatus(discovered_id, "waiting", "user input requested", last_timestamp)
    if task_active and last_assistant_question_at is not None and (
        last_user_message_at is None or last_user_message_at < last_assistant_question_at
    ):
        return CodexSessionStatus(discovered_id, "waiting", "assistant asked a question", last_timestamp)
    if pending_calls:
        pending_name = next(iter(pending_calls.values()))
        return CodexSessionStatus(discovered_id, "running", f"tool call in progress: {pending_name}", last_timestamp)
    if task_active:
        return CodexSessionStatus(discovered_id, "running", "task in progress", last_timestamp)
    if saw_non_meta or last_completed_at is not None:
        return CodexSessionStatus(discovered_id, "idle", "no active task", last_timestamp)
    return CodexSessionStatus(discovered_id, "unknown", None, last_timestamp)


def session_activity_at(
    session: Session,
    metadata_by_session: dict[tuple[str, str], SessionDisplayMetadata],
) -> str:
    return metadata_by_session.get(session_ref_key(session), SessionDisplayMetadata(session.provider, session.id)).updated_at or session.updated_at


def session_sort_key(
    session: Session,
    metadata_by_session: dict[tuple[str, str], SessionDisplayMetadata],
) -> str:
    return session_activity_at(session, metadata_by_session)


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


def extract_message_text(payload: dict[str, object]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = optional_text(item.get("text"))
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def looks_like_question(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return stripped.endswith("?")


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
