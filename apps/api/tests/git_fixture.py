"""Local git repositories for tests (no network)."""

import subprocess
from pathlib import Path


def git_repo(root: Path, tree: dict[str, str]) -> Path:
    """A repository at `root` on branch main with one commit of `tree` (path -> text)."""
    for rel, text in tree.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run([*git, "add", "-f", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    return root


def git_commit(root: Path, changes: dict[str, str | None], message: str) -> str:
    """Write (or, for None, delete) files, commit everything and return the new HEAD sha."""
    for rel, text in changes.items():
        path = root / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", "-A", "-f", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", message], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    return head.stdout.strip()
