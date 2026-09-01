from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tests.conftest import pytest_configure


def test_nested_basetemp_parent_is_created_before_fixture_setup(tmp_path: Path) -> None:
    nested_basetemp = tmp_path / "missing-parent" / "test-run"
    config = SimpleNamespace(getoption=lambda name: nested_basetemp if name == "basetemp" else None)

    pytest_configure(config)  # type: ignore[arg-type]

    assert nested_basetemp.parent.is_dir()
    assert not nested_basetemp.exists()
