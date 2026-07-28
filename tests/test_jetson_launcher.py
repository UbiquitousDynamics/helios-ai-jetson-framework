from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.run_jetson import (
    build_environment,
    find_openmp_runtime,
    prepend_preload,
    select_interpreter,
)


def make_interpreter(root: Path, environment_name: str = "venv") -> Path:
    interpreter = root / environment_name / "bin/python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"")
    return interpreter


def test_launcher_selects_repository_venv(tmp_path: Path) -> None:
    interpreter = make_interpreter(tmp_path)

    assert select_interpreter(tmp_path, {}) == interpreter.resolve()


def test_launcher_honors_explicit_interpreter(tmp_path: Path) -> None:
    explicit = make_interpreter(tmp_path, "deployment")
    make_interpreter(tmp_path)

    assert select_interpreter(tmp_path, {"HELIOS_PYTHON": str(explicit)}) == explicit.resolve()


def test_launcher_keeps_virtualenv_interpreter_symlink(tmp_path: Path) -> None:
    base_interpreter = tmp_path / "base/bin/python3.10"
    base_interpreter.parent.mkdir(parents=True)
    base_interpreter.write_bytes(b"")
    venv_interpreter = tmp_path / "venv/bin/python3"
    venv_interpreter.parent.mkdir(parents=True)
    try:
        os.symlink(base_interpreter, venv_interpreter)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    selected = select_interpreter(tmp_path, {})

    assert selected == venv_interpreter.absolute()
    assert selected != base_interpreter.resolve()


def test_launcher_rejects_missing_explicit_interpreter(tmp_path: Path) -> None:
    make_interpreter(tmp_path)

    with pytest.raises(RuntimeError, match="HELIOS_PYTHON does not point to a file"):
        select_interpreter(
            tmp_path,
            {"HELIOS_PYTHON": str(tmp_path / "missing/bin/python3")},
        )


def test_launcher_requires_a_virtualenv_interpreter(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="No Helios virtualenv interpreter"):
        select_interpreter(tmp_path, {})


def test_launcher_prefers_scikit_learn_openmp_runtime(tmp_path: Path) -> None:
    interpreter = make_interpreter(tmp_path)
    system_copy = tmp_path / "venv/lib/python3.10/site-packages/torch.libs/libgomp-torch.so.1"
    sklearn_copy = (
        tmp_path / "venv/lib/python3.10/site-packages/scikit_learn.libs/libgomp-sklearn.so.1"
    )
    system_copy.parent.mkdir(parents=True)
    sklearn_copy.parent.mkdir(parents=True)
    system_copy.write_bytes(b"")
    sklearn_copy.write_bytes(b"")

    assert find_openmp_runtime(interpreter) == sklearn_copy.resolve()


def test_launcher_prepends_openmp_runtime_once() -> None:
    # LD_PRELOAD is configured only on AArch64/Linux. A Windows tmp_path
    # contains a drive-letter colon, which is also the Linux list separator and
    # cannot represent a real LD_PRELOAD entry.
    library = Path("/opt/helios/libgomp.so.1")

    assert prepend_preload("/existing.so", library) == f"{library}:/existing.so"
    assert prepend_preload(f"{library}:/existing.so", library) == f"{library}:/existing.so"


def test_non_arm_launcher_does_not_change_preload(tmp_path: Path) -> None:
    interpreter = make_interpreter(tmp_path)

    environment, library = build_environment(
        interpreter,
        {"LD_PRELOAD": "/existing.so"},
        machine="x86_64",
    )

    assert library is None
    assert environment["LD_PRELOAD"] == "/existing.so"
