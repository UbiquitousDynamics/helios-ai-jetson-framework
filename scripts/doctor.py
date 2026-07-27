"""Validate a Helios checkout without loading models or opening audio devices."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

MINIMUM_PYTHON = (3, 10)
DEFAULT_MANIFEST = "assets-manifest.json"

RUNTIME_IMPORTS = {
    "numpy": "NumPy",
    "ollama": "Ollama Python client",
    "piper": "Piper TTS",
    "pyaudio": "PyAudio",
    "sentence_transformers": "Sentence Transformers",
    "sounddevice": "sounddevice",
    "vosk": "Vosk",
}
PLATFORM_IMPORTS = {
    "onnxruntime": "ONNX Runtime",
    "torch": "PyTorch",
}


@dataclass(frozen=True)
class Check:
    level: str
    code: str
    message: str


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_repo_path(root: Path, relative: str) -> Path | None:
    candidate = (root / Path(relative)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict):
        raise ValueError("asset manifest root must be an object")
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported asset manifest schema")
    if not isinstance(manifest.get("artifacts"), list):
        raise ValueError("asset manifest must contain an artifacts list")
    return manifest


def check_python(version: tuple[int, ...] = sys.version_info) -> list[Check]:
    current = tuple(version[:2])
    if current < MINIMUM_PYTHON:
        return [
            Check(
                "error",
                "python.unsupported",
                f"Python {current[0]}.{current[1]} is unsupported; "
                f"use {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer.",
            )
        ]
    return [
        Check(
            "ok",
            "python.supported",
            f"Python {current[0]}.{current[1]} satisfies the supported range.",
        )
    ]


def check_assets(root: Path, manifest_path: Path, verify_hashes: bool) -> list[Check]:
    checks: list[Check] = []
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return [Check("error", "assets.manifest", f"Cannot load {manifest_path}: {error}")]

    for raw_artifact in manifest["artifacts"]:
        if not isinstance(raw_artifact, dict):
            checks.append(
                Check("error", "assets.entry", "Asset inventory entry must be an object.")
            )
            continue
        artifact = raw_artifact
        artifact_id = str(artifact.get("id", "<missing-id>"))
        relative = artifact.get("path")
        if not isinstance(relative, str) or not relative:
            checks.append(
                Check("error", "assets.path", f"{artifact_id}: path is missing or invalid.")
            )
            continue

        target = _safe_repo_path(root, relative)
        if target is None:
            checks.append(
                Check("error", "assets.path_escape", f"{artifact_id}: path escapes the repository.")
            )
            continue

        required = bool(artifact.get("required", False))
        if not target.exists():
            level = "error" if required else "warning"
            checks.append(Check(level, "assets.missing", f"{artifact_id}: {relative} is missing."))
            continue

        expected_kind = artifact.get("kind")
        kind_matches = (expected_kind == "file" and target.is_file()) or (
            expected_kind == "directory" and target.is_dir()
        )
        if not kind_matches:
            checks.append(
                Check(
                    "error",
                    "assets.kind",
                    f"{artifact_id}: {relative} is not a {expected_kind}.",
                )
            )
            continue

        checks.append(Check("ok", "assets.present", f"{artifact_id}: {relative} is present."))

        companions = artifact.get("companions", [])
        if not isinstance(companions, list) or not all(
            isinstance(companion, str) for companion in companions
        ):
            checks.append(
                Check(
                    "error",
                    "assets.companions",
                    f"{artifact_id}: companions must be a list of paths.",
                )
            )
        else:
            for companion in companions:
                companion_target = _safe_repo_path(root, companion)
                if companion_target is None or not companion_target.is_file():
                    level = "error" if required else "warning"
                    checks.append(
                        Check(
                            level,
                            "assets.companion_missing",
                            f"{artifact_id}: required companion {companion} is missing.",
                        )
                    )

        license_metadata = artifact.get("license")
        license_status = (
            license_metadata.get("status") if isinstance(license_metadata, dict) else None
        )
        if license_status in {None, "not-recorded", "inherits-source-corpus"}:
            checks.append(
                Check(
                    "warning",
                    "assets.license_gap",
                    f"{artifact_id}: license metadata is {license_status or 'missing'}.",
                )
            )

        integrity = artifact.get("integrity")
        if verify_hashes and integrity:
            if not isinstance(integrity, dict):
                checks.append(
                    Check(
                        "error",
                        "assets.hash_metadata",
                        f"{artifact_id}: integrity metadata must be an object.",
                    )
                )
                continue
            hash_relative = integrity.get("file")
            expected_hash = integrity.get("sha256")
            hash_target = (
                _safe_repo_path(root, hash_relative) if isinstance(hash_relative, str) else None
            )
            if hash_target is None or not hash_target.is_file():
                checks.append(
                    Check(
                        "error",
                        "assets.hash_target",
                        f"{artifact_id}: hash target is missing or outside the repository.",
                    )
                )
            elif not isinstance(expected_hash, str) or len(expected_hash) != 64:
                checks.append(
                    Check(
                        "error",
                        "assets.hash_metadata",
                        f"{artifact_id}: expected SHA-256 is invalid.",
                    )
                )
            else:
                actual_hash = sha256(hash_target)
                if actual_hash.casefold() != expected_hash.casefold():
                    checks.append(
                        Check(
                            "error",
                            "assets.hash_mismatch",
                            f"{artifact_id}: SHA-256 does not match the inventory.",
                        )
                    )
                else:
                    checks.append(
                        Check("ok", "assets.hash_match", f"{artifact_id}: SHA-256 matches.")
                    )

    return checks


def check_runtime(imports: dict[str, str] | None = None) -> list[Check]:
    checks: list[Check] = []
    requested = imports if imports is not None else {**RUNTIME_IMPORTS, **PLATFORM_IMPORTS}
    for module, display_name in requested.items():
        if importlib.util.find_spec(module) is None:
            checks.append(
                Check("error", "runtime.import_missing", f"{display_name} ({module}) is missing.")
            )
        else:
            checks.append(
                Check("ok", "runtime.import_present", f"{display_name} ({module}) is available.")
            )
    return checks


def exit_code(checks: Iterable[Check]) -> int:
    return 1 if any(check.level == "error" for check in checks) else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--assets-only",
        action="store_true",
        help="validate Python and bundled assets, but not installed runtime packages",
    )
    scope.add_argument(
        "--runtime-only",
        action="store_true",
        help="validate Python and installed runtime packages, but not bundled assets",
    )
    parser.add_argument(
        "--check-hashes",
        action="store_true",
        help="calculate SHA-256 for inventory entries that provide a digest",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help=f"asset manifest path (default: <repository>/{DEFAULT_MANIFEST})",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    manifest_path = args.manifest or root / DEFAULT_MANIFEST

    checks = check_python()
    if not args.runtime_only:
        checks.extend(check_assets(root, manifest_path, args.check_hashes))
    if not args.assets_only:
        checks.extend(check_runtime())

    if args.json:
        print(json.dumps([asdict(check) for check in checks], indent=2))
    else:
        for check in checks:
            print(f"[{check.level.upper():7}] {check.message}")
        totals = {
            level: sum(check.level == level for check in checks)
            for level in ("ok", "warning", "error")
        }
        print(
            "Summary: "
            f"{totals['ok']} passed, {totals['warning']} warnings, {totals['error']} errors"
        )
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
