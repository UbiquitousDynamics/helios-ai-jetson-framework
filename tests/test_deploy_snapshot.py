import json
import subprocess

import pytest

from runtime_identity import STAMP_NAME, read_identity
from scripts import deploy_snapshot as deployment


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "app.py").write_text("original\n")
    (root / "obsolete.py").write_text("old\n")
    (root / ".gitignore").write_text(".env\n")

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, check=True, timeout=5
        )

    git("init")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
    return root


@pytest.mark.parametrize("dirty", [False, True])
def test_copy_and_stamp_match_source_without_copying_git_or_secrets(source, tmp_path, dirty):
    if dirty:
        (source / "app.py").write_text("edited\n")
        (source / "obsolete.py").unlink()
        (source / "untracked.py").write_text("not deployed\n")
    (source / ".env").write_text("secret\n")
    target = tmp_path / "release"
    stamp = deployment.deploy_snapshot(source, target)
    assert read_identity(target) == read_identity(source)
    assert read_identity(target)[1] is dirty
    assert (target / "app.py").read_bytes() == (source / "app.py").read_bytes()
    assert (target / "obsolete.py").exists() is (not dirty)
    assert not (target / ".git").exists()
    assert not (target / ".env").exists()
    assert not (target / "untracked.py").exists()
    assert json.loads(stamp.read_text())["files"]["app.py"]
    (target / "app.py").write_text("changed after deployment\n")
    assert read_identity(target) is None


def test_existing_release_and_nested_destination_are_rejected(source, tmp_path):
    target = tmp_path / "release"
    target.mkdir()
    stamp = target / STAMP_NAME
    stamp.write_text("existing stamp")
    with pytest.raises(FileExistsError):
        deployment.deploy_snapshot(source, target)
    assert stamp.read_text() == "existing stamp"
    with pytest.raises(ValueError, match="outside"):
        deployment.deploy_snapshot(source, source / "release")
    assert not (source / "release").exists()


@pytest.mark.parametrize("failure", ["copy", "source_change", "corrupt_copy"])
def test_failed_sync_never_publishes_stamp(source, tmp_path, monkeypatch, failure):
    target = tmp_path / "release"
    copy = deployment.shutil.copy2

    def interrupted_copy(src, dst):
        if failure == "copy":
            raise OSError("injected disk error")
        copy(src, dst)
        if failure == "source_change":
            (source / "app.py").write_text("concurrent edit\n")
        else:
            dst.write_text("corrupted copy\n")

    monkeypatch.setattr(deployment.shutil, "copy2", interrupted_copy)
    with pytest.raises((OSError, ValueError)):
        deployment.deploy_snapshot(source, target)
    assert not (target / STAMP_NAME).exists()
    assert read_identity(target) is None


def test_source_must_be_repository_root(source, tmp_path):
    nested = source / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="working-tree root"):
        deployment.deploy_snapshot(nested, tmp_path / "release")


def test_index_link_is_rejected_even_on_hosts_without_symlink_support(source, tmp_path):
    blob = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD:app.py"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(source), "update-index", "--cacheinfo", f"120000,{blob},app.py"],
        capture_output=True,
        check=True,
        timeout=5,
    )
    target = tmp_path / "release"
    with pytest.raises(ValueError, match="symlinks"):
        deployment.deploy_snapshot(source, target)
    assert not target.exists()


@pytest.mark.parametrize("files", [{}, [], {"../outside.py": "a" * 64}, {"missing.py": "a" * 64}])
def test_invalid_or_missing_manifest_files_fail_closed(tmp_path, files):
    (tmp_path / STAMP_NAME).write_text(
        json.dumps({"commit": "a" * 40, "dirty": False, "files": files})
    )
    assert read_identity(tmp_path) is None
