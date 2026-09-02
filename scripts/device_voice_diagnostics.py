"""Run a bounded, privacy-conscious voice-runtime diagnostic on a Jetson.

The suite is intentionally a diagnostic runner rather than a second production
entry point.  It exercises the same adapters used by Helios, records metadata
instead of transcripts, and keeps live probes bounded so a failed provider
cannot make the diagnostic hang indefinitely.

Examples::

    python scripts/device_voice_diagnostics.py --repetitions 5 --play-tts \
        --app-log /home/emilia/helios-branch-test-codex/logs/app.log

The command exits with status 1 when a required case fails.  Hardware and live
checks can be skipped explicitly when the device is in use.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import logging
import os
import platform
import re
import sys
import threading
import time
import traceback
import wave
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


LOGGER = logging.getLogger("helios.device_voice_diagnostics")
_INTERESTING_LOG = re.compile(
    r"(llm_route_selected|Completed (talk|think) request|worker_error|thread_resume|"
    r"stream_worker|Unable to stream|fallback|Network gate state|voice_listen_completed|"
    r"barge_in|assistant_turn_interrupted|Audio output underflow|tts_failed|"
    r"turn_completion_failed)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(r"(?i)(password|token|api[_-]?key)=\S+")
_COMPLETED_ROUTE = re.compile(r"Completed (?:talk|think) request using route ([A-Za-z0-9_-]+)")
_LOCAL_FALLBACK_INTERACTIVE_TARGET_SECONDS = 15.0
_LOCAL_FALLBACK_MAX_FIRST_TOKEN_SECONDS = 30.0
_LOCAL_FALLBACK_MAX_TOTAL_SECONDS = 35.0
_LOCAL_FALLBACK_HISTORY_TURNS = 10


@dataclass
class CaseResult:
    case: str
    stage: str
    status: str
    duration_ms: float
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    traceback: str | None = None


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = _redact(record.getMessage())
        except Exception:
            message = record.msg if isinstance(record.msg, str) else repr(record.msg)
        if _INTERESTING_LOG.search(message):
            self.lines.append(message)


def _redact(value: str) -> str:
    return _SECRET_VALUE.sub(r"\1=<redacted>", value)


def _exception_text(error: BaseException) -> str:
    chain: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).strip()
        chain.append(type(current).__name__ + (f": {message}" if message else ""))
        current = current.__cause__ or current.__context__
    return " <- ".join(_redact(item) for item in chain)


def _completed_route(lines: list[str]) -> str | None:
    """Return the final completed route from content-free client logs."""

    for line in reversed(lines):
        match = _COMPLETED_ROUTE.search(line)
        if match is not None:
            return match.group(1)
    return None


def _persistent_codex_auth_file() -> Path:
    from api.providers.codex_session import persistent_codex_auth_home

    source_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    return persistent_codex_auth_home(source_home) / "auth.json"


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Counter):
        return dict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if dataclasses.is_dataclass(value):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class DiagnosticSuite:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = Path(args.repo_root).expanduser().resolve()
        self.results: list[CaseResult] = []
        self.settings: Any | None = None
        self.client: Any | None = None
        self._log_capture = _LogCapture()

    def record(
        self,
        case: str,
        stage: str,
        operation: Callable[[], Mapping[str, Any] | None],
    ) -> CaseResult:
        started = time.monotonic()
        try:
            raw_details = operation() or {}
            details = dict(raw_details)
            status = str(details.pop("_status", "pass"))
            result = CaseResult(
                case=case,
                stage=stage,
                status=status,
                duration_ms=round((time.monotonic() - started) * 1_000, 1),
                details=details,
            )
        except Exception as error:  # noqa: BLE001 - diagnostic boundary
            result = CaseResult(
                case=case,
                stage=stage,
                status="fail",
                duration_ms=round((time.monotonic() - started) * 1_000, 1),
                error=_exception_text(error),
                traceback=traceback.format_exc() if self.args.verbose else None,
            )
        self.results.append(result)
        LOGGER.info(
            "case=%s stage=%s status=%s duration_ms=%s error=%s",
            result.case,
            result.stage,
            result.status,
            result.duration_ms,
            result.error or "none",
        )
        return result

    def run(self) -> dict[str, Any]:
        self.record("environment", "startup", self._environment)
        self.record("configuration", "startup", self._configuration)
        self.record("runtime_imports", "startup", self._runtime_imports)
        self.record("route_matrix", "routing", self._route_matrix)
        self.record("network_quality", "connectivity", self._network_quality)

        if self.args.skip_audio:
            self.results.append(
                CaseResult("audio_devices", "audio", "skip", 0.0, {"reason": "--skip-audio"})
            )
            self.results.append(
                CaseResult("microphone_capture", "audio", "skip", 0.0, {"reason": "--skip-audio"})
            )
            self.results.append(
                CaseResult("tts_synthesis", "audio", "skip", 0.0, {"reason": "--skip-audio"})
            )
        else:
            self.record("audio_devices", "audio", self._audio_devices)
            self.record("microphone_capture", "audio", self._microphone_capture)
            self.record("tts_synthesis", "audio", self._tts_synthesis)

        self.record("command_matrix", "orchestration", self._command_matrix)

        remote_live_cases = (
            "remote_preparation",
            "codex_resume_probe",
            "live_provider_matrix",
            "live_cancellation",
        )
        if self.args.skip_live or self.args.skip_remote_live:
            reason = "--skip-live" if self.args.skip_live else "--skip-remote-live"
            for case in remote_live_cases:
                self.results.append(CaseResult(case, "provider", "skip", 0.0, {"reason": reason}))
        else:
            self.record("remote_preparation", "provider", self._remote_preparation)
            self.record("codex_resume_probe", "provider", self._codex_resume_probe)
            self.record("live_provider_matrix", "provider", self._live_provider_matrix)
            self.record("live_cancellation", "provider", self._live_cancellation)

        if self.args.skip_live:
            self.results.append(
                CaseResult(
                    "bounded_local_fallback",
                    "provider",
                    "skip",
                    0.0,
                    {"reason": "--skip-live"},
                )
            )
        else:
            self.record("bounded_local_fallback", "provider", self._bounded_local_fallback)

        if self.args.app_log:
            self.record(
                "application_log_analysis",
                "observability",
                lambda: analyze_log(Path(self.args.app_log)),
            )
        if self.args.metrics:
            self.record(
                "metrics_analysis",
                "observability",
                lambda: analyze_metrics(Path(self.args.metrics)),
            )

        self._close_client()
        counts = Counter(result.status for result in self.results)
        return {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "host": platform.node(),
            "repository": str(self.root),
            "python": sys.version,
            "arguments": {
                key: value for key, value in vars(self.args).items() if key not in {"verbose"}
            },
            "summary": dict(counts),
            "results": [asdict(result) for result in self.results],
        }

    def _environment(self) -> Mapping[str, Any]:
        selected = {
            name: os.environ.get(name)
            for name in (
                "HELIOS_LLM_CONFIG",
                "HELIOS_LOG_FILE",
                "HELIOS_LOG_LEVEL",
                "HELIOS_AUDIO_INPUT_DEVICE",
                "HELIOS_AUDIO_OUTPUT_DEVICE",
                "HELIOS_AUDIO_OUTPUT_LATENCY",
                "HELIOS_OLLAMA_HOST",
            )
            if os.environ.get(name) is not None
        }
        return {
            "cwd": str(Path.cwd()),
            "root_exists": self.root.is_dir(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "selected_environment": selected,
            "codex_auth_file_present": _persistent_codex_auth_file().is_file(),
        }

    def _configuration(self) -> Mapping[str, Any]:
        import config

        self.settings = config.Settings.from_env(self.root)
        llm = self.settings.llm
        return {
            "language": self.settings.language,
            "listen_timeout_seconds": self.settings.listen_timeout,
            "audio_input_device": self.settings.audio_input_device,
            "audio_output_device": self.settings.audio_output_device,
            "audio_output_latency": self.settings.audio_output_latency,
            "routing_file": llm.routing_file,
            "routing_policy": llm.routing_policy,
            "remote_enabled": llm.remote_enabled,
            "emergency_local_only": llm.emergency_local_only,
            "privacy_default": llm.privacy.default,
            "allow_remote_transcripts": llm.privacy.allow_remote_transcripts,
            "allow_remote_context": llm.privacy.allow_remote_context,
            "providers": [
                {
                    "name": provider.name,
                    "adapter": provider.adapter,
                    "locality": provider.locality,
                    "enabled": provider.enabled,
                    "reuse_remote_thread": provider.reuse_remote_thread,
                }
                for provider in llm.providers
            ],
            "targets": [
                {
                    "name": target.name,
                    "provider": target.provider,
                    "model": target.model or target.model_for_language(self.settings.language),
                    "tier": target.tier,
                }
                for target in llm.targets
            ],
        }

    def _runtime_imports(self) -> Mapping[str, Any]:
        from scripts.doctor import (
            check_native_runtime,
            check_python,
            check_runtime,
        )

        checks = check_python() + check_runtime()
        if not any(check.level == "error" for check in checks):
            checks.extend(check_native_runtime(timeout=self.args.native_timeout))
        errors = [check.message for check in checks if check.level == "error"]
        return {
            "checks": [asdict(check) for check in checks],
            "errors": errors,
        }

    def _get_client(self) -> Any:
        if self.client is not None:
            return self.client
        if self.settings is None:
            self._configuration()
        from api.api_client import APIClient
        from api.routing import Connectivity

        self.client = APIClient(
            api_url=self.settings.ollama_host,
            model_talk=self.settings.profile.talk_model,
            model_think=self.settings.think_model,
            language=self.settings.language,
            llm_settings=self.settings.llm,
            kpi_settings=self.settings.kpi,
            connectivity=Connectivity.UNKNOWN,
            retry_wait=0,
        )
        return self.client

    def _route_matrix(self) -> Mapping[str, Any]:
        from api.providers.contracts import PrivacyLevel
        from api.routing import Connectivity

        client = self._get_client()
        request = client._request(
            mode="talk",
            message="diagnostic route probe",
            history=(),
            conversation_id="diagnostic-route",
            conversation_turn=1,
            context=None,
            context_origin="unknown",
            message_redacted=False,
            context_redacted=False,
            privacy=PrivacyLevel(self.settings.llm.privacy.default),
            request_options=None,
        )
        matrix: dict[str, Any] = {}
        for state in (Connectivity.ONLINE, Connectivity.OFFLINE, Connectivity.UNKNOWN):
            _planned, decision, _snapshot, selected = client._plan_detailed(
                request,
                connectivity=state,
            )
            matrix[state.value] = {
                "selected_connectivity": selected.value,
                "decision_reason": decision.reason,
                "network_forced_local": decision.network_forced_local,
                "routes": [target.name for target in decision.targets],
                "providers": [target.provider for target in decision.targets],
            }
        return {"matrix": matrix}

    def _network_quality(self) -> Mapping[str, Any]:
        from api.connectivity import ConnectivityMonitor, LinuxNetworkInspector

        settings = self.settings.llm.network
        inspector = LinuxNetworkInspector()
        passive = inspector.inspect(
            require_wifi=settings.require_wifi,
            interface_allowlist=settings.interface_allowlist,
        )
        samples: list[dict[str, Any]] = []
        monitor = ConnectivityMonitor(settings, inspector=inspector, route_watcher_factory=None)
        try:
            for _ in range(max(1, self.args.network_samples)):
                samples.append(asdict(monitor.refresh_once()))
        finally:
            monitor.close()
        return {
            "passive": asdict(passive),
            "samples": samples,
            "online_samples": sum(sample.get("connectivity") == "online" for sample in samples),
        }

    def _audio_devices(self) -> Mapping[str, Any]:
        import sounddevice

        devices = sounddevice.query_devices()
        summaries = [
            {
                "index": index,
                "name": str(device.get("name", "")),
                "max_input_channels": int(device.get("max_input_channels", 0)),
                "max_output_channels": int(device.get("max_output_channels", 0)),
                "default_samplerate": device.get("default_samplerate"),
            }
            for index, device in enumerate(devices)
        ]
        output_check: dict[str, Any]
        try:
            sounddevice.check_output_settings(
                device=self.settings.audio_output_device,
                samplerate=22_050,
                channels=1,
                dtype="int16",
            )
            output_check = {"status": "pass"}
        except Exception as error:  # noqa: BLE001 - report backend detail
            output_check = {"status": "fail", "error": _exception_text(error)}
        return {
            "configured_input": self.settings.audio_input_device,
            "configured_output": self.settings.audio_output_device,
            "devices": summaries,
            "output_settings_22050_mono_int16": output_check,
        }

    def _microphone_capture(self) -> Mapping[str, Any]:
        from recognizer.speech_recognizer import SpeechRecognizer

        recognizer = SpeechRecognizer(
            self.settings.profile.vosk_model,
            input_device=self.settings.audio_input_device,
        )
        try:
            recognizer._ensure_runtime()
            resolved = recognizer._resolve_input_device_index()
            events = list(recognizer.listen_events(timeout=max(0.2, self.args.capture_seconds)))
            final_count = sum(event.is_final for event in events)
            partial_count = sum(not event.is_final for event in events)
            energies = [
                float(event.frame_energy)
                for event in events
                if isinstance(event.frame_energy, (int, float))
            ]
            confidences = [
                float(event.confidence)
                for event in events
                if isinstance(event.confidence, (int, float))
            ]
            return {
                "resolved_input_device_index": resolved,
                "capture_seconds_requested": self.args.capture_seconds,
                "events": len(events),
                "final_events": final_count,
                "partial_events": partial_count,
                "max_frame_energy": max(energies) if energies else None,
                "max_confidence": max(confidences) if confidences else None,
                "recognized_text_recorded": False,
            }
        finally:
            recognizer.close()

    def _tts_synthesis(self) -> Mapping[str, Any]:
        from audio.tts import PiperTTS, SoundDeviceBackend

        tts = PiperTTS(
            self.settings.profile.tts_model,
            audio_backend=SoundDeviceBackend(
                device=self.settings.audio_output_device,
                latency=self.settings.audio_output_latency,
            ),
        )
        try:
            fragment = tts.synthesize_fragment("Diagnostica audio completata.")
            if fragment is None:
                raise RuntimeError("Piper returned no synthesized fragment")
            with wave.open(io.BytesIO(fragment.wave_bytes), "rb") as wave_file:
                audio = {
                    "channels": wave_file.getnchannels(),
                    "sample_width": wave_file.getsampwidth(),
                    "sample_rate": wave_file.getframerate(),
                    "frames": wave_file.getnframes(),
                }
            details: dict[str, Any] = {
                "wave_bytes": len(fragment.wave_bytes),
                "synthesis_ms": round(fragment.synthesis_ms, 1),
                "wave": audio,
                "playback_requested": self.args.play_tts,
            }
            if self.args.play_tts:
                timing = tts.play_fragment(fragment)
                details.update(
                    {
                        "playback_ms": round(timing.playback_ms, 1),
                        "audio_duration_ms": round(timing.audio_duration_ms, 1),
                        "playback_interrupted": tts.last_playback_was_interrupted,
                    }
                )
            return details
        finally:
            tts.close()

    def _command_matrix(self) -> Mapping[str, Any]:
        from assistant import VoiceAssistant

        class DiagnosticTTS:
            def __init__(self) -> None:
                self.spoken = 0

            def speak(self, _text: str) -> None:
                self.spoken += 1

            def close(self) -> None:
                return None

        class DiagnosticAPI:
            def __init__(self) -> None:
                self.talk_prompts: list[str] = []
                self.think_prompts: list[str] = []

            def talk(self, prompt: str, **_kwargs: Any) -> str:
                self.talk_prompts.append(prompt)
                return "Risposta diagnostica"

            def think(self, prompt: str, **_kwargs: Any) -> str:
                self.think_prompts.append(prompt)
                return "Ragionamento diagnostico"

            def close(self) -> None:
                return None

        class DiagnosticRecognizer:
            def close(self) -> None:
                return None

        class DiagnosticSound:
            def play_sound(self, _path: str) -> None:
                return None

            def close(self) -> None:
                return None

        tts = DiagnosticTTS()
        api = DiagnosticAPI()
        settings = replace(self.settings, barge_in_enabled=False)
        assistant = VoiceAssistant(
            settings=settings,
            tts=tts,
            sound_player=DiagnosticSound(),
            api_client=api,
            speech_recognizer=DiagnosticRecognizer(),
        )
        try:
            wake = settings.profile.wake_word
            think_word = settings.profile.think_words[0]
            cases = {
                "empty": assistant.process_command(""),
                "without_wake_word": assistant.process_command("accendi la luce"),
                "wake_only": assistant.process_command(wake),
                "valid_talk": assistant.process_command(f"{wake}, accendi la luce del soggiorno"),
                "valid_think": assistant.process_command(f"{wake}, {think_word} che ore sono"),
            }
            return {
                "returns": {name: value is not None for name, value in cases.items()},
                "talk_prompts": list(api.talk_prompts),
                "think_prompts": list(api.think_prompts),
                "spoken_responses": tts.spoken,
                "wake_word": wake,
                "automation_invocation_observed": False,
                "note": "The repository routes this command to the model; no home-automation adapter was invoked.",
            }
        finally:
            assistant.close()

    def _remote_preparation(self) -> Mapping[str, Any]:
        if not self.settings.llm.remote_enabled:
            return {"_status": "skip", "reason": "remote routing disabled"}
        thread = self._get_client().prepare_remote_async()
        if thread is None:
            return {"_status": "skip", "reason": "no configured Codex app-server target"}
        thread.join(timeout=self.args.remote_prepare_timeout)
        if thread.is_alive():
            return {
                "_status": "fail",
                "thread_alive_after_timeout": True,
                "timeout_seconds": self.args.remote_prepare_timeout,
            }
        return {
            "thread_alive_after_timeout": False,
            "runtime_health": self._get_client()._runtime_health_snapshot(),
        }

    def _codex_resume_probe(self) -> Mapping[str, Any]:
        """Probe the SDK resume RPC directly so its exception is not hidden.

        The production adapter deliberately converts SDK exceptions to safe
        provider errors. That is correct for user-facing behavior, but it also
        means an application log can say only ``worker_error``. This probe is
        bounded and records the exception type/message needed to distinguish a
        protocol, authentication, transport, or server-side failure.
        """

        if not self.settings.llm.remote_enabled:
            return {"_status": "skip", "reason": "remote routing disabled"}
        codex_provider = next(
            (
                provider
                for provider in self.settings.llm.providers
                if provider.adapter == "codex_app_server" and provider.enabled
            ),
            None,
        )
        from api.api_client import _HYBRID_SYSTEM_INSTRUCTIONS
        from api.providers.codex_app_server import (
            _OfficialCodexRuntime,
            _notification_parts,
            _prompt,
        )
        from api.providers.codex_session import field_value
        from api.providers.contracts import (
            ChatMessage,
            ChatRequest,
            ContentOrigin,
            PrivacyLevel,
            Role,
        )

        model = next(
            (
                target.model_for_language(self.settings.language)
                for target in self.settings.llm.targets
                if target.provider == "openai-codex" and target.name.startswith("codex-talk-")
            ),
            None,
        )
        if not model:
            return {"_status": "skip", "reason": "no Codex talk target configured"}

        runtime = _OfficialCodexRuntime()
        try:
            account = runtime.account_kind()
            if account != "chatgpt":
                return {"_status": "fail", "account_kind": account}
            request = ChatRequest(
                model=model,
                messages=(
                    ChatMessage(
                        Role.SYSTEM,
                        _HYBRID_SYSTEM_INSTRUCTIONS.get(
                            self.settings.language,
                            _HYBRID_SYSTEM_INSTRUCTIONS["en"],
                        ),
                        origin=ContentOrigin.STATIC_INSTRUCTION,
                    ),
                    ChatMessage(
                        Role.USER,
                        "Rispondi esclusivamente con OK.",
                        origin=ContentOrigin.RAW_TRANSCRIPT,
                    ),
                ),
                mode="talk",
                language=self.settings.language,
                privacy=PrivacyLevel.REMOTE_ALLOWED,
                max_output_tokens=self.settings.llm.talk.max_output_tokens,
            )
            developer, prompt = _prompt(request)
            try:
                first_turn = runtime.start_turn(
                    model=model,
                    prompt=prompt,
                    developer_instructions=developer,
                    effort="none",
                    service_tier=None,
                )
                event_count = 0
                completion_status: str | None = None
                completion_error_code: str | None = None
                completion_error_message: str | None = None
                for notification in first_turn.stream():
                    event_count += 1
                    method, payload = _notification_parts(notification)
                    if method != "turn/completed":
                        continue
                    completed_turn = field_value(payload, "turn", payload)
                    status = field_value(completed_turn, "status")
                    if hasattr(status, "value"):
                        status = status.value
                    completion_status = status if isinstance(status, str) else None
                    error = field_value(completed_turn, "error")
                    code = field_value(error, "code")
                    completion_error_code = code if isinstance(code, str) else None
                    # This probe always sends a fixed, content-free diagnostic
                    # prompt. Its provider message is therefore safe to retain
                    # after secret redaction and helps distinguish model access
                    # from account-token failures.
                    message = field_value(error, "message")
                    if isinstance(message, str) and message.strip():
                        completion_error_message = _redact(message.strip())[:400]
            except Exception as error:  # noqa: BLE001 - diagnostic evidence
                return {
                    "_status": "fail",
                    "account_kind": account,
                    "fresh_turn_success": False,
                    "fresh_exception_type": f"{type(error).__module__}.{type(error).__name__}",
                    "fresh_exception": _exception_text(error),
                }
            if completion_status != "completed":
                return {
                    "_status": "fail",
                    "account_kind": account,
                    "fresh_turn_success": False,
                    "first_turn_events": event_count,
                    "completion_status": completion_status,
                    "completion_error_code": completion_error_code,
                    "completion_error_message": completion_error_message,
                }
            thread_id = field_value(first_turn, "thread_id")
            if thread_id is None:
                thread_id = field_value(first_turn, "threadId")
            if not isinstance(thread_id, str) or not thread_id:
                return {
                    "_status": "fail",
                    "account_kind": account,
                    "first_turn_events": event_count,
                    "thread_id_observed": False,
                }
            if codex_provider is not None and not codex_provider.reuse_remote_thread:
                return {
                    "_status": "pass",
                    "account_kind": account,
                    "fresh_turn_success": True,
                    "first_turn_events": event_count,
                    "thread_id_observed": True,
                    "resume_probe_skipped": True,
                    "reuse_remote_thread": False,
                    "reason": (
                        "profile deliberately uses fresh Codex threads and sends canonical "
                        "Helios history with each turn"
                    ),
                }
            try:
                runtime.start_turn(
                    model=model,
                    prompt="Rispondi esclusivamente con OK.",
                    developer_instructions="You are a diagnostic model. Answer with OK.",
                    effort="none",
                    service_tier=None,
                    thread_id=thread_id,
                )
            except Exception as error:  # noqa: BLE001 - diagnostic evidence
                return {
                    "_status": "fail",
                    "account_kind": account,
                    "first_turn_events": event_count,
                    "thread_id_observed": True,
                    "resume_failure_observed": True,
                    "resume_exception_type": (f"{type(error).__module__}.{type(error).__name__}"),
                    "resume_exception": _exception_text(error),
                }
            return {
                "_status": "warn",
                "account_kind": account,
                "first_turn_events": event_count,
                "thread_id_observed": True,
                "resume_failure_observed": False,
                "note": "The SDK resume RPC returned successfully in this sample.",
            }
        finally:
            runtime.close()

    def _capture_client_logs(self, operation: Callable[[], Any]) -> tuple[Any, list[str]]:
        root_logger = logging.getLogger()
        root_logger.addHandler(self._log_capture)
        self._log_capture.lines.clear()
        try:
            value = operation()
            return value, list(self._log_capture.lines[-30:])
        finally:
            root_logger.removeHandler(self._log_capture)

    def _live_call(self, message: str, *, connectivity: Any) -> Mapping[str, Any]:
        from api.providers.contracts import PrivacyLevel

        started = time.monotonic()
        try:
            response, lines = self._capture_client_logs(
                lambda: self._get_client()._stream(
                    mode="talk",
                    message=message,
                    context=None,
                    speak=False,
                    privacy=PrivacyLevel.REMOTE_ALLOWED,
                    connectivity=connectivity,
                )
            )
            return {
                "response_nonempty": bool(response and response.strip()),
                "response_characters": len(response or ""),
                "elapsed_ms": round((time.monotonic() - started) * 1_000, 1),
                "completed_route": _completed_route(lines),
                "log_events": lines,
            }
        except Exception as error:  # noqa: BLE001 - include provider classification
            lines = list(self._log_capture.lines[-30:])
            return {
                "_status": "fail",
                "response_nonempty": False,
                "elapsed_ms": round((time.monotonic() - started) * 1_000, 1),
                "error": _exception_text(error),
                "log_events": lines,
            }

    def _live_provider_matrix(self) -> Mapping[str, Any]:
        from api.routing import Connectivity

        if not self.settings.llm.remote_enabled:
            return {"_status": "skip", "reason": "remote routing disabled"}
        attempts: list[dict[str, Any]] = []
        for index in range(self.args.repetitions):
            attempt = dict(
                self._live_call(
                    "Rispondi esclusivamente con OK.",
                    connectivity=Connectivity.ONLINE,
                )
            )
            attempt["attempt"] = index + 1
            attempts.append(attempt)
        successes = sum(attempt.get("response_nonempty") is True for attempt in attempts)
        remote_successes = sum(
            attempt.get("response_nonempty") is True
            and str(attempt.get("completed_route") or "").startswith("codex-")
            for attempt in attempts
        )
        return {
            "_status": "fail" if remote_successes < len(attempts) else "pass",
            "attempts": attempts,
            "response_successes": successes,
            "remote_successes": remote_successes,
            "remote_failures": len(attempts) - remote_successes,
            "remote_success_rate": remote_successes / len(attempts) if attempts else 0,
        }

    def _live_cancellation(self) -> Mapping[str, Any]:
        if not self.settings.llm.remote_enabled:
            return {"_status": "skip", "reason": "remote routing disabled"}
        from api.providers.contracts import PrivacyLevel
        from api.routing import Connectivity
        from api.streaming import CancellationController

        cancellation = CancellationController()
        outcome: dict[str, Any] = {}

        def worker() -> None:
            try:
                self._get_client()._stream(
                    mode="talk",
                    message="Scrivi una risposta lunga con molte frasi per consentire la cancellazione.",
                    context=None,
                    speak=False,
                    privacy=PrivacyLevel.REMOTE_ALLOWED,
                    connectivity=Connectivity.ONLINE,
                    cancellation=cancellation,
                )
                outcome["completed_before_cancel"] = True
            except Exception as error:  # noqa: BLE001 - cancellation is expected
                outcome["error"] = _exception_text(error)

        thread = threading.Thread(target=worker, name="diagnostic-cancellation", daemon=True)
        thread.start()
        time.sleep(max(0.05, self.args.cancel_after_seconds))
        cancellation.cancel()
        thread.join(timeout=self.args.cancel_timeout)
        if thread.is_alive():
            return {
                "_status": "fail",
                "thread_alive_after_timeout": True,
                "timeout_seconds": self.args.cancel_timeout,
            }
        if outcome.get("completed_before_cancel"):
            return {
                "_status": "warn",
                "cancel_observed": False,
                "note": "The provider completed before cancellation could be observed.",
                **outcome,
            }
        return {
            "cancel_observed": True,
            "thread_alive_after_timeout": False,
            **outcome,
        }

    def _bounded_local_fallback(self) -> Mapping[str, Any]:
        from api.api_client import APIClient
        from api.routing import Connectivity

        if self.settings is None:
            return {"_status": "skip", "reason": "configuration unavailable"}
        # A diagnostic must not inherit the remote profile's long total
        # timeout.  Its first-token budget deliberately matches the talk-mode
        # interactive budget: a cold, on-device fallback can legitimately
        # need longer than fifteen seconds to load a model, but it must still
        # complete inside the production-visible thirty-second budget.
        production_first_visible = (
            self.settings.llm.talk.first_visible_token_seconds
            or self.settings.llm.timeouts.first_token_seconds
        )
        total_seconds = min(
            _LOCAL_FALLBACK_MAX_TOTAL_SECONDS,
            self.settings.llm.timeouts.total_seconds,
        )
        first_token_seconds = min(
            _LOCAL_FALLBACK_MAX_FIRST_TOKEN_SECONDS,
            float(production_first_visible),
            total_seconds,
        )
        short_timeouts = replace(
            self.settings.llm.timeouts,
            connect_seconds=min(
                2.0,
                self.settings.llm.timeouts.connect_seconds,
                total_seconds,
            ),
            first_token_seconds=first_token_seconds,
            read_seconds=min(10.0, self.settings.llm.timeouts.read_seconds, total_seconds),
            total_seconds=total_seconds,
        )
        short_targets = tuple(
            replace(target, retry_attempts=1) for target in self.settings.llm.targets
        )
        short_llm = replace(
            self.settings.llm,
            remote_enabled=False,
            routing_policy="local_only",
            timeouts=short_timeouts,
            targets=short_targets,
        )
        client = APIClient(
            api_url=self.settings.ollama_host,
            model_talk=self.settings.profile.talk_model,
            model_think=self.settings.think_model,
            language=self.settings.language,
            llm_settings=short_llm,
            kpi_settings=self.settings.kpi,
            connectivity=Connectivity.OFFLINE,
            retry_wait=0,
        )
        try:
            # Reproduce the production failure that appeared only after a long
            # online conversation. The local target must receive its configured
            # recent-turn window while canonical provider-neutral history stays
            # complete inside ConversationSession.
            for index in range(_LOCAL_FALLBACK_HISTORY_TURNS):
                turn = client.conversation.begin_turn(
                    f"Domanda diagnostica precedente numero {index + 1}."
                )
                client.conversation.complete_turn(
                    turn,
                    "Risposta diagnostica precedente mantenuta nella cronologia canonica.",
                )
            local_target = next(
                (
                    target
                    for target in short_llm.targets
                    if target.name == "local-talk" and target.provider == "ollama"
                ),
                None,
            )
            if local_target is None or local_target.max_history_turns is None:
                return {
                    "_status": "fail",
                    "route_expected": "local",
                    "reason": "local talk target has no bounded history window",
                }
            started = time.monotonic()
            try:
                response = client._stream(
                    mode="talk",
                    message="Rispondi esclusivamente con OK.",
                    context=None,
                    speak=False,
                    connectivity=Connectivity.OFFLINE,
                )
                elapsed_seconds = time.monotonic() - started
                return {
                    "route_expected": "local",
                    "response_nonempty": bool(response and response.strip()),
                    "elapsed_ms": round(elapsed_seconds * 1_000, 1),
                    "canonical_history_turns_seeded": _LOCAL_FALLBACK_HISTORY_TURNS,
                    "local_history_turn_limit": local_target.max_history_turns,
                    "first_token_budget_seconds": first_token_seconds,
                    "interactive_target_exceeded": (
                        elapsed_seconds > _LOCAL_FALLBACK_INTERACTIVE_TARGET_SECONDS
                    ),
                }
            except Exception as error:  # noqa: BLE001 - local failure is evidence
                return {
                    "_status": "fail",
                    "route_expected": "local",
                    "elapsed_ms": round((time.monotonic() - started) * 1_000, 1),
                    "error": _exception_text(error),
                }
        finally:
            client.close()

    def _close_client(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                LOGGER.warning("Unable to close diagnostic API client", exc_info=self.args.verbose)


def analyze_log(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    patterns = {
        "codex_worker_error": re.compile(r"worker_error", re.IGNORECASE),
        "codex_thread_resume": re.compile(r"thread_resume", re.IGNORECASE),
        "local_stream_stop_unacknowledged": re.compile(
            r"stream_worker_stop_unacknowledged", re.IGNORECASE
        ),
        "model_stream_failure": re.compile(r"Unable to stream a model response", re.IGNORECASE),
        "network_offline": re.compile(r"Network gate state=offline", re.IGNORECASE),
        "network_online": re.compile(r"Network gate state=online", re.IGNORECASE),
        "audio_underflow": re.compile(r"Audio output underflow", re.IGNORECASE),
        "barge_in": re.compile(r"barge_in|assistant_turn_interrupted", re.IGNORECASE),
        "listen_timeout": re.compile(r"voice_listen_completed.*outcome=timeout", re.IGNORECASE),
        "listen_failure": re.compile(r"voice_listen_completed.*outcome=failed", re.IGNORECASE),
        "llm_success": re.compile(r"Completed (talk|think) request using route", re.IGNORECASE),
        "llm_route_selected": re.compile(r"event=llm_route_selected", re.IGNORECASE),
    }
    with path.open(encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            line = _redact(raw_line.rstrip())
            for name, pattern in patterns.items():
                if pattern.search(line):
                    counts[name] += 1
                    bucket = samples.setdefault(name, [])
                    if len(bucket) < 5:
                        bucket.append(line)
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "counts": dict(counts),
        "samples": samples,
        "interpretation": {
            "codex_resume_instability": counts["codex_worker_error"] > 0
            and counts["codex_thread_resume"] > 0,
            "local_fallback_timeout_signal": counts["local_stream_stop_unacknowledged"] > 0,
            "network_flapping_signal": counts["network_offline"] > 0
            and counts["network_online"] > 0,
            "audio_underflow_signal": counts["audio_underflow"] > 0,
        },
    }


def analyze_metrics(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    events: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    with path.open(encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            event = payload.get("event") or payload.get("name")
            if isinstance(event, str):
                events[event] += 1
            category = payload.get("error_category") or payload.get("timeout_category")
            if isinstance(category, str):
                failures[category] += 1
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "events": dict(events),
        "error_categories": dict(failures),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, default=None, help="JSON report path")
    parser.add_argument("--app-log", type=Path, default=None, help="existing Helios log to analyze")
    parser.add_argument("--metrics", type=Path, default=None, help="existing KPI JSONL to analyze")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--network-samples", type=int, default=3)
    parser.add_argument("--capture-seconds", type=float, default=1.5)
    parser.add_argument("--native-timeout", type=float, default=90.0)
    parser.add_argument("--remote-prepare-timeout", type=float, default=90.0)
    parser.add_argument("--cancel-after-seconds", type=float, default=0.25)
    parser.add_argument("--cancel-timeout", type=float, default=15.0)
    parser.add_argument("--play-tts", action="store_true", help="also play the diagnostic phrase")
    parser.add_argument("--skip-audio", action="store_true")
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument(
        "--skip-remote-live",
        action="store_true",
        help="skip external Codex calls but still exercise local Ollama",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("--repetitions must be at least one")
    if args.network_samples < 1:
        parser.error("--network-samples must be at least one")
    if args.capture_seconds <= 0:
        parser.error("--capture-seconds must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output
    if output is None:
        output = Path("logs") / (
            "voice-diagnostics-" + datetime.now().strftime("%Y%m%dT%H%M%S") + ".json"
        )
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        force=True,
    )
    suite = DiagnosticSuite(args)
    report = suite.run()
    output.write_text(json.dumps(report, indent=2, default=_json_value), encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))
    print(f"report={output}")
    return 1 if report["summary"].get("fail", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
