from __future__ import annotations

import os
from pathlib import Path
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[1]


def run_script(script_name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / "scripts" / script_name)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def run_track(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["track", *args],
        cwd=cwd,
        env=env,
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


def init_repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


def install_isolated_track(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    codex_home = home / ".codex"
    xdg_data_home = home / ".local" / "share"
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

    install_env = {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "XDG_DATA_HOME": str(xdg_data_home),
        "PATH": os.environ["PATH"],
    }
    install_result = run_script("install.sh", install_env)
    assert install_result.returncode == 0, install_result.stderr

    runtime_env = {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "TRACK_COORDINATOR_HOME": str(home / "state"),
        "XDG_DATA_HOME": str(xdg_data_home),
        "PATH": f"{fake_bin}:{home / '.local' / 'bin'}:{os.environ['PATH']}",
    }
    return runtime_env, home, log_path


def test_resume_e2e_with_and_without_codex_sessions_and_done_refusal(tmp_path: Path) -> None:
    env, home, log_path = install_isolated_track(tmp_path)
    repo = init_repo(tmp_path)

    session_index = home / ".codex" / "session_index.jsonl"
    session_index.parent.mkdir(parents=True, exist_ok=True)
    session_index.write_text(
        '{"id":"session-a","thread_name":"Resume Track Session","updated_at":"2026-05-08T09:00:00Z"}\n',
        encoding="utf-8",
    )

    result = run_track(["new", "resume-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["next", "resume-track", "Pick", "up", "docs"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["codex", "attach", "resume-track", "session-a"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["pause", "resume-track"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["resume"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "Track: resume-track (resume-track)" in result.stdout
    assert "Status: active" in result.stdout
    assert "Next step: Pick up docs" in result.stdout
    assert "Resume Track Session" in result.stdout
    assert "Reopen Codex session in the VS Code extension: Resume Track Session" in result.stdout
    assert f"code:-n {repo}" in log_path.read_text(encoding="utf-8")

    result = run_track(["show", "resume-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "Status: active" in result.stdout

    second_repo = init_repo(tmp_path, name="repo-no-session")
    result = run_track(["new", "solo-track", "--here"], second_repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["pause", "solo-track"], second_repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["resume", "solo-track"], second_repo, env)
    assert result.returncode == 0, result.stderr
    assert "Track: solo-track (solo-track)" in result.stdout
    assert "No Codex sessions attached." in result.stdout

    result = run_track(["done", "solo-track"], second_repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["resume", "solo-track"], second_repo, env)
    assert result.returncode == 1
    assert "Track 'solo-track' is done." in result.stderr


def test_metadata_editing_e2e(tmp_path: Path) -> None:
    env, _home, log_path = install_isolated_track(tmp_path)
    repo = init_repo(tmp_path)

    result = run_track(["new", "root-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["purpose", "root-track", "Tighten", "resume", "flow"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "root-track: purpose=Tighten resume flow" in result.stdout

    result = run_track(["show", "root-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "Purpose: Tighten resume flow" in result.stdout

    workspace_file = tmp_path / "root-track.code-workspace"
    workspace_file.write_text('{"folders":[{"path":"."}]}', encoding="utf-8")

    result = run_track(["workspace", "root-track", str(workspace_file)], repo, env)
    assert result.returncode == 0, result.stderr
    assert f"root-track: workspace={workspace_file.resolve()}" in result.stdout

    result = run_track(["open", "root-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert str(workspace_file.resolve()) in log_path.read_text(encoding="utf-8")

    result = run_track(["new", "child-track"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["parent", "child-track", "root-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "child-track: parent=root-track" in result.stdout

    result = run_track(["show", "child-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "Parent: root-track" in result.stdout

    result = run_track(["purpose", "root-track", "--clear"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "root-track: purpose=-" in result.stdout

    result = run_track(["workspace", "root-track", "--clear"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "root-track: workspace=-" in result.stdout

    result = run_track(["parent", "child-track", "--clear"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "child-track: parent=-" in result.stdout

    result = run_track(["show", "root-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "Purpose:" not in result.stdout
    assert "Workspace:" not in result.stdout

    result = run_track(["show", "child-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "Parent:" not in result.stdout

    result = run_track(["open", "root-track"], repo, env)
    assert result.returncode == 0, result.stderr
    assert f"code:-n {repo}" in log_path.read_text(encoding="utf-8")

    result = run_track(["workspace", "root-track", str(tmp_path / "missing.code-workspace")], repo, env)
    assert result.returncode == 1
    assert "Workspace path does not exist:" in result.stderr

    result = run_track(["parent", "root-track", "root-track"], repo, env)
    assert result.returncode == 1
    assert "A track cannot be its own parent." in result.stderr

    result = run_track(["purpose", "root-track", "--clear", "extra"], repo, env)
    assert result.returncode != 0
    assert "extra" in result.stderr

    result = run_track(["workspace", "root-track", "--clear", str(workspace_file)], repo, env)
    assert result.returncode != 0
    assert str(workspace_file) in result.stderr

    result = run_track(["parent", "child-track", "--clear", "root-track"], repo, env)
    assert result.returncode != 0
    assert "root-track" in result.stderr


def test_completion_e2e_with_installer_and_shell_smoke(tmp_path: Path) -> None:
    env, home, _log_path = install_isolated_track(tmp_path)
    repo = init_repo(tmp_path)

    result = run_track(["new", "root-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["new", "done-track"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["done", "done-track"], repo, env)
    assert result.returncode == 0, result.stderr

    result = run_track(["pause", "root-track"], repo, env)
    assert result.returncode == 0, result.stderr

    completion_result = run_track(["completion", "bash"], repo, env)
    assert completion_result.returncode == 0, completion_result.stderr
    assert "complete -F _track_complete track" in completion_result.stdout
    assert "track _complete tracks" in completion_result.stdout

    result = run_track(["_complete", "tracks"], repo, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "root-track"

    result = run_track(["_complete", "tracks", "--all"], repo, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["root-track", "done-track"]

    result = run_track(["_complete", "tracks", "--statuses", "parked"], repo, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "root-track"

    completion_file = home / ".local" / "share" / "bash-completion" / "completions" / "track"
    assert completion_file.exists()
    assert "complete -F _track_complete track" in completion_file.read_text(encoding="utf-8")

    shell_script = textwrap.dedent(
        f"""\
        set -euo pipefail
        source "{completion_file}"

        complete_case() {{
          local label="$1"
          shift
          COMP_WORDS=("$@")
          COMP_CWORD=$((${{#COMP_WORDS[@]}} - 1))
          COMPREPLY=()
          _track_complete
          printf '%s:%s\\n' "$label" "${{COMPREPLY[*]}}"
        }}

        complete_case top track re
        complete_case resume track resume ""
        complete_case purpose track purpose ""
        complete_case cleanup track cleanup ""
        """
    )
    bash_result = subprocess.run(
        ["bash", "-c", shell_script],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert bash_result.returncode == 0, bash_result.stderr

    output_lines = dict(line.split(":", 1) for line in bash_result.stdout.splitlines() if ":" in line)
    assert "resume" in output_lines["top"]
    assert "rename" in output_lines["top"]
    assert "root-track" in output_lines["resume"]
    assert "done-track" not in output_lines["resume"]
    assert "root-track" in output_lines["purpose"]
    assert "done-track" in output_lines["purpose"]
    assert "done-track" in output_lines["cleanup"]
    assert "root-track" not in output_lines["cleanup"]

    uninstall_env = {
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "PATH": os.environ["PATH"],
    }
    uninstall_result = run_script("uninstall.sh", uninstall_env)
    assert uninstall_result.returncode == 0, uninstall_result.stderr
    assert "Removed bash completion:" in uninstall_result.stdout
    assert not completion_file.exists()


def test_codex_status_e2e(tmp_path: Path) -> None:
    env, home, _log_path = install_isolated_track(tmp_path)
    repo = init_repo(tmp_path)

    session_index = home / ".codex" / "session_index.jsonl"
    session_index.parent.mkdir(parents=True, exist_ok=True)
    session_index.write_text(
        '{"id":"session-running","thread_name":"Running Session","updated_at":"2026-05-08T09:00:00Z"}\n'
        '{"id":"session-waiting","thread_name":"Waiting Session","updated_at":"2026-05-08T09:05:00Z"}\n'
        '{"id":"session-idle","thread_name":"Idle Session","updated_at":"2026-05-08T09:10:00Z"}\n',
        encoding="utf-8",
    )
    sessions_dir = home / ".codex" / "sessions" / "2026" / "05" / "08"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "rollout-2026-05-08T09-00-00-session-running.jsonl").write_text(
        f'{{"timestamp":"2026-05-08T09:00:00.000Z","type":"session_meta","payload":{{"id":"session-running","cwd":"{repo}"}}}}\n'
        '{"timestamp":"2026-05-08T09:00:01.000Z","type":"event_msg","payload":{"type":"task_started","turn_id":"turn-running"}}\n'
        '{"timestamp":"2026-05-08T09:00:02.000Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","call_id":"call-running","arguments":"{}"}}\n',
        encoding="utf-8",
    )
    (sessions_dir / "rollout-2026-05-08T09-05-00-session-waiting.jsonl").write_text(
        f'{{"timestamp":"2026-05-08T09:05:00.000Z","type":"session_meta","payload":{{"id":"session-waiting","cwd":"{repo}"}}}}\n'
        '{"timestamp":"2026-05-08T09:05:01.000Z","type":"event_msg","payload":{"type":"task_started","turn_id":"turn-waiting"}}\n'
        '{"timestamp":"2026-05-08T09:05:02.000Z","type":"response_item","payload":{"type":"function_call","name":"request_user_input","call_id":"call-waiting","arguments":"{}"}}\n',
        encoding="utf-8",
    )
    (sessions_dir / "rollout-2026-05-08T09-10-00-session-idle.jsonl").write_text(
        f'{{"timestamp":"2026-05-08T09:10:00.000Z","type":"session_meta","payload":{{"id":"session-idle","cwd":"{repo}"}}}}\n'
        '{"timestamp":"2026-05-08T09:10:01.000Z","type":"event_msg","payload":{"type":"task_started","turn_id":"turn-idle"}}\n'
        '{"timestamp":"2026-05-08T09:10:02.000Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","call_id":"call-idle","arguments":"{}"}}\n'
        '{"timestamp":"2026-05-08T09:10:03.000Z","type":"response_item","payload":{"type":"function_call_output","call_id":"call-idle","output":"ok"}}\n'
        '{"timestamp":"2026-05-08T09:10:04.000Z","type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-idle"}}\n',
        encoding="utf-8",
    )

    result = run_track(["new", "status-track", "--here"], repo, env)
    assert result.returncode == 0, result.stderr

    for session_id in ("session-running", "session-waiting", "session-idle"):
        result = run_track(["codex", "attach", "status-track", session_id], repo, env)
        assert result.returncode == 0, result.stderr

    result = run_track(["codex", "status"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "session-running" in result.stdout
    assert "running" in result.stdout
    assert "tool call in progress: exec_command" in result.stdout
    assert "session-waiting" in result.stdout
    assert "waiting" in result.stdout
    assert "user input requested" in result.stdout
    assert "session-idle" in result.stdout
    assert "idle" in result.stdout

    result = run_track(["sessions"], repo, env)
    assert result.returncode == 0, result.stderr
    assert "Track: status-track (status-track)" in result.stdout
    assert "codex" in result.stdout
    assert "session-running" in result.stdout
    assert "live=run:1 wait:1 idle:1" in result.stdout
