# Sequential implementation prompt: natural realtime conversation for Helios AI

You are **GPT Astra**, acting as a senior Python engineer on the **Helios AI** repository. Evolve its voice interaction toward the natural, low-latency behavior associated with products such as GPT Live or Meta AI on Ray-Ban, while preserving Helios's offline-first, privacy-first, NVIDIA Jetson architecture.

Do not claim product parity. Implement and verify concrete behaviors.

## Mandatory execution protocol

Work on **exactly one task at a time**, in the numbered order below.

For every task:

1. Read only the files listed for that task plus directly required definitions.
2. Restate the task's scope and invariants in no more than five bullets.
3. Inspect the current implementation before editing.
4. Make the smallest coherent change that completes only that task.
5. Add or update focused unit, integration, concurrency, failure-path, and regression tests appropriate to that task.
6. Run exhaustive validation for the completed task as defined by the mandatory test gate below.
7. Fix every implementation-caused failure and repeat the complete gate until it passes.
8. Report changed files, exact test commands, results, risks, and limitations.
9. Update `docs/live-conversation-progress.md` with the completed task ID, decisions, interfaces, test evidence, target-device evidence when required, and next task ID.
10. Stop. Do not begin the next task in the same execution turn.

On the next turn, first read the progress checkpoint, inspect the working tree, and resume only the recorded next task. Never redo a completed task unless its tests fail or the user requests it.

If a prerequisite belongs to a later task, record it instead of implementing it early. If blocked, report the precise blocker and stop. Keep context small: use symbol search and narrow file ranges; do not repeatedly load the whole repository, test suite, models, assets, corpora, or logs. Do not use sub-agents unless explicitly requested.

## Mandatory test gate after every task

No task is complete, and the next task must not begin, until all applicable checks below pass:

1. Static/syntax validation for every changed Python file.
2. Focused unit tests for each new or changed behavior, including boundary values and invalid inputs.
3. Integration tests across every component boundary affected by the task.
4. Cancellation, timeout, concurrency, race, cleanup, and failure-injection tests whenever the task touches asynchronous or I/O behavior.
5. Relevant pre-existing regression tests for conversation, recognition, barge-in, TTS, streaming, context, routing, privacy, and shutdown.
6. The complete automated test suite, unless it is demonstrably impossible in the current environment. A full-suite failure may be classified as unrelated only with reproducible evidence; record it, but never hide or silently ignore it.
7. Target-device validation on Emilia whenever the change touches audio hardware, Vosk/Piper inference, system resources, Linux networking, Ollama, device timing, or shutdown behavior.

Use deterministic fakes first, then the real target where applicable. Test success paths, boundary conditions, malformed inputs, dependency failures, cancellation at every relevant lifecycle phase, repeated use, and resource cleanup. Never proceed based only on a smoke test. Never weaken, skip, delete, or broadly mark tests as expected failures merely to make the gate pass.

If exhaustive validation cannot pass, mark the task `blocked`, preserve the evidence in the checkpoint, and stop. Do not begin the next task.

## Emilia target-device access and validation

The authorized target is the Emilia device at `192.168.1.100`, using SSH account `emilia`. The user has provided the login password separately in the conversation. Treat it as a secret:

- never write the password into this repository, the progress checkpoint, source code, tests, shell history, process arguments, logs, reports, or generated scripts;
- use an existing SSH key, an interactive password prompt, or an approved secret-injection mechanism;
- never use tools that expose the password through command-line arguments or captured output;
- verify the SSH host key/fingerprint before the first connection and stop on an unexpected change;
- do not change system packages, services, network configuration, credentials, or unrelated files without explicit approval;
- deploy only the files needed for the current task to a dedicated project/worktree path;
- inspect target state before overwriting anything, preserve device-specific configuration, and never copy secrets or runtime data back into Git;
- run bounded commands and retain only sanitized, content-free test evidence;
- do not record microphone audio or real user speech during automated validation;
- stop and report a blocker if the device is unreachable or authentication fails; do not brute-force or repeatedly retry credentials.

Before target execution, run local deterministic tests. On the target, record the commit/worktree identity, Python/platform versions, relevant dependency versions, test command, sanitized outcome, and resource/latency measurements. A target-only pass does not replace local tests, and a local pass does not replace target validation when hardware behavior is in scope.

## Global invariants

- Preserve existing public APIs unless a task explicitly adds a backward-compatible extension.
- Preserve provider-neutral canonical history in `ConversationSession`.
- Never send provisional STT text to an LLM, tool, memory store, RAG, or remote service.
- Continue processing microphone input during generation, synthesis, and playback.
- Maintain one clear microphone-capture owner; never create competing PyAudio streams.
- Preserve privacy, provenance, connectivity, health, budget, fallback, and no-replay gates.
- Keep stop, cancel, mute, privacy, and session controls local and deterministic.
- Never report an external action as completed before authoritative backend confirmation.
- Never log audio, transcripts, prompts, answers, credentials, or headers.
- Use bounded queues/timeouts, cooperative cancellation, dependency injection, and deterministic tests.
- Do not modify binary models, voice/image/sound assets, or the RAG corpus.
- Do not add heavy dependencies without approval and Jetson/JetPack resource analysis.
- Keep every existing test green.

## Baseline to preserve

- `VoiceAssistant` and `VoiceConversationState` coordinate wake word, follow-ups, generation, speech, barge-in, and follow-up capture.
- `SpeechRecognizer` emits Vosk provisional and final events.
- `BargeInDetector` and `ConservativeEchoSuppressionPolicy` filter likely TTS echo and confirm interruption.
- `PiperTTS` supports interrupt, duck, and resume; `SpeechPipeline` overlaps synthesis/playback and supports cancellation.
- `StreamingResponseCoordinator`, `CancellationController`, and `APIClient.cancel_current()` implement streaming and cancellation.
- `ConversationSession` stores bounded provider-neutral history and safely represents interrupted turns.
- Hybrid routing enforces privacy, network, provider health, budget, and fail-closed fallback.

Known limitations must remain explicit: the session is single-user and in-process; history is not durable across restart; no spoken shutdown, general task manager, camera/world state, real diarization, cross-device handoff, or live translation currently exists.

## Task 00 — Establish the baseline

**Goal:** Record current behavior without changing runtime code.

**Read:** relevant symbols/ranges in `README.md`, `assistant.py`, `config.py`, `api/conversation.py`, `recognizer/speech_recognizer.py`, `recognizer/barge_in_detector.py`, `audio/speech_pipeline.py`, `audio/tts.py`, and directly relevant tests.

**Deliverables:**

- Create `docs/live-conversation-progress.md`.
- Add a concise microphone-to-speaker flow map.
- Add an FR-01 through FR-45 matrix with `supported`, `partial`, or `unsupported`, evidence by file/symbol, and proposed task ID.
- Record thread ownership, cancellation ownership, queue bounds, and timeout invariants.
- Run focused existing conversation tests without changing production code.

**Stop when:** the baseline and gaps are evidence-based and reproducible.

## Task 01 — Define conversation events and floor states

**Goal:** Add a typed provider-neutral control model without changing behavior.

**Read:** `assistant.py`, `api/conversation.py`, `recognizer/speech_recognizer.py`, related state tests.

**Implement:** typed events for provisional/final speech, silence, playback and generation lifecycle, and interruption; validated states `IDLE`, `ARMED`, `USER_SPEAKING`, `USER_PAUSED`, `FINALIZING`, `THINKING`, `ASSISTANT_SPEAKING`, `BARGE_IN_CANDIDATE`, `INTERRUPTED`, `SUSPENDED`; content-free snapshots; a compatibility adapter if replacing `VoiceConversationState` is risky.

**Do not:** alter endpointing, barge-in thresholds, routing, or task execution.

**Tests:** legal/illegal transitions, idempotent terminal signals, thread-safe snapshots.

## Task 02 — Separate provisional and authoritative transcripts

**Goal:** Prevent provisional text from escaping the realtime layer.

**Read:** recognizer, assistant, conversation session, and their focused tests.

**Implement:** immutable provisional revisions and authoritative utterances; segment/revision identity where metadata permits; one promotion path; guards at API, RAG, tool, history, and metrics boundaries.

**Tests:** revised partials, duplicate finals, stale segments, partial-only timeout, exactly-once authoritative dispatch.

## Task 03 — Add adaptive turn endpointing

**Goal:** Distinguish thinking pauses from end-of-turn locally.

**Read:** Task 02 interfaces, recognizer, relevant configuration/tests.

**Implement:** a pure `TurnEndpointDetector`; configurable short-pause, finalization, inactivity, and maximum-utterance bounds; decisions using local VAD/activity, Vosk revision stability, final/confidence/timing metadata, and monotonic time; exactly one finalization; maximum-bound fail-safe.

**Do not:** add a neural endpointing dependency.

**Tests:** short/long pause, resumed speech, noisy silence, explicit final, maximum duration, fake-clock boundaries.

## Task 04 — Preserve intra-utterance self-corrections

**Goal:** Make “Tuesday... actually, Wednesday” one authoritative request.

**Read:** Tasks 02–03 code, recognizer parsing, dispatch boundary.

**Implement:** revision aggregation preserving final recognizer wording and exactly-once dispatch. Do not add semantic rewriting that guesses intent.

**Tests:** Italian/English corrections, repetitions, Vosk revisions, one model call with only final wording.

## Task 05 — Integrate the realtime floor controller

**Goal:** Route microphone, STT, generation, synthesis, and playback through one source of truth.

**Read:** `assistant.py`, Tasks 01–04, speech pipeline, TTS.

**Implement:** one controller; one capture owner across normal/barge-in listening; event-driven transitions; compatibility with `run_once()` and injected doubles.

**Tests:** complete normal lifecycle, timeout, failure recovery, clean stop, no duplicate capture stream.

## Task 06 — Harden full-duplex capture

**Goal:** Guarantee input processing during generation, synthesis, and playback.

**Read:** assistant barge-in paths, speech pipeline, TTS, Task 05.

**Implement:** continuous response-time processing with bounded queues; correct lifecycle across generation/playback timing gaps and synthesis failure; bounded cleanup without worker leaks.

**Tests:** speech during each pipeline phase, playback gaps, provider EOF, shutdown during every phase.

## Task 07 — Refine interruption candidate handling

**Goal:** Duck quickly for plausible speech but cancel only after confirmation.

**Read:** barge-in detector, echo policy, assistant, TTS controls.

**Implement:** candidate → bounded duck; confirmation → TTS interrupt plus model cancellation; rejection/expiry → resume; segment-scoped echo suppression; validated configurable thresholds.

**Tests:** real speech, exact/fuzzy echo, low-energy noise, short hallucination, timeout, repeated interruption.

## Task 08 — Define local control-intent semantics

**Goal:** Separate speech, session, and task control deterministically.

**Read:** assistant, active language profiles, Task 05.

**Implement:** local Italian/English parser with `STOP_SPEAKING`, `MUTE`, `UNMUTE`, `SUSPEND_SESSION`, `RESUME_SESSION`, `END_SESSION`, `CANCEL_TASK`. “Stop/basta/silenzio” stops current speech only. Ending a session requires new activation. Keep task cancellation explicit even before TaskManager exists.

**Do not:** send control commands to an LLM or canonical conversation history.

**Tests:** commands and false matches, offline behavior, speaking-time behavior, reactivation.

## Task 09 — Stabilize wake and follow-up sessions

**Goal:** Make the follow-up window independent of one STT call timeout.

**Read:** assistant, conversation session, configuration/tests.

**Implement:** configurable monotonic activation timeout; session touch only for meaningful activity; one wake then wake-free follow-ups; explicit local reset coordinated with canonical history.

**Tests:** wake-only, ten wake-free follow-ups, exact expiry, termination, fallback, safe reset.

## Task 10 — Improve pacing and backchannels

**Goal:** Keep speech concise and responsive without false acknowledgement.

**Read:** backchannel, speech chunker, assistant, language profiles.

**Implement:** delayed cancellable backchannels; supersession before real speech; suppression during short pauses, dictation, controls, and sensitive confirmation; concise spoken-response instruction without weakening correctness.

**Tests:** fast/slow response, start race, interruption, suppression cases, both languages.

## Task 11 — Add a provider-neutral task-state core

**Goal:** Introduce lifecycle management without real external services.

**Read:** assistant, conversation state, cancellation contracts, checkpoint.

**Implement:** bounded `TaskManager` with stable IDs, cancellation tokens, immutable snapshots, and states `PENDING`, `EXECUTING`, `WAITING_FOR_USER`, `COMPLETED`, `FAILED`, `CANCELLED`, `SUPERSEDED`; deterministic fake backend; strict separation from conversation floor.

**Do not:** integrate web, calendar, booking, messaging, vehicle, or robot services.

**Tests:** legal/illegal transitions, races, bounded retention, concurrency.

## Task 12 — Add task delegation and verified reporting

**Goal:** Remain conversational while fake delegated work executes.

**Read:** Task 11, assistant, streaming/cancellation contracts.

**Implement:** non-blocking delegation; sparse progress without chain-of-thought; success language only after authoritative confirmation; unambiguous failure/cancellation; sensitive confirmation immediately before fake commit.

**Tests:** conversation during execution, success/failure/cancel, waiting/denied confirmation, no premature completion claim.

## Task 13 — Add mid-task steering

**Goal:** Refine, replace, or cancel active work from later speech.

**Read:** Tasks 11–12 and canonical conversation interfaces.

**Implement:** explicit update-or-supersede policy; causal task links; late-result rejection; uninterrupted conversation.

**Tests:** update, supersede, cancel, late completion, repeated steering, intact history.

## Task 14 — Verify fallback, privacy, and offline controls

**Goal:** Prove new behavior has not weakened safety boundaries.

**Read:** privacy, connectivity, routing, streaming, and new integration seams.

**Implement only if a test reveals a defect.**

**Tests:** privacy/network denial, failure before/after speech, offline controls, interrupted history, provider-thread rotation, no replay after audio commit.

## Task 15 — Stress, resource, and shutdown validation

**Goal:** Demonstrate bounded behavior suitable for later Jetson certification.

**Read:** new runtime components, KPI interfaces, shutdown tests.

**Implement:** content-free metrics for final-STT-to-first-audio, interruption, rejected barge-in, endpointing, task latency, fallback, and errors; bounded-state assertions. Do not claim real hardware results.

**Tests:** at least 50 turns, repeated interruptions/steering, queue pressure, cancellation in every phase, no surviving owned worker after bounded shutdown.

## Task 16 — Documentation and final audit

**Goal:** Document only verified behavior.

**Read:** checkpoint, changed interfaces, README, relevant docs.

**Deliverables:** update architecture/configuration/migration docs; repeat FR-01–FR-45 audit with test evidence; clearly defer unsupported features; document Jetson acoustic, latency, resource, thermal, and power validation; run the complete suite.

**Stop after Task 16.**

## Optional future tracks — never start automatically

These require separate approval and design/privacy review: semantic camera/world state, consent-controlled persistent memory, speaker diarization, cross-device handoff, dynamic multilingual STT/TTS and live translation, real tools or vehicle actuation, and context-triggered proactive assistance.

## Required report after every task

```text
Task: TASK_ID — title
Status: completed | blocked
Changed: files and one-line purpose
Tests: commands and pass/fail counts
Exhaustive gate: passed | blocked, with evidence
Emilia validation: passed | not applicable | blocked, with sanitized evidence
Invariants checked: concise list
Known limitations: concise list
Checkpoint: docs/live-conversation-progress.md updated
Next task: TASK_ID — title
STOP: waiting for the next execution turn
```
