from __future__ import annotations

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


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


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

    result = run_cli(["new", "XR5ML-482-marlin-fixes"], repo, env)
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

    result = run_cli(["list"], repo, env)
    assert "xr5ml-482-marlin-fixes" not in result.stdout

    result = run_cli(["list", "--all"], repo, env)
    assert "xr5ml-482-marlin-fixes" in result.stdout


def test_worktree_creation_scan_here_and_unlabeled(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    selection_file = tmp_path / "selection.log"
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

    result = run_cli(["new", "feature-x", "--worktree"], repo, env)
    assert result.returncode == 0, result.stderr
    expected_worktree = tmp_path / "repo-feature-x"
    assert expected_worktree.exists()

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


def test_attach_current_falls_back_to_session_metadata(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = init_repo(tmp_path)
    env = {
        "HOME": str(home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
    }

    result = run_cli(["new", "metadata-track"], repo, env)
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


def test_note_edit_and_interactive_open(tmp_path: Path):
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

    result = run_cli(["new", "interactive-track"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["note", "edit", "interactive-track"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_cli(["show", "interactive-track"], repo, env)
    assert "edited note" in result.stdout

    result = run_cli(["i", "open"], repo, env)
    assert result.returncode == 0, result.stderr
    log_text = log_path.read_text(encoding="utf-8")
    assert "code:-n" in log_text
