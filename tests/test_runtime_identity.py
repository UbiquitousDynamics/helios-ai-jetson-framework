import json

import pytest

import runtime_identity


def test_stamp_round_trip_and_invalid_stamp_falls_back(tmp_path, monkeypatch):
    source, deployment = tmp_path / "source", tmp_path / "deployment"
    source.mkdir()
    deployment.mkdir()
    sha = "a" * 40
    monkeypatch.setattr(runtime_identity, "git_identity", lambda root: (sha, True))
    stamp = runtime_identity.write_stamp(source, deployment)
    assert json.loads(stamp.read_text()) == {"commit": sha, "dirty": True}
    assert runtime_identity.read_identity(deployment) == (sha, True)
    stamp.write_text('{"commit":"invalid","dirty":false}')
    assert runtime_identity.read_identity(deployment) == (sha, True)
    with pytest.raises(ValueError):
        runtime_identity.write_stamp(source, tmp_path / "missing")


def test_unknown_identity_and_bad_git_output(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_identity, "git_identity", lambda root: None)
    assert runtime_identity.read_identity(tmp_path) is None
    with pytest.raises(ValueError):
        runtime_identity.write_stamp(tmp_path, tmp_path)
