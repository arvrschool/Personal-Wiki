from __future__ import annotations

import subprocess
from pathlib import Path


def find_git_root(start: Path, max_depth: int = 8) -> Path | None:
    path = start.resolve()
    for _ in range(max_depth):
        if (path / ".git").exists():
            return path
        if path.parent == path:
            break
        path = path.parent
    return None


def git_commit_paths(base_dir: Path, touched_paths: list[Path], message: str) -> tuple[bool, str]:
    """Stage and commit only the touched paths that live inside the same git repo."""
    git_root = find_git_root(base_dir)
    if git_root is None:
        return False, "git repo not found"

    rel_paths: list[str] = []
    seen: set[str] = set()
    for path in touched_paths:
        try:
            rel = path.resolve().relative_to(git_root.resolve())
        except ValueError:
            continue
        rel_str = str(rel)
        if rel_str not in seen:
            seen.add(rel_str)
            rel_paths.append(rel_str)

    if not rel_paths:
        return False, "no touched paths inside git repo"

    add_result = subprocess.run(
        ["git", "-C", str(git_root), "add", "--", *rel_paths],
        capture_output=True,
        text=True,
    )
    if add_result.returncode != 0:
        stderr = (add_result.stderr or add_result.stdout).strip()
        return False, f"git add failed: {stderr[:160]}"

    diff_result = subprocess.run(
        ["git", "-C", str(git_root), "diff", "--cached", "--quiet", "--", *rel_paths],
        capture_output=True,
        text=True,
    )
    if diff_result.returncode == 0:
        return False, "no staged changes in touched paths"
    if diff_result.returncode not in (0, 1):
        stderr = (diff_result.stderr or diff_result.stdout).strip()
        return False, f"git diff failed: {stderr[:160]}"

    commit_result = subprocess.run(
        ["git", "-C", str(git_root), "commit", "-m", message, "--", *rel_paths],
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        stderr = (commit_result.stderr or commit_result.stdout).strip()
        return False, f"git commit failed: {stderr[:160]}"

    return True, message
