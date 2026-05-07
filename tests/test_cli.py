from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]


def run_cli(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    merged_env = dict(env)
    merged_env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "track_coordinator", *args],
        cwd=cwd,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)


def git_output(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)
    return completed.stdout.strip()


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def write_workspace_storage_entry(home: Path, storage_id: str, payload: dict[str, object]) -> None:
    storage_dir = home / ".config" / "Code" / "User" / "workspaceStorage" / storage_id
    storage_dir.mkdir(parents=True)
    (storage_dir / "workspace.json").write_text(json.dumps(payload), encoding="utf-8")


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


def test_track_lifecycle_and_codex_commands(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "commands.log"
    make_executable(
        fake_bin / "code",
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            printf "code:%s\\n" "$*" >> "{log_path}"
            exit 0
            """
        ),
    )
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{fake_bin}:{Path('/usr/bin')}:{Path('/bin')}",
    }
    session_index = home / ".codex" / "session_index.jsonl"
    session_index.parent.mkdir(parents=True)
    session_index.write_text(
        '{"id":"session-self","thread_name":"Build track workflow tool","updated_at":"2026-05-07T09:30:00Z"}\n'
        '{"id":"session-123","thread_name":"Review branch changes","updated_at":"2026-05-07T09:15:00Z"}\n',
        encoding="utf-8",
    )

    result = run_cli(["new", "XR5ML-482-marlin-fixes", "--here"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "xr5ml-482-marlin-fixes" in result.stdout

    self_env = dict(env)
    self_env["CODEX_THREAD_ID"] = "session-self"
    result = run_cli(["codex", "attach-current"], repo, self_env)
    assert result.returncode == 0, result.stderr
    assert "session-self" in result.stdout

    result = run_cli(["next", "xr5ml-482-marlin-fixes", "Update", "the", "tests"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["codex", "attach", "xr5ml-482-marlin-fixes", "session-123"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["codex", "name", "session-123", "Review work"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["list"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "xr5ml-482-marlin-fixes" in result.stdout
    assert "session-123" not in result.stdout
    assert "Update the tests" in result.stdout

    result = run_cli(["codex", "list", "xr5ml-482-marlin-fixes"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "session-self" in result.stdout
    assert "session-123" in result.stdout
    assert "Review work" in result.stdout
    assert "Build track workflow tool" in result.stdout
    assert "Review branch changes" in result.stdout

    result = run_cli(["codex", "list"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "session-self" in result.stdout
    assert "session-123" in result.stdout
    assert "Build track workflow tool" in result.stdout

    result = run_cli(["open", "xr5ml-482-marlin-fixes"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["prompt"], repo, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[xr5ml-482-marlin-fixes]"

    result = run_cli(["prompt", "--status"], repo, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[xr5ml-482-marlin-fixes|active]"

    result = run_cli(["codex", "resume", "xr5ml-482-marlin-fixes"], repo, env)
    assert result.returncode == 1
    assert "VS Code extension resume is not supported yet." in result.stderr
    assert "Build track workflow tool" in result.stderr
    assert "track open xr5ml-482-marlin-fixes" in result.stderr

    result = run_cli(["codex", "resume", "xr5ml-482-marlin-fixes", "Build track workflow tool"], repo, env)
    assert result.returncode == 1
    assert "VS Code extension resume is not supported yet." in result.stderr
    assert "Build track workflow tool" in result.stderr

    log_text = log_path.read_text(encoding="utf-8")
    assert "code:-n" in log_text

    result = run_cli(["done", "xr5ml-482-marlin-fixes"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["prompt", "--status"], repo, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[xr5ml-482-marlin-fixes|done]"

    result = run_cli(["list"], repo, env)
    assert "xr5ml-482-marlin-fixes" not in result.stdout

    result = run_cli(["list", "--all"], repo, env)
    assert "xr5ml-482-marlin-fixes" in result.stdout


def test_new_creates_child_worktree_and_scan_here_and_unlabeled(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    make_executable(
        fake_bin / "fzf",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys

            lines = [line.rstrip("\\n") for line in sys.stdin if line.strip()]
            if not lines:
                raise SystemExit(1)
            print(lines[0])
            """
        ),
    )
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{fake_bin}:{Path('/usr/bin')}:{Path('/bin')}",
    }

    result = run_cli(["new", "root-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    (repo / "child-base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "child-base.txt")
    git(repo, "commit", "-m", "child base")
    parent_head = git_output(repo, "rev-parse", "HEAD")

    result = run_cli(["new", "feature-x", "--purpose", "Break out feature work"], repo, env)
    assert result.returncode == 0, result.stderr
    expected_worktree = tmp_path / "repo-feature-x"
    assert expected_worktree.exists()
    assert "Parent: root-track" in result.stdout
    assert "Purpose: Break out feature work" in result.stdout
    assert git_output(expected_worktree, "rev-parse", "HEAD") == parent_head

    result = run_cli(["codex", "list", "feature-x"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "No Codex sessions attached to feature-x." in result.stdout

    extra_worktree = tmp_path / "repo-scan-target"
    git(repo, "worktree", "add", "-b", "p/bcg/scan-target", str(extra_worktree), "HEAD")

    result = run_cli(["scan"], repo, env)
    assert result.returncode == 0, result.stderr
    assert str(extra_worktree) in result.stdout

    result = run_cli(["i", "scan"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "scan-target" in result.stdout

    result = run_cli(["here"], extra_worktree, env)
    assert result.returncode == 0, result.stderr
    assert "scan-target" in result.stdout

    session_index = home / ".codex" / "session_index.jsonl"
    session_index.parent.mkdir(parents=True)
    session_index.write_text(
        '{"id":"session-a","thread_name":"One","updated_at":"2026-05-07T08:00:00Z"}\n'
        '{"id":"session-b","thread_name":"Two","updated_at":"2026-05-07T09:00:00Z"}\n',
        encoding="utf-8",
    )

    result = run_cli(["codex", "attach", "feature-x", "session-a"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["codex", "unlabeled"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "session-b" in result.stdout
    assert "session-a" not in result.stdout


def test_prompt_is_silent_outside_tracked_git_context(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
    }

    outside = tmp_path / "outside"
    outside.mkdir()

    result = run_cli(["prompt"], outside, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_attach_current_falls_back_to_session_metadata(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
    }

    result = run_cli(["new", "metadata-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    sessions_dir = home / ".codex" / "sessions" / "2026" / "05" / "07"
    sessions_dir.mkdir(parents=True)
    old_session = sessions_dir / "rollout-2026-05-07T08-00-00-old-session.jsonl"
    old_session.write_text(
        '{"timestamp":"2026-05-07T08:00:00.000Z","type":"session_meta","payload":{"id":"old-session","cwd":"/tmp/elsewhere"}}\n',
        encoding="utf-8",
    )
    current_session = sessions_dir / "rollout-2026-05-07T09-00-00-current-session.jsonl"
    current_session.write_text(
        f'{{"timestamp":"2026-05-07T09:00:00.000Z","type":"session_meta","payload":{{"id":"current-session","cwd":"{repo}"}}}}\n'
        '{"timestamp":"2026-05-07T09:00:05.000Z","type":"event_msg","payload":{"type":"thread_name_updated","thread_id":"current-session","thread_name":"Resume current work"}}\n',
        encoding="utf-8",
    )

    result = run_cli(["codex", "attach-current"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "current-session" in result.stdout

    result = run_cli(["codex", "list", "metadata-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "current-session" in result.stdout
    assert "Resume current work" in result.stdout


def test_init_here_discovers_session_from_recent_activity_path(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
    }

    sessions_dir = home / ".codex" / "sessions" / "2026" / "05" / "07"
    sessions_dir.mkdir(parents=True)
    current_session = sessions_dir / "rollout-2026-05-07T09-00-00-current-session.jsonl"
    current_session.write_text(
        '{"timestamp":"2026-05-07T09:00:00.000Z","type":"session_meta","payload":{"id":"current-session","cwd":"/tmp/elsewhere"}}\n'
        f'{{"timestamp":"2026-05-07T09:00:03.000Z","type":"turn_context","payload":{{"cwd":"{repo}"}}}}\n'
        '{"timestamp":"2026-05-07T09:00:05.000Z","type":"event_msg","payload":{"type":"thread_name_updated","thread_id":"current-session","thread_name":"Resume current work"}}\n',
        encoding="utf-8",
    )

    result = run_cli(["init-here", "metadata-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "metadata-track" in result.stdout
    assert "Attached current Codex session: current-session" in result.stdout
    assert "Resume current work" in result.stdout

    result = run_cli(["codex", "list", "metadata-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "current-session" in result.stdout
    assert "Resume current work" in result.stdout


def test_new_requires_existing_track_for_child_creation(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
    }

    result = run_cli(["new", "child-track"], repo, env)
    assert result.returncode == 1
    assert "Use 'track init-here' or 'track new <name> --here' first." in result.stderr


def test_init_here_creates_current_track_and_attaches_session(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    workspace = repo / "track-coordinator.code-workspace"
    workspace.write_text("{}", encoding="utf-8")
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
    }
    session_index = home / ".codex" / "session_index.jsonl"
    session_index.parent.mkdir(parents=True)
    session_index.write_text(
        '{"id":"session-self","thread_name":"Bootstrap current work","updated_at":"2026-05-07T10:00:00Z"}\n',
        encoding="utf-8",
    )

    result = run_cli(["here"], repo, env)
    assert result.returncode == 1
    assert 'track init-here "repo"' in result.stdout

    self_env = dict(env)
    self_env["CODEX_THREAD_ID"] = "session-self"
    result = run_cli(["init-here", "Current Work Track"], repo, self_env)
    assert result.returncode == 0, result.stderr
    assert "Track: Current Work Track (current-work-track)" in result.stdout
    assert f"Workspace: {workspace}" in result.stdout
    assert "session-self" in result.stdout
    assert "Bootstrap current work" in result.stdout
    assert "Initialized current worktree." in result.stdout
    assert "Attached current Codex session: session-self" in result.stdout

    result = run_cli(["init-here"], repo, self_env)
    assert result.returncode == 0, result.stderr
    assert "Track: Current Work Track (current-work-track)" in result.stdout
    assert "Initialized current worktree." not in result.stdout
    assert "Attached current Codex session: session-self" in result.stdout

    result = run_cli(["codex", "list", "current-work-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("session-self") == 1


def test_new_here_attaches_current_session(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
    }

    session_index = home / ".codex" / "session_index.jsonl"
    session_index.parent.mkdir(parents=True)
    session_index.write_text(
        '{"id":"session-self","thread_name":"Current work session","updated_at":"2026-05-07T10:00:00Z"}\n',
        encoding="utf-8",
    )

    self_env = dict(env)
    self_env["CODEX_THREAD_ID"] = "session-self"
    result = run_cli(["new", "Current Work Track", "--here"], repo, self_env)
    assert result.returncode == 0, result.stderr
    assert "Track: Current Work Track (current-work-track)" in result.stdout
    assert "Attached current Codex session: session-self" in result.stdout

    result = run_cli(["codex", "list", "current-work-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "session-self" in result.stdout
    assert "Current work session" in result.stdout


def test_init_here_captures_current_multi_root_workspace(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    extra_dir = tmp_path / "shared-tools"
    extra_dir.mkdir()
    workspace_dir = home / "workspaces"
    workspace_dir.mkdir()
    source_workspace = workspace_dir / "current-window.code-workspace"
    source_workspace.write_text(
        json.dumps(
            {
                "folders": [
                    {"path": str(repo)},
                    {"path": str(extra_dir), "name": "shared-tools"},
                ],
                "settings": {"window.title": "captured"},
            }
        ),
        encoding="utf-8",
    )
    write_workspace_storage_entry(
        home,
        "current-window",
        {"workspace": source_workspace.as_uri()},
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "commands.log"
    make_executable(
        fake_bin / "code",
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            printf "code:%s\\n" "$*" >> "{log_path}"
            exit 0
            """
        ),
    )
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{fake_bin}:{Path('/usr/bin')}:{Path('/bin')}",
        "VSCODE_CLI": "1",
    }

    result = run_cli(["init-here", "workspace-track"], repo, env)
    assert result.returncode == 0, result.stderr
    managed_workspace = home / "state" / "workspaces" / "workspace-track.code-workspace"
    assert f"Workspace: {managed_workspace}" in result.stdout

    captured = json.loads(managed_workspace.read_text(encoding="utf-8"))
    assert captured["settings"]["window.title"] == "captured"
    assert captured["folders"] == [
        {"path": str(repo)},
        {"path": str(extra_dir), "name": "shared-tools"},
    ]

    result = run_cli(["open", "workspace-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert str(managed_workspace) in log_path.read_text(encoding="utf-8")


def test_new_captures_current_multi_root_workspace_for_child_track(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    extra_dir = tmp_path / "shared-tools"
    extra_dir.mkdir()
    workspace_dir = home / "workspaces"
    workspace_dir.mkdir()
    source_workspace = workspace_dir / "current-window.code-workspace"
    source_workspace.write_text(
        json.dumps(
            {
                "folders": [
                    {"path": str(repo)},
                    {"path": str(repo / "docs"), "name": "docs"},
                    {"path": str(extra_dir), "name": "shared-tools"},
                ],
                "settings": {"files.trimTrailingWhitespace": True},
            }
        ),
        encoding="utf-8",
    )
    write_workspace_storage_entry(
        home,
        "current-window",
        {"workspace": source_workspace.as_uri()},
    )
    (repo / "docs").mkdir()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "commands.log"
    make_executable(
        fake_bin / "code",
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            printf "code:%s\\n" "$*" >> "{log_path}"
            exit 0
            """
        ),
    )
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{fake_bin}:{Path('/usr/bin')}:{Path('/bin')}",
        "VSCODE_CLI": "1",
    }

    result = run_cli(["new", "root-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["new", "child-track"], repo, env)
    assert result.returncode == 0, result.stderr
    child_worktree = tmp_path / "repo-child-track"
    managed_workspace = home / "state" / "workspaces" / "child-track.code-workspace"
    assert f"Workspace: {managed_workspace}" in result.stdout

    captured = json.loads(managed_workspace.read_text(encoding="utf-8"))
    assert captured["settings"]["files.trimTrailingWhitespace"] is True
    assert captured["folders"] == [
        {"path": str(child_worktree)},
        {"path": str(child_worktree / "docs"), "name": "docs"},
        {"path": str(extra_dir), "name": "shared-tools"},
    ]

    result = run_cli(["open", "child-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert str(managed_workspace) in log_path.read_text(encoding="utf-8")


def test_note_edit_and_interactive_open_alias(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "commands.log"
    make_executable(
        fake_bin / "code",
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            printf "code:%s\\n" "$*" >> "{log_path}"
            exit 0
            """
        ),
    )
    make_executable(
        fake_bin / "fzf",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys

            lines = [line.rstrip("\\n") for line in sys.stdin if line.strip()]
            if not lines:
                raise SystemExit(1)
            print(lines[0])
            """
        ),
    )
    editor_script = fake_bin / "editor"
    make_executable(
        editor_script,
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import pathlib
            import sys

            path = pathlib.Path(sys.argv[-1])
            path.write_text("edited note\\n", encoding="utf-8")
            """
        ),
    )
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{fake_bin}:{Path('/usr/bin')}:{Path('/bin')}",
        "EDITOR": str(editor_script),
    }

    result = run_cli(["new", "interactive-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["note", "edit", "interactive-track"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["show", "interactive-track"], repo, env)
    assert "edited note" in result.stdout

    result = run_cli(["i", "open"], repo, env)
    assert result.returncode == 0, result.stderr
    log_text = log_path.read_text(encoding="utf-8")
    assert "code:-n" in log_text

    result = run_cli(["open"], repo, env)
    assert result.returncode == 0, result.stderr
    log_text = log_path.read_text(encoding="utf-8")
    assert log_text.count("code:-n") >= 2


def test_pause_paused_wait_and_interactive_wake(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    make_executable(
        fake_bin / "fzf",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys

            lines = [line.rstrip("\\n") for line in sys.stdin if line.strip()]
            if not lines:
                raise SystemExit(1)
            print(lines[0])
            """
        ),
    )
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{fake_bin}:{Path('/usr/bin')}:{Path('/bin')}",
    }

    result = run_cli(["new", "pause-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["wait", "pause-track"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["paused"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "pause-track" not in result.stdout

    result = run_cli(["pause", "pause-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "pause-track: parked" in result.stdout

    result = run_cli(["paused"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "pause-track" in result.stdout

    result = run_cli(["i", "wake"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "pause-track: active" in result.stdout

    result = run_cli(["paused"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "pause-track" not in result.stdout


def test_cleanup_and_interactive_cleanup_remove_worktree(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "commands.log"
    make_executable(
        fake_bin / "code",
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            printf "code:%s\\n" "$*" >> "{log_path}"
            exit 0
            """
        ),
    )
    make_executable(
        fake_bin / "fzf",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys

            lines = [line.rstrip("\\n") for line in sys.stdin if line.strip()]
            if not lines:
                raise SystemExit(1)
            print(lines[0])
            """
        ),
    )
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{fake_bin}:{Path('/usr/bin')}:{Path('/bin')}",
    }

    result = run_cli(["new", "root-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["new", "cleanup-track", "--purpose", "Cleanup validation"], repo, env)
    assert result.returncode == 0, result.stderr
    cleanup_worktree = tmp_path / "repo-cleanup-track"
    assert cleanup_worktree.exists()

    session_index = home / ".codex" / "session_index.jsonl"
    session_index.parent.mkdir(parents=True)
    session_index.write_text(
        '{"id":"session-a","thread_name":"Cleanup Session","updated_at":"2026-05-07T10:00:00Z"}\n',
        encoding="utf-8",
    )

    result = run_cli(["codex", "attach", "cleanup-track", "session-a"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["done", "cleanup-track"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["cleanup", "cleanup-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "cleanup-track: cleaned" in result.stdout

    result = run_cli(["show", "cleanup-track"], repo, env)
    assert "Cleaned:" in result.stdout
    assert "Worktree removed:" not in result.stdout

    result = run_cli(["codex", "list", "cleanup-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "session-a" in result.stdout

    result = run_cli(["open", "cleanup-track"], repo, env)
    assert result.returncode == 0, result.stderr
    log_text = log_path.read_text(encoding="utf-8")
    assert "code:-n" in log_text

    result = run_cli(["i", "cleanup", "--remove-worktree"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "cleanup-track: cleaned" in result.stdout
    assert "cleanup-track: worktree removed" in result.stdout
    assert not cleanup_worktree.exists()
    assert "p/bcg/cleanup-track" in git_output(repo, "branch", "--list", "p/bcg/cleanup-track")

    result = run_cli(["show", "cleanup-track"], repo, env)
    assert "Worktree removed:" in result.stdout

    result = run_cli(["codex", "list", "cleanup-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "session-a" in result.stdout

    result = run_cli(["open", "cleanup-track"], repo, env)
    assert result.returncode == 1
    assert "Worktree for track 'cleanup-track' no longer exists" in result.stderr


def test_cleanup_refuses_main_checkout_and_is_idempotent_when_missing(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
    }

    result = run_cli(["new", "root-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["done", "root-track"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["cleanup", "root-track", "--remove-worktree"], repo, env)
    assert result.returncode == 1
    assert "Cannot remove the main checkout for track 'root-track'." in result.stderr

    result = run_cli(["new", "child-track"], repo, env)
    assert result.returncode == 0, result.stderr
    child_worktree = tmp_path / "repo-child-track"
    assert child_worktree.exists()

    result = run_cli(["done", "child-track"], repo, env)
    assert result.returncode == 0, result.stderr

    git(repo, "worktree", "remove", str(child_worktree))
    assert not child_worktree.exists()

    result = run_cli(["cleanup", "child-track", "--remove-worktree"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "child-track: worktree removed" in result.stdout

    result = run_cli(["show", "child-track"], repo, env)
    assert "Worktree removed:" in result.stdout


def test_rename_updates_display_name_only_and_rejects_duplicates(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
    }

    result = run_cli(["new", "first-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["rename", "first-track", "Readable First Track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "first-track: Readable First Track" in result.stdout

    result = run_cli(["show", "first-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "Track: Readable First Track (first-track)" in result.stdout
    assert "Branch: master" in result.stdout
    assert f"Worktree: {repo}" in result.stdout

    other_root = tmp_path / "other"
    other_root.mkdir()
    other_repo = init_repo(other_root)

    child_result = run_cli(["new", "second-track", "--here"], other_repo, env)
    assert child_result.returncode == 0, child_result.stderr

    result = run_cli(["rename", "second-track", "Readable First Track"], other_repo, env)
    assert result.returncode == 1
    assert "Track name 'Readable First Track' is already in use." in result.stderr


def test_remove_deletes_track_detaches_sessions_and_clears_parent(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
    }

    result = run_cli(["new", "root-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    session_index = home / ".codex" / "session_index.jsonl"
    session_index.parent.mkdir(parents=True)
    session_index.write_text(
        '{"id":"session-a","thread_name":"Root Session","updated_at":"2026-05-07T10:00:00Z"}\n',
        encoding="utf-8",
    )

    result = run_cli(["codex", "attach", "root-track", "session-a"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["new", "child-track"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["remove", "root-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "root-track: removed" in result.stdout
    assert "root-track: detached 1 session(s)" in result.stdout
    assert "root-track: cleared parent on 1 child track(s)" in result.stdout

    result = run_cli(["show", "root-track"], repo, env)
    assert result.returncode == 1
    assert "Track 'root-track' was not found." in result.stderr

    result = run_cli(["codex", "unlabeled"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "session-a" in result.stdout

    result = run_cli(["show", "child-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "Parent:" not in result.stdout


def test_remove_with_worktree_removes_child_and_refuses_main_checkout(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
    }

    result = run_cli(["new", "root-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["remove", "root-track", "--remove-worktree"], repo, env)
    assert result.returncode == 1
    assert "Cannot remove the main checkout for track 'root-track'." in result.stderr

    result = run_cli(["new", "child-track"], repo, env)
    assert result.returncode == 0, result.stderr
    child_worktree = tmp_path / "repo-child-track"
    assert child_worktree.exists()

    result = run_cli(["remove", "child-track", "--remove-worktree"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "child-track: removed" in result.stdout
    assert "child-track: worktree removed" in result.stdout
    assert not child_worktree.exists()
    assert "p/bcg/child-track" in git_output(repo, "branch", "--list", "p/bcg/child-track")


def test_interactive_remove_and_remove_worktree(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    make_executable(
        fake_bin / "fzf",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys

            lines = [line.rstrip("\\n") for line in sys.stdin if line.strip()]
            if not lines:
                raise SystemExit(1)
            print(lines[0])
            """
        ),
    )
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{fake_bin}:{Path('/usr/bin')}:{Path('/bin')}",
    }

    result = run_cli(["new", "root-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["i", "remove"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "root-track: removed" in result.stdout

    result = run_cli(["show", "root-track"], repo, env)
    assert result.returncode == 1
    assert "Track 'root-track' was not found." in result.stderr

    result = run_cli(["new", "root-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr
    result = run_cli(["new", "child-track"], repo, env)
    assert result.returncode == 0, result.stderr

    child_worktree = tmp_path / "repo-child-track"
    assert child_worktree.exists()

    result = run_cli(["i", "remove", "--remove-worktree"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "child-track: removed" in result.stdout
    assert "child-track: worktree removed" in result.stdout
    assert not child_worktree.exists()
