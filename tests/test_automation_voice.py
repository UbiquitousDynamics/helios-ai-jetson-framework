from dataclasses import replace

import anyio
import pytest

from automation.contracts import ActionOutcome, OutcomeStatus
from automation.planner import PlanKind, PlanResult
from automation.voice import VoiceActionController, VoiceActionState
from recognizer.speech_recognizer import RecognitionResult
from tests.test_automation_policy import action, catalog, policy


class Planner:
    policy = policy()

    async def plan(self, *args, **kwargs):
        return PlanResult(
            PlanKind.PROPOSE, (action(session_id=kwargs["session_id"], turn_id=kwargs["turn_id"]),)
        )


class Executor:
    def __init__(self, status=OutcomeStatus.SUCCESS):
        self.calls = []
        self.status = status

    async def execute(self, proposal, confirmation, **kwargs):
        self.calls.append((proposal, confirmation))
        return ActionOutcome(proposal.action_id, self.status, "fixture")


class Verifier:
    calibration_id = "synthetic-fixture-not-hardware-evidence"

    def accepts(self, recognition, *, after):
        return recognition.frame_energy == 1.0


def recognition(text="confermo", **changes):
    fields = dict(
        text=text,
        is_final=True,
        capture_id=2,
        segment_id=1,
        segment_started_at=21,
        frame_energy=1.0,
    )
    return RecognitionResult(**(fields | changes))


def controller(status=OutcomeStatus.SUCCESS, verifier=True):
    spoken = []
    executor = Executor(status)

    async def discover():
        return catalog()

    controller = VoiceActionController(
        Planner(),
        discover=discover,
        executor=executor,
        speak=spoken.append,
        describe=lambda proposal: "accendere la luce dell'ufficio (light.office)",
        verifier=Verifier() if verifier else None,
        clock=lambda: 80,
        capture_clock=lambda: 20,
    )
    return controller, spoken, executor


async def propose(voice):
    await voice.handle(
        "domotica accendi la luce",
        recognition(capture_id=1, segment_started_at=10),
        session_id="s",
        turn_id="t",
    )


def test_exact_pending_confirmation_dispatches_once():
    voice, spoken, executor = controller()

    async def scenario():
        await propose(voice)
        assert voice.state == VoiceActionState.AWAITING_CONFIRMATION
        assert "light.office" in spoken[0]
        assert executor.calls == []
        outcome = await voice.handle("confermo", recognition(), session_id="s", turn_id="next")
        assert outcome.status == OutcomeStatus.SUCCESS
        assert voice.state == VoiceActionState.OUTCOME
        await voice.handle("confermo", recognition(capture_id=3), session_id="s", turn_id="next")
        assert len(executor.calls) == 1

    anyio.run(scenario)


@pytest.mark.parametrize(
    "text,changes",
    [
        ("no", {}),
        ("sì", {}),
        ("non confermo", {}),
        ("confermo", {"is_final": False}),
        ("confermo", {"capture_id": 1}),
        ("confermo", {"segment_started_at": 19}),
        ("confermo", {"frame_energy": 0.0}),
    ],
)
def test_negative_ambiguous_partial_stale_and_echo_cannot_confirm(text, changes):
    voice, _, executor = controller()

    async def scenario():
        await propose(voice)
        await voice.handle(text, recognition(text, **changes), session_id="s", turn_id="next")
        assert executor.calls == []
        assert voice.pending is None

    anyio.run(scenario)


def test_uncalibrated_confirmation_blocks_voice_writes():
    voice, _, executor = controller(verifier=False)

    async def scenario():
        await propose(voice)
        assert voice.pending is None
        assert voice.state == VoiceActionState.CLARIFICATION
        assert executor.calls == []

    anyio.run(scenario)


def test_expiry_session_change_and_barge_cancel_block_dispatch():
    for mode in ("expired", "session", "barge"):
        voice, _, executor = controller()

        async def scenario():
            await propose(voice)
            if mode == "expired":
                voice.clock = lambda: 101
            if mode == "barge":
                voice.cancel()
            await voice.handle(
                "confermo",
                recognition(),
                session_id="other" if mode == "session" else "s",
                turn_id="next",
            )
            assert executor.calls == []

        anyio.run(scenario)


@pytest.mark.parametrize("status", list(OutcomeStatus))
def test_spoken_outcome_comes_from_executor(status):
    voice, spoken, executor = controller(status)

    async def scenario():
        await propose(voice)
        outcome = await voice.handle("confermo", recognition(), session_id="s", turn_id="next")
        assert outcome.status == status
        if status == OutcomeStatus.UNKNOWN:
            assert "Non posso confermare" in spoken[-1]
        assert len(executor.calls) == 1

    anyio.run(scenario)


def test_speech_failure_after_execution_does_not_leave_pending_replay():
    voice, _, executor = controller()

    async def scenario():
        await propose(voice)

        def fail(message):
            raise RuntimeError("speaker failed")

        voice.speak = fail
        with pytest.raises(RuntimeError):
            await voice.handle("confermo", recognition(), session_id="s", turn_id="next")
        assert voice.pending is None
        assert voice.state == VoiceActionState.OUTCOME
        assert len(executor.calls) == 1

    anyio.run(scenario)


def test_assistant_hook_requires_wake_and_preserves_normal_requests():
    from tests.test_assistant import make_assistant

    voice, _, executor = controller()
    assistant, _, api, _, _ = make_assistant(
        [
            recognition("domotica accendi", capture_id=1),
            recognition("Emilia domotica accendi", capture_id=2),
        ]
    )
    assistant.settings = replace(
        assistant.settings, automation=voice.planner.policy.settings, barge_in_enabled=False
    )
    assistant.automation_controller = voice
    try:
        assert not assistant.run_once()
        assert assistant.run_once()
        assert api.messages == []
        assert executor.calls == []
        assert voice.pending is not None
    finally:
        assistant.close()
