# Track Coordinator CLI V1

## Summary

Build a Python/uv CLI named `track` in `/home/bcg/GIT/track-coordinator`.
The tool manages track-first metadata stored on the Ubuntu workstation under
the user's home directory. Codex remains the owner of actual conversation
history and session persistence; this tool stores only organization metadata
around opaque session IDs.

Use:

- Python 3.10+ with `argparse` and stdlib JSON.
- `uv` project packaging with a console script entry point.
- `pytest` tests.
- End-to-end CLI tests using temp homes, temp git repos, and fake `code`,
  `fzf`, and `codex` binaries.

## Key Changes

- Store metadata in one inspectable JSON file.
  - Default path: `$XDG_STATE_HOME/track-coordinator/tracks.json`.
  - Fallback path: `~/.local/state/track-coordinator/tracks.json`.
  - Override path root: `TRACK_COORDINATOR_HOME`.
  - Include `schema_version`, `tracks`, and provider-neutral `sessions`.
  - Protect writes with a lock file and atomic replace.
  - Use POSIX locking on Ubuntu and Windows-compatible locking where possible.

- Track model:
  - Stable lowercase slug ID generated from the track name.
  - Separate human-readable display name.
  - Status values: `active`, `waiting`, `parked`, `done`.
  - Repo path, worktree path, branch name, optional VS Code workspace path.
  - Next step, notes, created/updated/last-touched timestamps.
  - Provider-neutral session records with provider, ID, alias, track ID, and
    timestamps.
  - One session has one primary track; attaching it to a different track moves
    ownership.

- Core raw commands:
  - `track list` shows active, waiting, and parked tracks by default.
  - `track list --all` includes done tracks.
  - `track show <track>`.
  - `track here` detects the current git repo/worktree/branch and shows the
    matching track. If none exists, it prints exact create/attach commands.
  - `track new <name>` binds the current repo/worktree/branch.
  - `track new <name> --worktree` creates a sibling worktree at
    `/home/bcg/GIT/<repo>-<slug>` from the repo default branch and branch
    `p/bcg/<slug>`.
  - `track open <track>` runs `code -r <workspace-or-worktree>` and prints the
    command if launching fails.
  - `track park <track>`, `track wait <track>`, `track activate <track>`, and
    `track done <track>`.
  - `track next <track> <text>`.
  - `track note <track> <text> --append`.
  - `track note edit <track>` opens an editor-backed note.

- Interactive fzf command family:
  - Use the consistent prefix `track i ...`.
  - Include `track i open`, `track i park`, `track i done`, `track i show`,
    `track i scan`, and `track i codex resume`.
  - Raw equivalents always exist and never require fzf.

- Codex-aware commands:
  - `track codex attach <track> <session-id>`.
  - `track codex name <session-id> "Readable name"`.
  - `track codex list <track>`.
  - `track codex resume <track> [session-id-or-alias]` runs
    `codex resume <id> -C <worktree>`.
  - `track codex unlabeled` best-effort reads `~/.codex/session_index.jsonl`
    for IDs not attached to any track, without reading or storing conversation
    history.

- Bootstrap/import:
  - `track scan` lists untracked worktrees from the current repo's
    `git worktree list`.
  - `track i scan` lets the user pick worktrees with fzf and create tracks for
    them.
  - Do not add a global `/home/bcg/GIT` scan in v1.

## Test Plan

- Unit tests:
  - Slug generation, including Jira-style names like
    `XR5ML-482 Add Left Right` becoming `xr5ml-482-add-left-right`.
  - JSON load/save, schema defaults, lock behavior, and atomic writes.
  - Track lookup by slug, name, and unambiguous prefix.
  - Session attach, move, alias, list, and unlabeled detection.
  - Git context parsing from mocked or temp git repos.
  - Workspace fallback from `.code-workspace` to worktree directory.

- End-to-end tests:
  - Create temp HOME and temp git repo with commits/worktrees.
  - Run `uv run track new`, `list`, `here`, `open`, `park`, and `done`.
  - Use a fake `code` executable to verify the exact open command.
  - Use a fake `codex` executable to verify
    `codex resume <id> -C <worktree>`.
  - Use a fake `fzf` executable to verify `track i ...` commands without
    manual input.
  - Use a temp `~/.codex/session_index.jsonl` to test
    `track codex unlabeled`.

## Assumptions

- V1 runs primarily on the Ubuntu workstation. Windows usefulness comes from
  Remote SSH hitting the same workstation storage.
- `track open` uses the workstation `code` CLI and falls back to printing the
  exact command on failure.
- Worktree creation defaults to the repo default branch and branch pattern
  `p/bcg/<slug>`.
- `.code-workspace` files are optional. V1 opens the worktree directory when no
  workspace file is recorded.
- Codex internals are not required for core functionality.
  `~/.codex/session_index.jsonl` is used only for best-effort unlabeled-session
  discovery.
