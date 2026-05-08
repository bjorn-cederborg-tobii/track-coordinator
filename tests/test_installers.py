from __future__ import annotations

from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def run_script(script_name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / "scripts" / script_name)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def fake_uv_script(log_path: Path) -> str:
    return f"""#!/usr/bin/env python3
import os
import pathlib
import sys

log_path = pathlib.Path({str(log_path)!r})
state_path = pathlib.Path(os.environ["FAKE_UV_STATE"])
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.touch(exist_ok=True)
args = sys.argv[1:]
log_path.parent.mkdir(parents=True, exist_ok=True)
with log_path.open("a", encoding="utf-8") as handle:
    handle.write("uv:" + " ".join(args) + "\\n")

if args[:2] == ["tool", "install"]:
    state_path.write_text("track-coordinator\\n", encoding="utf-8")
    raise SystemExit(0)
if args[:2] == ["tool", "list"]:
    sys.stdout.write(state_path.read_text(encoding="utf-8"))
    raise SystemExit(0)
if args[:2] == ["tool", "uninstall"]:
    state_path.write_text("", encoding="utf-8")
    raise SystemExit(0)

raise SystemExit(1)
"""


def test_install_script_installs_cli_and_syncs_skills(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    xdg_data_home = home / ".local" / "share"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    uv_state = tmp_path / "uv-state.txt"
    stale_skill = codex_home / "skills" / "stale-skill"
    stale_skill.mkdir(parents=True)
    (stale_skill / "SKILL.md").write_text("stale\n", encoding="utf-8")
    (codex_home / ".track-coordinator-installed-skills").write_text("stale-skill\n", encoding="utf-8")

    write_executable(fake_bin / "uv", fake_uv_script(uv_log))
    env = {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "FAKE_UV_STATE": str(uv_state),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = run_script("install.sh", env)
    assert result.returncode == 0, result.stderr
    assert "Installed CLI command: track" in result.stdout
    assert "Installed bash completion:" in result.stdout
    assert "Installed skill: track-workflow" in result.stdout
    assert "Removed stale managed skill: stale-skill" in result.stdout

    uv_log_text = uv_log.read_text(encoding="utf-8")
    assert f"uv:tool install --editable {ROOT} --force" in uv_log_text

    installed_skill = codex_home / "skills" / "track-workflow" / "SKILL.md"
    assert installed_skill.read_text(encoding="utf-8") == (ROOT / "skills" / "track-workflow" / "SKILL.md").read_text(encoding="utf-8")
    assert not stale_skill.exists()
    assert (codex_home / ".track-coordinator-installed-skills").read_text(encoding="utf-8") == "track-workflow\n"
    completion_file = xdg_data_home / "bash-completion" / "completions" / "track"
    assert completion_file.exists()
    assert "complete -F _track_complete track" in completion_file.read_text(encoding="utf-8")


def test_uninstall_script_removes_managed_skill_and_cli(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    completion_file = home / ".local" / "share" / "bash-completion" / "completions" / "track"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    uv_state = tmp_path / "uv-state.txt"

    managed_skill = codex_home / "skills" / "track-workflow"
    managed_skill.mkdir(parents=True)
    (managed_skill / "SKILL.md").write_text("managed\n", encoding="utf-8")
    unrelated_skill = codex_home / "skills" / "keep-me"
    unrelated_skill.mkdir(parents=True)
    (unrelated_skill / "SKILL.md").write_text("keep\n", encoding="utf-8")
    (codex_home / ".track-coordinator-installed-skills").write_text("track-workflow\n", encoding="utf-8")
    uv_state.write_text("track-coordinator v0.1.0\n", encoding="utf-8")
    completion_file.parent.mkdir(parents=True)
    completion_file.write_text("complete -F _track_complete track\n", encoding="utf-8")

    write_executable(fake_bin / "uv", fake_uv_script(uv_log))
    env = {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "FAKE_UV_STATE": str(uv_state),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = run_script("uninstall.sh", env)
    assert result.returncode == 0, result.stderr
    assert "Removed skill: track-workflow" in result.stdout
    assert "Uninstalled CLI package: track-coordinator" in result.stdout
    assert "Removed bash completion:" in result.stdout

    assert not managed_skill.exists()
    assert unrelated_skill.exists()
    assert not (codex_home / ".track-coordinator-installed-skills").exists()
    assert not completion_file.exists()

    uv_log_text = uv_log.read_text(encoding="utf-8")
    assert "uv:tool list" in uv_log_text
    assert "uv:tool uninstall track-coordinator" in uv_log_text
