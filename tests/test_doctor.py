from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.doctor import check_assets, check_python, exit_code


def write_manifest(root: Path, artifacts: list[object]) -> Path:
    path = root / "assets-manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "artifacts": artifacts}), encoding="utf-8")
    return path


def test_supported_python_is_accepted() -> None:
    checks = check_python((3, 10))

    assert exit_code(checks) == 0
    assert checks[0].code == "python.supported"


def test_required_asset_and_hash_are_validated(tmp_path: Path) -> None:
    model = tmp_path / "model.bin"
    model.write_bytes(b"known model")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "model",
                "path": "model.bin",
                "kind": "file",
                "required": True,
                "license": {"status": "verified"},
                "integrity": {
                    "file": "model.bin",
                    "sha256": hashlib.sha256(b"known model").hexdigest(),
                },
            }
        ],
    )

    checks = check_assets(tmp_path, manifest, verify_hashes=True)

    assert exit_code(checks) == 0
    assert any(check.code == "assets.hash_match" for check in checks)


def test_hash_mismatch_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "model.bin").write_bytes(b"unexpected")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "model",
                "path": "model.bin",
                "kind": "file",
                "required": True,
                "license": {"status": "verified"},
                "integrity": {"file": "model.bin", "sha256": "0" * 64},
            }
        ],
    )

    checks = check_assets(tmp_path, manifest, verify_hashes=True)

    assert exit_code(checks) == 1
    assert any(check.code == "assets.hash_mismatch" for check in checks)


def test_required_companion_is_validated(tmp_path: Path) -> None:
    (tmp_path / "model.bin").write_bytes(b"model")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "model",
                "path": "model.bin",
                "kind": "file",
                "required": True,
                "companions": ["model.json"],
                "license": {"status": "verified"},
                "integrity": None,
            }
        ],
    )

    checks = check_assets(tmp_path, manifest, verify_hashes=False)

    assert exit_code(checks) == 1
    assert any(check.code == "assets.companion_missing" for check in checks)


def test_missing_optional_asset_is_only_a_warning(tmp_path: Path) -> None:
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "legacy",
                "path": "legacy.bin",
                "kind": "file",
                "required": False,
                "license": {"status": "not-recorded"},
                "integrity": None,
            }
        ],
    )

    checks = check_assets(tmp_path, manifest, verify_hashes=False)

    assert exit_code(checks) == 0
    assert checks[0].level == "warning"


def test_manifest_path_cannot_escape_repository(tmp_path: Path) -> None:
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "escape",
                "path": "../outside.bin",
                "kind": "file",
                "required": True,
                "license": {"status": "verified"},
                "integrity": None,
            }
        ],
    )

    checks = check_assets(tmp_path, manifest, verify_hashes=False)

    assert exit_code(checks) == 1
    assert checks[0].code == "assets.path_escape"


def test_malformed_inventory_entry_is_reported(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path, ["not-an-object"])

    checks = check_assets(tmp_path, manifest, verify_hashes=False)

    assert exit_code(checks) == 1
    assert checks[0].code == "assets.entry"
