"""Explicit voice action states; uncalibrated recognition cannot authorize writes."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from automation.contracts import ActionOutcome, ActionProposal, Confirmation, OutcomeStatus
from automation.planner import PlanKind, ProposalPlanner
from recognizer.speech_recognizer import RecognitionResult


class VoiceActionState(str, Enum):
    IDLE = "idle"
    CLARIFICATION = "clarification"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    DISPATCH = "dispatch"
    OUTCOME = "outcome"


class CalibratedConfirmationVerifier(Protocol):
    calibration_id: str

    def accepts(self, recognition: RecognitionResult, *, after: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class PendingAction:
    proposal: ActionProposal
    initial_capture_id: int | None
    prompt_finished_at: float


class VoiceActionController:
    def __init__(
        self,
        planner: ProposalPlanner,
        *,
        discover,
        executor,
        speak: Callable[[str], None],
        describe: Callable[[ActionProposal], str],
        verifier: CalibratedConfirmationVerifier | None = None,
        language: str = "it",
        clock: Callable[[], float] = time.time,
        capture_clock: Callable[[], float] = time.monotonic,
    ):
        if language not in {"it", "en"}:
            raise ValueError("Unsupported voice action language")
        self.planner = planner
        self.discover = discover
        self.executor = executor
        self.speak = speak
        self.describe = describe
        self.verifier = verifier
        self.language = language
        self.clock = clock
        self.capture_clock = capture_clock
        self.state = VoiceActionState.IDLE
        self.pending: PendingAction | None = None
        self._cancelled = threading.Event()

    def accepts(self, text: str) -> bool:
        return self.pending is not None or text.casefold().startswith(
            "domotica " if self.language == "it" else "home control "
        )

    def cancel(self):
        self._cancelled.set()
        self.pending = None
        self.state = VoiceActionState.IDLE

    def _say(self, italian: str, english: str):
        self.speak(italian if self.language == "it" else english)

    async def handle(
        self, text: str, recognition: RecognitionResult, *, session_id: str, turn_id: str
    ):
        if self.pending is not None:
            pending, self.pending = self.pending, None
            proposal = pending.proposal
            valid = (
                proposal.session_id == session_id
                and not proposal.is_expired(self.clock())
                and recognition.is_final
                and recognition.capture_id is not None
                and (
                    pending.initial_capture_id is None
                    or recognition.capture_id > pending.initial_capture_id
                )
                and recognition.segment_started_at is not None
                and recognition.segment_started_at > pending.prompt_finished_at
                and self.verifier is not None
                and bool(self.verifier.calibration_id)
                and self.verifier.accepts(recognition, after=pending.prompt_finished_at)
                and text.strip().casefold()
                in ({"confermo"} if self.language == "it" else {"confirm"})
                and recognition.text.strip().casefold() == text.strip().casefold()
                and not self._cancelled.is_set()
            )
            if not valid:
                self.state = VoiceActionState.CLARIFICATION
                self._say("Azione annullata.", "Action cancelled.")
                return None
            confirmation = Confirmation(proposal.fingerprint, session_id, proposal.expires_at)
            return await self._dispatch(proposal, confirmation)

        self._cancelled.clear()
        prefix = "domotica " if self.language == "it" else "home control "
        if not text.casefold().startswith(prefix):
            return None
        if not recognition.is_final:
            return None
        try:
            catalog = await self.discover()
            plan = await self.planner.plan(
                text[len(prefix) :], catalog, session_id=session_id, turn_id=turn_id
            )
        except Exception:
            plan = None
        if plan is None or plan.kind != PlanKind.PROPOSE or len(plan.proposals) != 1:
            self.state = VoiceActionState.CLARIFICATION
            self._say("Specifica un dispositivo e un'azione.", "Specify one device and action.")
            return None
        proposal = plan.proposals[0]
        if not self.planner.policy.requires_confirmation(proposal):
            return await self._dispatch(proposal, None)
        if self.verifier is None or not self.verifier.calibration_id:
            self.state = VoiceActionState.CLARIFICATION
            self._say(
                "La conferma vocale non è ancora verificata: azione non eseguita.",
                "Voice confirmation is not verified; action was not executed.",
            )
            return None
        description = self.describe(proposal)
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Exact local action description is required")
        try:
            self._say(
                f"Vuoi {description}? Di' confermo per autorizzare.",
                f"Do you want to {description}? Say confirm to authorize.",
            )
        except Exception:
            self.cancel()
            raise
        if self._cancelled.is_set():
            self.cancel()
            return None
        self.pending = PendingAction(proposal, recognition.capture_id, self.capture_clock())
        self.state = VoiceActionState.AWAITING_CONFIRMATION
        return None

    async def _dispatch(self, proposal, confirmation) -> ActionOutcome:
        self.state = VoiceActionState.DISPATCH
        outcome = await self.executor.execute(
            proposal, confirmation, cancelled=self._cancelled.is_set
        )
        self.pending = None
        self.state = VoiceActionState.OUTCOME
        phrases = {
            OutcomeStatus.SUCCESS: (
                "Il servizio ha confermato il completamento.",
                "The service confirmed completion.",
            ),
            OutcomeStatus.DENIED: ("Azione non autorizzata.", "Action not authorized."),
            OutcomeStatus.FAILED: (
                "Il servizio ha segnalato un errore.",
                "The service reported an error.",
            ),
            OutcomeStatus.UNKNOWN: (
                "Non posso confermare l'esito. Non ripeto l'azione.",
                "I cannot confirm the outcome. I will not repeat the action.",
            ),
        }
        self._say(*phrases[outcome.status])
        return outcome
