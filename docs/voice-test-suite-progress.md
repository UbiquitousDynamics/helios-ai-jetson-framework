# Voice test suite progress

## Current execution

Current test task: **02 — Provisional versus authoritative transcripts**.

Status: **blocked on required acoustic HIL evidence; complete local gate passed**. Completed test tasks: **00**. Task 01 remains blocked on its separate acoustic playback/capture-to-floor gate; Task 02 local results do not resolve it.

- [Task 02 report, exact commands and device inventory](voice-test-suite/task02-report.md)
- [Task 02 sanitized JSON evidence](voice-test-suite/task02-results.json)
- [Task 02 typed event contract v1.0.0](../tests/voice_suite/task02-contract.json): 10 Italian/English scenarios, 36 events (10 provisional, 14 authoritative finals, 12 ignored).
- [Task 02 desktop/Jetson local invariant profiles v1.0.0](../tests/voice_suite/task02-profile.json): zero authority/final-count/provisional-boundary errors, each with at least 10 actual samples.

Final Task 02 local gate: **29 focused tests passed**. Full repository local gate: **2043 passed, 1 Windows symlink privilege skip, 1 `remote_live` test deselected**. Syntax, Ruff and `git diff --check` passed. The two CLI profile runs each passed all local invariants with 36 event samples, 10 case samples and 10 provisional-boundary samples; all errors were zero. Desktop run: `8250499e-9985-4401-ae73-1c80698a0afb`; Jetson-profile-on-PC run: `09d45154-39e7-444a-8271-13a4f8b60c59`. Both exited 2 with HIL blocked. Sanitized correlated records are in `.voice-test-artifacts/task02/<run_id>/`.

Real device enumeration was performed without opening streams. PC PortAudio exposed 25 endpoints; Emilia's two enumerations exposed 13 and 24 endpoints respectively, with shifted indices. Physical roles, capture isolation and a calibration artifact remain unverified. The dedicated PC test environment received only `sounddevice` 0.5.6 for inventory; Piper, Vosk, PyAudio and ONNX Runtime remain absent there. Emilia's existing virtual environment has those packages, but no acoustic exchange ran. HIL has **0 exchanges**, **0 opened streams**, and acoustic latency/WER/CER/state mapping **n=0, unverified**. No Task 02 HIL adapter or calibration is claimed as complete. Cleanup passed for executed local work.

Next sequential test task: **03 — Adaptive turn endpointing**, **not started**. Task 01 and Task 02 acoustic gates remain blocked. **STOP at Task 02.**

## Retained Task 01

Current test task: **01 — Conversation events and floor states**.

Status: **blocked on required acoustic evidence; local gate passed**. Completed test tasks: **00**. Task 00 remains passed with HIL not applicable. Task 01's explicitly required observed playback/capture-to-floor mapping has not been exercised, and no local signal is reported as acoustic evidence.

- [Task 01 report and exact commands](voice-test-suite/task01-report.md)
- [Task 01 sanitized JSON evidence](voice-test-suite/task01-results.json)
- [Independent transition matrix v1.0.0](../tests/voice_suite/task01-transitions.json): 228 state/event cases, 136 legal and 92 illegal, including all candidate return states.
- [Task 01 desktop/Jetson local invariant profiles v1.0.0](../tests/voice_suite/task01-profile.json): zero errors for all 228 cases; bounded trace and test worker waits.

Final Task 01 local gate: **603 focused tests passed**, **1177 relevant regression tests passed including those 603**, zero failures/skips. Syntax, Ruff and `git diff --check` passed. Real floor/controller logic is exercised with deterministic typed inputs; no audio callback is treated as acoustic proof.

Both final CLI commands produced **228 passed matrix cases** and **blocked HIL**, exit 2. Desktop run: `d2e2a978-fa4a-4425-bc22-0e2b2d8e178b`; Jetson-profile-on-PC run: `3ffe2ab1-45ec-4e81-9c60-b36b0b234887`. Raw sanitized local counts per run: transition verdict errors=0, floor/return-state errors=0, revision errors=0, rejected-event mutations=0, snapshot identity errors=0; each has n=228 and median/p95/max=0 cases against maximum=0. Correlated raw events and per-case verdicts are under `.voice-test-artifacts/task01/<run_id>/`. Acoustic/resource/task metrics remain n=0 with null summaries and `unverified` status.

HIL prerequisites remain unresolved: explicit PC/Helios device IDs, calibration and isolated generated-speech conditions were requested but not supplied; Piper/Vosk/sounddevice/PyAudio/ONNX Runtime are absent in the selected PC environment. The actual acoustic adapter/event mapping remains to be completed and validated after device/calibration selection. No device inventory, audio opening, synthesis, recording or remote target execution occurred. Cleanup passed for local work, with joined test workers and no audio resources. Production files, baseline harness/specifications and the original Task 00 51/227 evidence remain unchanged.

At the close of Task 01, Task 02 was next and had not started. Task 01's required HIL gate remains pending; the subsequent Task 02 local work above does not satisfy it.

## Retained Task 00 baseline

Test task: **00 — Baseline and requirement traceability**.

Status: **passed**. The Task 00 local baseline and exhaustive applicable gate passed on the documented local checks. HIL is **not applicable** because Task 00 is documentation/specification-only and executes no runtime audio. The catalog, definitions, traceability and deterministic validation are complete. No FR or live-conversation acoustic behavior is verified by this baseline. This retained evidence predates the Task 01 execution above.

This checkpoint follows the separate automated voice-suite request. It does not advance the production implementation checkpoint, which currently records implementation tasks 00–10 complete and 11 next. Preserve the user's existing worktree modifications/deletions. Work on one test-suite task per execution turn, complete its applicable gates, update this checkpoint and stop.

- [Task 00 report: exact commands, versions, limitations and artifacts](voice-test-suite/task00-report.md)
- [Task 00 sanitized JSON evidence](voice-test-suite/task00-results.json)
- [Fixture catalog v1.0.0](../tests/voice_suite/fixtures.json): 46 synthetic specifications, 23 Italian and 23 English; no WAV generated.
- [Metric definitions v1.0.0](../tests/voice_suite/metrics.json): 52 metrics and 38 monotonic timestamp events.
- [Desktop/Jetson threshold profiles v1.0.0](../tests/voice_suite/thresholds.json): proposed; first-audio p95 5000 ms is provisional with 20 required samples; other acoustic/Jetson limits require calibration.
- [Hardware manifest template v1.0.0](../tests/voice_suite/hardware.example.json): explicit roles unset, calibration unverified, synthetic-only isolation required.
- [FR-01–FR-45 planned traceability](../tests/voice_suite/traceability.json): all IDs/source criteria verified structurally. The catalog's pending local/HIL evidence describes future runtime verification, not the Task 00 acceptance gate.

Final local gate: **51 focused tests passed**; **227 relevant tests passed including those 51**, zero failures/skips. Syntax, Ruff and `git diff --check` passed. These documented checks satisfy the **Task 00 exhaustive gate: passed**. Two desktop baseline commands exited 0. The historical Jetson-profile `--require-hil` guard, run locally on Windows, exited 2 (`blocked`); that observed command result is preserved but is not an applicable Task 00 gate. Three runs had matching specification/worktree/model/environment fingerprints and distinct correlation IDs. This disposition correction does not rerun or replace those results.

Raw sanitized baseline validation durations: **359, 359, 438 ms**; n=3, median=359 ms, R-7 p95=430.1 ms, max=438 ms. These are validator durations, not audio-path measurements, and carry no acceptance threshold. All 52 conversation/resource metrics have **n=0**, null median/p95/max and `unverified` status. Their raw empty-sample records, units and thresholds are retained under `.voice-test-artifacts/task00/<correlation_id>/metrics.jsonl`; precise IDs and hashes are in the linked report/results.

HIL: **not applicable for Task 00**, with no failed or blocked Task 00 HIL gate. Defer the HIL requirement to the **first test-suite task that actually exercises audio**. That task must resolve explicit PC/Helios device identities, acoustic calibration/isolation and native dependency prerequisites before acoustic execution. The current baseline script explicitly never opens audio devices; no production code, harness, tests, thresholds or calibration were changed for this documentation correction. Existing target memory-playback checks remain insufficient acoustic evidence. No hardware, remote inference, generated speech or user recording was used. Cleanup passed with zero audio streams/workers/audio/transcript artifacts.

Task 00 handed off to **01 — Conversation events and floor states**. See the current execution above for its local results and outstanding acoustic gate.
