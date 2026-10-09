"""Copy tracked working-tree files to a NEW release directory and stamp last."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from runtime_identity import STAMP_NAME, git_identity


def _tracked_files(source: Path) -> tuple[Path, ...]:
    root = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()
    if Path(root).resolve() != source:
        raise ValueError("source must be the Git working-tree root")
    output = subprocess.run(
        ["git", "-C", str(source), "ls-files", "--stage", "-z"],
        capture_output=True,
        check=True,
        timeout=5,
    ).stdout
    paths = []
    for entry in output.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, _object, stage = metadata.split()
        if mode not in {b"100644", b"100755"} or stage != b"0":
            raise ValueError("symlinks, submodules and unresolved merges are unsupported")
        relative = Path(name.decode("utf-8"))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative in {Path(STAMP_NAME), Path(STAMP_NAME + ".tmp")}
        ):
            raise ValueError("unsupported tracked path")
        paths.append(relative)
    if not paths:
        raise ValueError("source has no tracked files")
    return tuple(paths)


def _digest(source: Path, relative: Path) -> str | None:
    path = source / relative
    # Reject working-tree links even when the Git index describes a regular file.
    if any(
        (source / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)
    ):
        raise ValueError("working-tree symlinks are unsupported")
    if not path.exists():
        return None  # A tracked deletion is part of a dirty working tree.
    return hashlib.sha256(path.read_bytes()).hexdigest()


def deploy_snapshot(source: Path, destination: Path) -> Path:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents:
        raise ValueError("deployment must be outside the source tree")
    identity = git_identity(source)
    if identity is None:
        raise ValueError("source tree has no readable Git identity")
    paths = _tracked_files(source)
    snapshot = {p.as_posix(): _digest(source, p) for p in paths}
    if not any(digest is not None for digest in snapshot.values()):
        raise ValueError("source has no existing tracked files")
    destination.mkdir()  # Exclusive: never overlay code, assets or configuration.
    for relative in paths:
        expected = snapshot[relative.as_posix()]
        if expected is None:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
        if _digest(destination, relative) != expected:
            raise ValueError("source changed during copy; deployment remains unstamped")
    if (
        git_identity(source) != identity
        or _tracked_files(source) != paths
        or any(_digest(source, p) != snapshot[p.as_posix()] for p in paths)
    ):
        raise ValueError("source changed during copy; deployment remains unstamped")
    files = {name: digest for name, digest in snapshot.items() if digest is not None}
    stamp = destination / STAMP_NAME
    temporary = destination / (STAMP_NAME + ".tmp")
    temporary.write_text(
        json.dumps({"commit": identity[0], "dirty": identity[1], "files": files}) + "\n",
        encoding="utf-8",
    )
    temporary.replace(stamp)
    return stamp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("deployment", type=Path, help="new directory; parent must exist")
    args = parser.parse_args()
    print(deploy_snapshot(args.source, args.deployment))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
