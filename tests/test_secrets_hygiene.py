"""Regression guard: no real secrets file ever gets tracked by git.

Complements the manual review in SECURITY_AUDIT.md §10 with an automated
check that runs on every PR (via `make check` / CI), so a future contributor
who accidentally `git add`s a real `.env` gets caught before merge instead of
relying on someone noticing in review.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_ALLOWED_ENV_FILES = {".env.example", ".env.dev.example"}


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def test_no_real_env_file_is_tracked_by_git() -> None:
    tracked_env_files = [
        path
        for path in _tracked_files()
        if Path(path).name.startswith(".env")
        and Path(path).name not in _ALLOWED_ENV_FILES
    ]
    assert tracked_env_files == [], (
        f"Real .env file(s) tracked by git: {tracked_env_files}. "
        "Only .env.example and .env.dev.example may be committed."
    )


def test_gitignore_excludes_env_files() -> None:
    gitignore = Path(__file__).resolve().parent.parent / ".gitignore"
    content = gitignore.read_text()
    assert ".env" in content
    assert ".env.*" in content


def test_dockerignore_excludes_env_files() -> None:
    dockerignore = Path(__file__).resolve().parent.parent / ".dockerignore"
    content = dockerignore.read_text()
    assert ".env" in content


def test_dockerfile_never_copies_an_env_file() -> None:
    dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile"
    content = dockerfile.read_text()
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("COPY") or stripped.startswith("ADD"):
            assert ".env" not in stripped, f"Dockerfile copies an env file: {line}"
