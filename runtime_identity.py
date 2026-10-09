"""Content-free deployment identity for startup and source synchronization."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

STAMP_NAME = ".helios-version.json"


def git_identity(root: Path) -> tuple[str, bool] | None:
    """Return source revision, with a bounded local Git lookup."""

    try:
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        ).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            return None
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
                capture_output=True,
                text=True,
                check=True,
                timeout=3,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return sha, dirty


def read_identity(root: Path) -> tuple[str, bool] | None:
    if (root / ".git").exists() and (live := git_identity(root)) is not None:
        return live
    stamp = root / STAMP_NAME
    if stamp.is_file():
        try:
            data = json.loads(stamp.read_text(encoding="utf-8"))
            sha, dirty = data["commit"], data["dirty"]
            if re.fullmatch(r"[0-9a-f]{40,64}", sha) and isinstance(dirty, bool):
                if "files" in data and not _matching_snapshot(root, data["files"]):
                    return None
                return sha, dirty
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return git_identity(root)


def _matching_snapshot(root: Path, files: object) -> bool:
    if not isinstance(files, dict) or not files:
        return False
    for name, digest in files.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            return False
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            return False
        try:
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return False
        except OSError:
            return False
    return True


def write_stamp(source: Path, destination: Path) -> Path:
    identity = git_identity(source.resolve())
    if identity is None:
        raise ValueError("source tree has no readable Git identity")
    destination = destination.resolve()
    if not destination.is_dir():
        raise ValueError("deployment directory does not exist")
    stamp = destination / STAMP_NAME
    stamp.write_text(
        json.dumps({"commit": identity[0], "dirty": identity[1]}) + "\n", encoding="utf-8"
    )
    return stamp
