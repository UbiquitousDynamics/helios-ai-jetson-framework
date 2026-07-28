"""Launch Helios with the repository virtualenv and Jetson native-library setup."""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARM64_NAMES = {"aarch64", "arm64"}


def interpreter_candidates(
    root: Path,
    environ: Mapping[str, str],
) -> tuple[Path, ...]:
    """Return deployment interpreter candidates in priority order."""

    candidates: list[Path] = []
    explicit = environ.get("HELIOS_PYTHON", "").strip()
    active_venv = environ.get("VIRTUAL_ENV", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if active_venv:
        candidates.append(Path(active_venv).expanduser() / "bin/python3")
    candidates.extend((root / "venv/bin/python3", root / ".venv/bin/python3"))
    return tuple(candidates)


def select_interpreter(root: Path, environ: Mapping[str, str]) -> Path:
    """Select an existing virtualenv interpreter or fail with an actionable error."""

    explicit = environ.get("HELIOS_PYTHON", "").strip()
    if explicit:
        requested = Path(explicit).expanduser()
        if requested.is_file():
            return requested.resolve()
        raise RuntimeError(f"HELIOS_PYTHON does not point to a file: {requested}")

    candidates = interpreter_candidates(root, environ)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    rendered = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(
        "No Helios virtualenv interpreter was found. Create 'venv' or '.venv', "
        f"activate one, or set HELIOS_PYTHON. Checked: {rendered}"
    )


def find_openmp_runtime(interpreter: Path) -> Path | None:
    """Prefer the libgomp bundled by scikit-learn, then the Jetson system copy."""

    environment_root = interpreter.parent.parent
    private_patterns = (
        "lib/python*/site-packages/scikit_learn.libs/libgomp*.so*",
        "lib/python*/site-packages/torch.libs/libgomp*.so*",
    )
    for pattern in private_patterns:
        candidates = sorted(path for path in environment_root.glob(pattern) if path.is_file())
        if candidates:
            return candidates[0].resolve()

    system_library = Path("/usr/lib/aarch64-linux-gnu/libgomp.so.1")
    return system_library if system_library.is_file() else None


def prepend_preload(existing: str, library: Path) -> str:
    """Prepend a library to LD_PRELOAD without duplicating it."""

    library_text = str(library)
    entries = [entry for entry in existing.split(":") if entry]
    entries = [entry for entry in entries if entry != library_text]
    return ":".join((library_text, *entries))


def build_environment(
    interpreter: Path,
    environ: Mapping[str, str],
    *,
    machine: str,
) -> tuple[dict[str, str], Path | None]:
    """Build the child environment and return any selected OpenMP preload."""

    child = dict(environ)
    library = find_openmp_runtime(interpreter) if machine.lower() in ARM64_NAMES else None
    if library is not None:
        child["LD_PRELOAD"] = prepend_preload(child.get("LD_PRELOAD", ""), library)
    return child, library


def main() -> int:
    try:
        interpreter = select_interpreter(PROJECT_ROOT, os.environ)
    except RuntimeError as exc:
        print(f"Helios Jetson launcher: {exc}", file=sys.stderr)
        return 2

    machine = platform.machine()
    child_environment, library = build_environment(
        interpreter,
        os.environ,
        machine=machine,
    )
    if machine.lower() in ARM64_NAMES:
        if library is None:
            print(
                "Helios Jetson launcher: no libgomp runtime was found; "
                "the RAG import may fail with a static TLS error.",
                file=sys.stderr,
            )
        else:
            print(f"Helios Jetson launcher: preloading {library}", file=sys.stderr)
    print(f"Helios Jetson launcher: using {interpreter}", file=sys.stderr)

    arguments = sys.argv[1:]
    target = PROJECT_ROOT / "main.py"
    if arguments[:1] == ["--doctor"]:
        target = PROJECT_ROOT / "scripts/doctor.py"
        arguments = arguments[1:]

    command = [str(interpreter), str(target), *arguments]
    os.execve(str(interpreter), command, child_environment)
    return 0  # pragma: no cover - os.execve replaces the process


if __name__ == "__main__":
    raise SystemExit(main())
