---
name: track-workflow
description: Use when the user wants Codex to interact with development tracks managed by the `track` CLI: inspect the current track, create or rename tracks, create child worktree tracks, open or resume tracks, edit metadata, attach Codex sessions, or clean up done tracks.
---

# Track Workflow

Use this skill when the user wants help operating the local `track` CLI.
Prefer raw `track` commands. Use `track i ...` only when the user explicitly wants interactive selection.

## Core Rules

- The top-level concept is the track, not the Codex session.
- New child tracks should start with no attached Codex session.
- Do not use cleanup to mark work done. The flow is `track done` first, then `track cleanup`.
- `track rename` changes only the display name. It does not rename the track ID, branch, or worktree path.

## Fast Start

1. Find the current track:
   - `track here`
2. If the current worktree is not tracked yet:
   - `track init-here`
   - or `track init-here "<display name>"`
3. To inspect attached sessions:
   - `track codex list`
   - `track sessions`

## Current Worktree Capture

Use these when the user is already in the right repo/worktree.

- Capture the current worktree as a track:
  - `track init-here`
  - `track init-here "<display name>"`
- Attach the current Codex session to the current track:
  - `track codex attach-current`
- Update the track state:
  - `track next <track> "<next step>"`
  - `track next "<next step>"` for the current track
  - `track note <track> "<note>"`
  - `track note edit <track>`

## Create A New Child Track

Use this when the user says things like "create a new track for this task".

- Create a child track from the current tracked worktree:
  - `track new "<display name>"`
  - optional brief: `track new "<display name>" --purpose "<short purpose>"`
  - optional base override: `track new "<display name>" --base <ref>`
- This creates:
  - a new branch `p/bcg/<slug>`
  - a sibling git worktree
  - a new track record with `parent_track_id`
- If run from a VS Code multi-root workspace, it captures the current workspace layout for the new track and rewrites the current worktree folder to the new child worktree path, so `track open <track-id>` reopens the same directory set against the child track.
- It does **not** attach the current Codex session to the new track.
- Open the new track after creation:
  - `track open <track-id>`

Use `track new "<display name>" --here` only when the user explicitly wants to create a track for the current worktree instead of creating a new worktree.

## Pause And Wake

- Pause a track:
  - `track pause <track>`
- Find paused tracks:
  - `track paused`
- Wake a paused track:
  - `track wake <track>`
- Interactive wake:
  - `track i wake`

`track park` and `track activate` still work, but prefer `pause` and `wake` in normal use.

## Resume

- Resume a specific track:
  - `track resume <track>`
- Resume by picking from active, waiting, or parked tracks:
  - `track resume`

Resume behavior:

- opens the track in a new VS Code window
- marks the track `active`
- shows the track summary and next step
- shows which Codex session to reopen manually in the VS Code extension

## Cleanup

Cleanup is for finished tracks. It preserves metadata and attached sessions.

1. Mark the track done:
   - `track done <track>`
2. Record cleanup:
   - `track cleanup <track>`
3. Optionally remove the linked worktree from disk:
   - `track cleanup <track> --remove-worktree`

Important cleanup behavior:

- Cleanup requires the track to already be `done`.
- `--remove-worktree` removes only linked worktrees.
- It must not remove the main checkout.
- It keeps the branch.
- After worktree removal, `track open <track>` will fail until a worktree exists again.

## Rename

- Rename the track display name:
  - `track rename <track> "<new display name>"`

This keeps:
- track ID
- branch name
- worktree path

## Metadata Editing

Use these to adjust safe track metadata after creation.

- Set or clear purpose:
  - `track purpose <track> "<purpose>"`
  - `track purpose <track> --clear`
- Set or clear workspace path:
  - `track workspace <track> /abs/path/to/file.code-workspace`
  - `track workspace <track> --clear`
- Set or clear parent track:
  - `track parent <track> <parent-track>`
  - `track parent <track> --clear`

Do not directly edit repo path, worktree path, or branch through metadata commands. Those are structural fields tied to git state.

## Remove

- Remove a track record only:
  - `track remove <track>`
- Remove the track record and its linked child worktree:
  - `track remove <track> --remove-worktree`
- Interactive remove:
  - `track i remove`
  - `track i remove --remove-worktree`

Remove behavior:

- attached Codex sessions become unlabeled instead of being deleted
- child tracks that pointed at the removed track lose their `parent_track_id`
- managed workspace snapshots for the removed track are deleted
- `--remove-worktree` keeps the git branch
- `--remove-worktree` must not remove the main checkout

## Codex Session Commands

- Attach the current session to the current track:
  - `track codex attach-current`
- Attach a known session ID to a track:
  - `track codex attach <track> <session-id>`
- Detach a wrongly attached session by ID:
  - `track codex detach <session-id>`
- Name a session:
  - `track codex name <session-id> "<alias>"`
- List sessions on a track:
  - `track codex list <track>`
  - `track codex list` for the current track
- Show inferred live status for attached sessions:
  - `track codex status <track>`
  - `track codex status` for the current track
- Find unattached discoverable sessions:
  - `track codex unlabeled`
- Interactive attach/detach:
  - `track i codex attach`
  - `track i codex detach`

## Global Session Overview

- Show all attached sessions grouped by track:
  - `track sessions`
- This view is provider-agnostic.
- Live status is provider-aware when implemented.
  - Codex currently supports inferred `running`, `waiting`, `idle`, and `unknown`.
  - Other providers show `unknown` until support is added.

## Useful Inspection Commands

- `track list`
- `track list --all`
- `track sessions`
- `track show <track>`
- `track paused`
- `track resume`
- `track prompt`
- `track prompt --status`
- `track scan`
- `track open`
- `track i open`
- `track completion bash`
- `track i cleanup --remove-worktree`
