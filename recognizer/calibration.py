"""Explicit deployment-local calibration selection; never configures audio or actions."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


class CalibrationError(ValueError):
    """A selected local profile cannot be trusted for the current audio path."""


@dataclass(frozen=True, slots=True)
class AudioIdentity:
    deployment_id: str
    input_source: str
    hardware_id: str
    active_port: str
    model_sha256: str
    mixer_sha256: str
    sample_rate_hz: int
    channel_mode: str
    capture_gain_db: float
    microphone_boost_db: float

    def __post_init__(self):
        for field in ("deployment_id", "input_source", "hardware_id", "active_port"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field).strip():
                raise CalibrationError("Incomplete audio identity")
        for digest in (self.model_sha256, self.mixer_sha256):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise CalibrationError("Invalid model or mixer digest")
        if type(self.sample_rate_hz) is not int or self.sample_rate_hz <= 0:
            raise CalibrationError("Invalid sample rate")
        if self.channel_mode not in {"mono", "average", "sum", "stronger"}:
            raise CalibrationError("Invalid channel mode")
        for gain in (self.capture_gain_db, self.microphone_boost_db):
            if type(gain) not in (int, float) or not math.isfinite(gain):
                raise CalibrationError("Invalid gain")


@dataclass(frozen=True, slots=True)
class CalibrationProfile:
    profile_id: str
    identity: AudioIdentity
    artifact_sha256: str
    # This format deliberately cannot represent acoustic approval yet.
    status: str = "candidate"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CalibrationError("Duplicate profile field")
        result[key] = value
    return result


def load_profile(path: str | Path) -> CalibrationProfile:
    """Load bounded candidate data only; no thresholds, gain or authorization changes."""
    try:
        with Path(path).open("rb") as handle:
            payload = handle.read(16385)
        if len(payload) > 16384:
            raise CalibrationError("Profile too large")
        data = json.loads(payload, object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or set(data) != {
            "schema_version",
            "profile_id",
            "status",
            "identity",
        }:
            raise CalibrationError("Invalid profile fields")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise CalibrationError("Unsupported profile version")
        if data["status"] != "candidate":
            raise CalibrationError("Acoustic approval is not supported by this profile format")
        if not isinstance(data["profile_id"], str) or not data["profile_id"].strip():
            raise CalibrationError("Missing profile identifier")
        if not isinstance(data["identity"], dict):
            raise CalibrationError("Invalid profile identity")
        identity = AudioIdentity(**data["identity"])
        return CalibrationProfile(data["profile_id"], identity, hashlib.sha256(payload).hexdigest())
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise CalibrationError("Cannot load local calibration profile") from exc


class DeviceBoundCalibration:
    """Recheck actual injectable runtime identity; snapshots are never cached as approval."""

    def __init__(self, profile: CalibrationProfile, identity: Callable[[], AudioIdentity]):
        self.profile = profile
        self._identity = identity

    def require_match(self) -> CalibrationProfile:
        try:
            current = self._identity()
        except Exception as exc:
            raise CalibrationError("Current audio identity unavailable") from exc
        if not isinstance(current, AudioIdentity) or current != self.profile.identity:
            raise CalibrationError("Calibration does not match current device and audio settings")
        return self.profile

    @property
    def calibration_id(self) -> str:
        # Candidate evidence must never arm VoiceActionController.
        return ""

    def accepts(self, recognition, *, after: float) -> bool:
        del recognition, after
        return False


def selected_calibration(
    environment: Mapping[str, str], identity: Callable[[], AudioIdentity]
) -> DeviceBoundCalibration | None:
    """Explicit selection only; invalid selected profiles fail rather than falling back."""
    path = environment.get("HELIOS_AUDIO_CALIBRATION_CONFIG", "").strip()
    if not path:
        return None
    selected = DeviceBoundCalibration(load_profile(path), identity)
    selected.require_match()
    return selected


def _command(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=5).stdout


def _model_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        raise CalibrationError("Model files unavailable")
    for path in files:
        name = path.relative_to(root).as_posix().encode()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        content = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1048576), b""):
                content.update(chunk)
        digest.update(content.digest())
    return digest.hexdigest()


def pulse_audio_identity(
    source: str,
    model_path: Path,
    *,
    sample_rate_hz: int,
    channel_mode: str,
    machine_id_path: Path = Path("/etc/machine-id"),
    command: Callable[[list[str]], str] = _command,
) -> AudioIdentity:
    """Read Linux metadata only. Unsupported/ambiguous capture controls fail closed."""
    try:
        listing = json.loads(command(["pactl", "--format=json", "list", "sources"]))
        matches = [item for item in listing if item["name"] == source]
        if len(matches) != 1:
            raise CalibrationError("Exact audio source unavailable or ambiguous")
        device = matches[0]
        properties = device["properties"]
        if device.get("monitor_source") or device.get("mute") is not False:
            raise CalibrationError("Monitor or muted source cannot be calibrated")
        card = properties["alsa.card"]
        if not isinstance(card, str) or not card.isdecimal():
            raise CalibrationError("Invalid ALSA card")
        mixer = command(["amixer", "-c", card, "scontents"])

        def control_gain(names, *, optional=False):
            for name in names:
                match = re.search(
                    r"Simple mixer control '"
                    + re.escape(name)
                    + r"',0\n(.*?)(?=Simple mixer control|\Z)",
                    mixer,
                    re.S,
                )
                if match:
                    values = re.findall(r"\[(-?\d+(?:\.\d+)?)dB\]", match[1])
                    if values and len(set(values)) == 1:
                        return float(values[0])
                    raise CalibrationError("Unknown or asymmetric capture gain")
            if optional:
                return 0.0
            raise CalibrationError("Capture gain control unavailable")

        hardware = {
            key: properties[key] for key in ("device.bus_path", "alsa.card_name", "alsa.id")
        }
        if not all(isinstance(v, str) and v.strip() for v in hardware.values()):
            raise CalibrationError("Hardware identity unavailable")
        mixer_state = json.dumps(
            {"controls": mixer, "volume": device["volume"], "mute": device["mute"]}, sort_keys=True
        ).encode()
        machine = machine_id_path.read_bytes().strip()
        if not machine:
            raise CalibrationError("Deployment identity unavailable")
        return AudioIdentity(
            hashlib.sha256(b"helios-audio-calibration-v1:" + machine).hexdigest(),
            source,
            json.dumps(hardware, sort_keys=True),
            device["active_port"],
            _model_digest(Path(model_path)),
            hashlib.sha256(mixer_state).hexdigest(),
            sample_rate_hz,
            channel_mode,
            control_gain(("Capture", "Mic")),
            control_gain(("Internal Mic Boost",), optional=True),
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise CalibrationError("Live Linux audio identity unavailable") from exc
