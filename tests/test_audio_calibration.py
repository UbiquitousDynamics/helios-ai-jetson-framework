"""Fictional profile identities, never acoustic release evidence."""

import json
from dataclasses import asdict, replace

import pytest

from recognizer.calibration import (
    AudioIdentity,
    CalibrationError,
    load_profile,
    selected_calibration,
    pulse_audio_identity,
)


@pytest.fixture
def identity():
    return AudioIdentity(
        "test-debian",
        "test",
        "test-card",
        "test-port",
        "a" * 64,
        "b" * 64,
        16000,
        "mono",
        30,
        12,
    )


@pytest.fixture
def profile_path(tmp_path, identity):
    path = tmp_path / "local.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "fictional",
                "status": "candidate",
                "identity": asdict(identity),
            }
        )
    )
    return path


def test_no_selection_does_not_probe_identity():
    def unavailable():
        pytest.fail("Clean checkout must not probe hardware")

    assert selected_calibration({}, unavailable) is None


def test_selected_candidate_matches_but_never_authorizes(profile_path, identity):
    selected = selected_calibration(
        {"HELIOS_AUDIO_CALIBRATION_CONFIG": str(profile_path)}, lambda: identity
    )
    assert selected.require_match().identity == identity
    assert selected.calibration_id == ""
    assert selected.accepts(object(), after=0) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("deployment_id", "other-jetson"),
        ("input_source", "pulse:usb"),
        ("hardware_id", "other-card"),
        ("active_port", "headset"),
        ("model_sha256", "b" * 64),
        ("sample_rate_hz", 48000),
        ("channel_mode", "stronger"),
        ("capture_gain_db", 10),
        ("microphone_boost_db", 36),
    ],
)
def test_every_identity_change_invalidates_selection(profile_path, identity, field, value):
    with pytest.raises(CalibrationError, match="does not match"):
        selected_calibration(
            {"HELIOS_AUDIO_CALIBRATION_CONFIG": str(profile_path)},
            lambda: replace(identity, **{field: value}),
        )


def test_identity_is_rechecked_after_selection(profile_path, identity):
    current = [identity]
    selected = selected_calibration(
        {"HELIOS_AUDIO_CALIBRATION_CONFIG": str(profile_path)}, lambda: current[0]
    )
    current[0] = replace(identity, microphone_boost_db=36)
    with pytest.raises(CalibrationError):
        selected.require_match()


def test_unavailable_identity_fails_closed(profile_path):
    def unavailable():
        raise OSError("No device")

    with pytest.raises(CalibrationError, match="unavailable"):
        selected_calibration({"HELIOS_AUDIO_CALIBRATION_CONFIG": str(profile_path)}, unavailable)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "verified"},
        {"schema_version": True},
        {"schema_version": 2},
        {"profile_id": ""},
        {"threshold": 0.01},
        {"identity": {}},
        {"identity": []},
    ],
)
def test_unapproved_or_malformed_profiles_rejected(profile_path, change):
    data = json.loads(profile_path.read_text())
    data.update(change)
    profile_path.write_text(json.dumps(data))
    with pytest.raises(CalibrationError):
        load_profile(profile_path)


@pytest.mark.parametrize(
    "payload", ['{"status":"candidate","status":"verified"}', "[]", "{", " " * 16385]
)
def test_duplicate_invalid_or_oversized_json_rejected(profile_path, payload):
    profile_path.write_text(payload)
    with pytest.raises(CalibrationError):
        load_profile(profile_path)


def test_missing_selected_file_never_falls_back(tmp_path, identity):
    with pytest.raises(CalibrationError):
        selected_calibration(
            {"HELIOS_AUDIO_CALIBRATION_CONFIG": str(tmp_path / "missing")}, lambda: identity
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("capture_gain_db", float("nan")),
        ("microphone_boost_db", float("inf")),
        ("sample_rate_hz", True),
        ("hardware_id", ""),
        ("model_sha256", "model-name"),
    ],
)
def test_invalid_identity_metadata_rejected(identity, field, value):
    with pytest.raises(CalibrationError):
        replace(identity, **{field: value})


@pytest.fixture
def linux_probe(tmp_path):
    machine = tmp_path / "machine-id"
    machine.write_text("fictional-machine")
    model = tmp_path / "model"
    model.mkdir()
    (model / "weights").write_bytes(b"fictional-model")
    source = {
        "name": "pulse:test",
        "mute": False,
        "monitor_source": "",
        "active_port": "internal-mic",
        "volume": {"mono": {"value": 65536}},
        "properties": {
            "alsa.card": "1",
            "device.bus_path": "pci-test",
            "alsa.card_name": "test-card",
            "alsa.id": "test-input",
        },
    }
    mixer = (
        "Simple mixer control 'Capture',0\n Mono: Capture 1 [30.00dB]\n"
        "Simple mixer control 'Internal Mic Boost',0\n Mono: Capture 1 [12.00dB]\n"
    )
    state = {"sources": [source], "mixer": mixer}
    commands = []

    def command(args):
        commands.append(args)
        return json.dumps(state["sources"]) if args[0] == "pactl" else state["mixer"]

    def probe():
        return pulse_audio_identity(
            "pulse:test",
            model,
            sample_rate_hz=16000,
            channel_mode="mono",
            machine_id_path=machine,
            command=command,
        )

    return probe, state, commands, machine, model


def test_linux_probe_binds_real_metadata_without_mutation(linux_probe):
    probe, state, commands, _, _ = linux_probe
    observed = probe()
    assert observed.capture_gain_db == 30
    assert observed.microphone_boost_db == 12
    assert commands == [
        ["pactl", "--format=json", "list", "sources"],
        ["amixer", "-c", "1", "scontents"],
    ]
    state["sources"][0]["properties"]["alsa.card"] = "2"
    assert probe() == observed  # Volatile card index is not a calibration identity.


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "muted", "monitor", "no_gain", "asymmetric", "bad_card"]
)
def test_linux_probe_fails_on_ambiguous_or_unsafe_metadata(linux_probe, mutation):
    probe, state, _, _, _ = linux_probe
    source = state["sources"][0]
    if mutation == "missing":
        state["sources"] = []
    elif mutation == "duplicate":
        state["sources"].append(dict(source))
    elif mutation == "muted":
        source["mute"] = True
    elif mutation == "monitor":
        source["monitor_source"] = "speaker"
    elif mutation == "no_gain":
        state["mixer"] = "unknown"
    elif mutation == "asymmetric":
        state["mixer"] += " Mono: Capture [6.00dB]\n"
    elif mutation == "bad_card":
        source["properties"]["alsa.card"] = "not-a-card"
    with pytest.raises(CalibrationError):
        probe()


def test_model_machine_and_agc_changes_invalidate_linux_identity(linux_probe):
    probe, state, _, machine, model = linux_probe
    original = probe()
    (model / "weights").write_bytes(b"changed-model")
    assert probe().model_sha256 != original.model_sha256
    machine.write_text("another-machine")
    assert probe().deployment_id != original.deployment_id
    state["mixer"] += "Simple mixer control 'Auto Gain Control',0\n Mono: Playback [off]\n"
    assert probe().mixer_sha256 != original.mixer_sha256


def test_candidate_cli_selection_and_no_overwrite(tmp_path, identity, capsys):
    from scripts.audio_calibration import main

    path = tmp_path / "candidate.json"

    def probe(*args, **kwargs):
        return identity

    args = ["--source", "pulse:test", "--model", "fake-model"]
    assert (
        main(
            args + ["--candidate-output", str(path), "--profile-id", "test"],
            environment={},
            probe=probe,
        )
        == 0
    )
    original = path.read_bytes()
    assert (
        main(
            args + ["--candidate-output", str(path), "--profile-id", "other"],
            environment={},
            probe=probe,
        )
        == 1
    )
    assert path.read_bytes() == original
    assert main(args, environment={"HELIOS_AUDIO_CALIBRATION_CONFIG": str(path)}, probe=probe) == 0
    assert '"voice_writes_enabled": false' in capsys.readouterr().out


def test_cli_no_selection_does_not_probe():
    from scripts.audio_calibration import main

    def probe(*args, **kwargs):
        pytest.fail("Unexpected hardware access")

    assert main([], environment={}, probe=probe) == 0


def test_candidate_cannot_arm_existing_action_controller(profile_path, identity):
    import anyio
    from automation.voice import VoiceActionState
    from tests.test_automation_voice import controller, propose

    voice, _, executor = controller(verifier=False)
    voice.verifier = selected_calibration(
        {"HELIOS_AUDIO_CALIBRATION_CONFIG": str(profile_path)}, lambda: identity
    )
    anyio.run(propose, voice)
    assert voice.state == VoiceActionState.CLARIFICATION
    assert voice.pending is None
    assert executor.calls == []


def runtime_recognizer(profile_path, identity, probe=None):
    from recognizer.speech_recognizer import SpeechRecognizer
    from tests.test_recognizer import FakeAudio, FinalRecognizer

    class PulseAudio(FakeAudio):
        def get_device_count(self):
            return 1

        def get_device_info_by_index(self, index):
            return {"name": "pulse", "maxInputChannels": 2, "defaultSampleRate": 16000}

    audio = PulseAudio()
    return SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=FinalRecognizer,
        input_device="pulse:test",
        input_device_strict=True,
        pulse_sources=lambda: ("test",),
        calibration_path=profile_path,
        calibration_probe=probe or (lambda: identity),
        calibration_stream_probe=lambda: None,
    ), audio


def test_runtime_selected_profile_checks_open_and_final(profile_path, identity):
    calls = []

    def probe():
        calls.append(True)
        return identity

    recognizer, audio = runtime_recognizer(profile_path, identity, probe)
    events = recognizer.listen_events()
    try:
        assert next(events).is_final
        assert len(calls) == 2
        assert audio.open_kwargs["channels"] == 1
        assert audio.open_kwargs["rate"] == 16000
    finally:
        events.close()
        recognizer.close()
    assert audio.stream.closed


def test_runtime_identity_change_blocks_final_before_observer(profile_path, identity):
    from recognizer.speech_recognizer import SpeechRecognitionError

    identities = iter((identity, replace(identity, microphone_boost_db=36)))
    recognizer, audio = runtime_recognizer(profile_path, identity, lambda: next(identities))
    observed = []
    try:
        with pytest.raises(SpeechRecognitionError, match="does not match"):
            next(recognizer.listen_events(on_frame=lambda *args: observed.append(args)))
    finally:
        recognizer.close()
    assert observed == []
    assert audio.stream.closed


def test_runtime_invalid_identity_closes_stream_at_open(profile_path, identity):
    from recognizer.speech_recognizer import SpeechRecognitionError

    recognizer, audio = runtime_recognizer(
        profile_path, identity, lambda: replace(identity, deployment_id="other-machine")
    )
    try:
        with pytest.raises(SpeechRecognitionError):
            next(recognizer.listen_events())
    finally:
        recognizer.close()
    assert audio.stream.closed


@pytest.mark.parametrize("device,strict", [(None, True), (0, True), ("pulse:test", False)])
def test_runtime_profile_requires_strict_explicit_source(profile_path, device, strict):
    from recognizer.speech_recognizer import SpeechRecognizer

    with pytest.raises(ValueError, match="explicit strict"):
        SpeechRecognizer(
            model=object(),
            input_device=device,
            input_device_strict=strict,
            calibration_path=profile_path,
        )


@pytest.mark.parametrize(
    "field,value",
    [("sample_rate_hz", 48000), ("channel_mode", "stronger"), ("input_source", "other")],
)
def test_runtime_profile_rejects_different_configured_format(profile_path, identity, field, value):
    data = json.loads(profile_path.read_text())
    data["identity"] = asdict(replace(identity, **{field: value}))
    profile_path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="capture format/source"):
        runtime_recognizer(profile_path, identity)


def test_settings_profile_selection_is_explicit_and_project_rooted(tmp_path):
    import config

    assert (
        config.Settings.from_env(project_root=tmp_path, environ={}).audio_calibration_config is None
    )
    selected = config.Settings.from_env(
        environ={"HELIOS_AUDIO_CALIBRATION_CONFIG": "local-candidate.json"}, project_root=tmp_path
    )
    assert selected.audio_calibration_config == tmp_path / "local-candidate.json"


@pytest.mark.parametrize(
    "mutation",
    [None, "route", "rate", "gain", "muted", "corked", "other_pid", "duplicate", "no_source"],
)
def test_opened_pulse_stream_route_and_format_are_verified(mutation):
    from recognizer.calibration import verify_pulse_stream

    sources = [{"name": "test", "index": 17}]
    stream = {
        "source": 17,
        "sample_specification": "s16le 1ch 16000Hz",
        "mute": False,
        "corked": False,
        "volume": {"mono": {"value": 65536}},
        "properties": {"application.process.id": "123"},
    }
    streams = [stream]
    if mutation == "route":
        stream["source"] = 18
    elif mutation == "rate":
        stream["sample_specification"] = "s16le 1ch 48000Hz"
    elif mutation == "gain":
        stream["volume"]["mono"]["value"] = 60000
    elif mutation == "muted":
        stream["mute"] = True
    elif mutation == "corked":
        stream["corked"] = True
    elif mutation == "other_pid":
        stream["properties"]["application.process.id"] = "456"
    elif mutation == "duplicate":
        streams.append(dict(stream))
    elif mutation == "no_source":
        sources.clear()

    def command(args):
        return json.dumps(streams if args[-1] == "source-outputs" else sources)

    if mutation is None:
        verify_pulse_stream(
            "test", sample_rate_hz=16000, channels=1, process_id=123, command=command
        )
    else:
        with pytest.raises(CalibrationError):
            verify_pulse_stream(
                "test", sample_rate_hz=16000, channels=1, process_id=123, command=command
            )


def test_runtime_flush_rechecks_before_final_observer(profile_path, identity):
    from recognizer.speech_recognizer import SpeechRecognitionError
    from recognizer.turn_endpoint_detector import EndpointAction
    from tests.test_recognizer import PendingRecognizer

    identities = iter((identity, replace(identity, deployment_id="changed")))
    recognizer, audio = runtime_recognizer(profile_path, identity, lambda: next(identities))
    recognizer._recognizer_factory = PendingRecognizer
    observed = []

    def observe(result, energy):
        observed.append(result)
        return EndpointAction.REQUEST_FINAL_RESULT

    events = recognizer.listen_events(on_frame=observe)
    try:
        assert next(events).is_final is False
        with pytest.raises(SpeechRecognitionError):
            next(events)
    finally:
        events.close()
        recognizer.close()
    assert len(observed) == 1
    assert observed[0].is_final is False
    assert audio.stream.closed


def test_runtime_route_change_blocks_result_even_when_source_identity_matches(
    profile_path, identity
):
    from recognizer.speech_recognizer import SpeechRecognitionError

    recognizer, audio = runtime_recognizer(profile_path, identity)

    def invalid_route():
        raise CalibrationError("Stream moved")

    recognizer._calibration_stream_probe = invalid_route
    try:
        with pytest.raises(SpeechRecognitionError):
            next(recognizer.listen_events())
    finally:
        recognizer.close()
    assert audio.stream.closed
