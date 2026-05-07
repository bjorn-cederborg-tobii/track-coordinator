from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    branch: str
    detached: bool


@dataclass(frozen=True)
class GitContext:
    repo_path: Path
    worktree_path: Path
    branch: str
    detached: bool


def git_output(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise GitError(stderr or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def current_context(cwd: Path) -> GitContext:
    worktree_path = Path(git_output(cwd, "rev-parse", "--show-toplevel")).resolve()
    worktrees = list_worktrees(worktree_path)
    repo_path = worktrees[0].path if worktrees else worktree_path
    branch, detached = current_branch(worktree_path)
    return GitContext(
        repo_path=repo_path.resolve(),
        worktree_path=worktree_path.resolve(),
        branch=branch,
        detached=detached,
    )


def current_branch(cwd: Path) -> tuple[str, bool]:
    completed = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout.strip(), False
    head = git_output(cwd, "rev-parse", "--short", "HEAD")
    return head, True


def list_worktrees(cwd: Path) -> list[WorktreeInfo]:
    output = git_output(cwd, "worktree", "list", "--porcelain")
    worktrees: list[WorktreeInfo] = []
    current_path: Path | None = None
    branch = "HEAD"
    detached = False
    for raw_line in output.splitlines() + [""]:
        line = raw_line.strip()
        if not line:
            if current_path is not None:
                worktrees.append(WorktreeInfo(path=current_path.resolve(), branch=branch, detached=detached))
            current_path = None
            branch = "HEAD"
            detached = False
            continue
        if line.startswith("worktree "):
            current_path = Path(line.split(" ", 1)[1])
        elif line.startswith("branch "):
            branch = line.split(" ", 1)[1].removeprefix("refs/heads/")
            detached = False
        elif line == "detached":
            detached = True
            branch = "HEAD"
    return worktrees


def find_default_base_ref(cwd: Path) -> str:
    candidates = [
        ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"),
        ("show-ref", "--verify", "--quiet", "refs/remotes/origin/main"),
        ("show-ref", "--verify", "--quiet", "refs/remotes/origin/master"),
    ]
    try:
        origin_head = git_output(cwd, *candidates[0])
    except GitError:
        origin_head = ""
    if origin_head:
        return origin_head

    for ref_name, result in (("origin/main", candidates[1]), ("origin/master", candidates[2])):
        completed = subprocess.run(["git", *result], cwd=cwd, check=False)
        if completed.returncode == 0:
            return ref_name

    branch, detached = current_branch(cwd)
    return "HEAD" if detached else branch


def add_worktree(cwd: Path, branch: str, worktree_path: Path, base_ref: str) -> None:
    completed = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), base_ref],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise GitError(stderr or "git worktree add failed")

