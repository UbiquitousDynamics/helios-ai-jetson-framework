from __future__ import annotations

import pytest

from api.providers.contracts import ChatMessage, ChatRequest, PrivacyLevel, Role
from api.routing import Connectivity, NoRouteError, ProviderTarget, RoutePlanner, RoutingPolicy
from audio.tts import PiperTTS


class TinyVoice:
    def synthesize(self, _text: str, wav_file: object) -> None:
        wav_file.setnchannels(1)  # type: ignore[attr-defined]
        wav_file.setsampwidth(2)  # type: ignore[attr-defined]
        wav_file.setframerate(16_000)  # type: ignore[attr-defined]
        wav_file.writeframes(b"\x00\x00\x01\x00")  # type: ignore[attr-defined]


class NoopAudio:
    def play(self, *_args: object) -> None:
        return None


def test_piper_reports_synthesis_playback_and_actual_audio_boundary() -> None:
    observed = iter([10.0, 10.01, 10.01, 10.03])
    tts = PiperTTS(
        "unused.onnx",
        voice=TinyVoice(),
        audio_backend=NoopAudio(),
        clock=lambda: next(observed),
    )

    timing = tts.speak_with_timing("hello")

    assert timing is not None
    assert timing.synthesis_ms == pytest.approx(10.0)
    assert timing.audio_started_at == 10.01
    assert timing.playback_ms == pytest.approx(20.0)
    assert timing.audio_duration_ms == pytest.approx(0.125)


def test_piper_legacy_speak_keeps_none_return_value() -> None:
    tts = PiperTTS(
        "unused.onnx",
        voice=TinyVoice(),
        audio_backend=NoopAudio(),
    )

    assert tts.speak("hello") is None


def test_route_diagnostics_expose_tier_rejections_and_network_forcing() -> None:
    local = ProviderTarget(
        "local",
        "ollama",
        "local-model",
        False,
        tier="edge",
    )
    remote_small = ProviderTarget(
        "remote-small",
        "remote",
        "small-model",
        True,
        min_complexity_score=0,
        tier="small",
    )
    remote_large = ProviderTarget(
        "remote-large",
        "remote",
        "large-model",
        True,
        min_complexity_score=3,
        tier="large",
    )
    planner = RoutePlanner(
        (remote_small, remote_large, local),
        policy=RoutingPolicy.AUTO,
        auto_complexity_threshold=2,
    )
    request = ChatRequest(
        model="requested",
        messages=(ChatMessage(Role.USER, "explain this"),),
        mode="think",
        language="en",
        privacy=PrivacyLevel.REMOTE_ALLOWED,
        remote_authorized=True,
    )

    online = planner.plan_detailed(request, connectivity=Connectivity.ONLINE)
    offline = planner.plan_detailed(request, connectivity=Connectivity.OFFLINE)

    assert online.complexity_score is not None
    assert online.selected_tier in {"small", "large"}
    assert any(rejection.reason == "adaptive_tier" for rejection in online.rejections)
    assert offline.targets == (local,)
    assert offline.network_forced_local is True
    assert offline.reason == "network.offline.local"
    assert any(rejection.reason == "network_offline" for rejection in offline.rejections)


def test_non_auto_routing_and_no_route_errors_preserve_rejection_diagnostics() -> None:
    local = ProviderTarget("local", "ollama", "local-model", False)
    remote = ProviderTarget("remote", "remote", "remote-model", True)
    request = ChatRequest(
        model="requested",
        messages=(ChatMessage(Role.USER, "explain this"),),
        mode="talk",
        language="en",
        privacy=PrivacyLevel.REMOTE_ALLOWED,
        remote_authorized=True,
    )

    remote_first = RoutePlanner((remote, local), policy=RoutingPolicy.REMOTE_FIRST)
    decision = remote_first.plan_detailed(request, connectivity=Connectivity.OFFLINE)

    assert decision.targets == (local,)
    assert decision.network_forced_local is True
    assert decision.reason == "network.offline.local"

    remote_only = RoutePlanner((remote,), policy=RoutingPolicy.REMOTE_ONLY)
    with pytest.raises(NoRouteError) as captured:
        remote_only.plan_detailed(
            ChatRequest(
                model="requested",
                messages=(ChatMessage(Role.USER, "private"),),
                mode="talk",
                language="en",
                privacy=PrivacyLevel.LOCAL_ONLY,
                remote_authorized=False,
            ),
            connectivity=Connectivity.ONLINE,
        )

    assert captured.value.decision is not None
    assert captured.value.decision.targets == ()
    assert [item.reason for item in captured.value.decision.rejections] == ["privacy"]
