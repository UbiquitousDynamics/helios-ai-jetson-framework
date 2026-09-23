# Test Task 01 — Conversation events and floor states

Status: **blocked** on required acoustic playback/capture-to-floor evidence. The complete applicable **local gate passed**. Task 00 remains passed with HIL not applicable. Task 02 has not started.

Implementation under test: `d0446f0bb04ac74aaebd59f858ee0ba5170a4fb2` plus the existing worktree, principally `api/conversation_control.py` and `api/realtime_conversation.py`. [Sanitized results](task01-results.json) record the production file identities, environment, command lines, thresholds and artifacts. No production file, existing threshold, model or Task 00 evidence file was changed.

## Implemented increment

- [Transition contract](../../tests/voice_suite/task01-transitions.json), version **1.0.0**: an independent explicit matrix of **228** cases, comprising **136 legal and 92 illegal** transitions. The 10 floor states produce **12** distinct contexts because a candidate may return to armed, thinking or assistant speaking. Each of 19 event kinds is specified for every context. Expectations are not imported or generated from the production private transition table.
- [Dedicated floor group](../../tests/test_voice_suite_floor_states.py): **563** tests cover exact reachable snapshots, every matrix pair, duplicate events, rejected-event recovery, session termination/suspension recovery, latest candidate return state, allocation failure, immutable/content-free types, deterministic concurrent publication and joined test workers.
- [Correlation and harness group](../../tests/test_voice_suite_events.py): **40** tests cover stale events after finished/interrupted responses, active-response isolation, synthesis/playback failure recovery, capture lease snapshots, complete trace metadata, artifact isolation, bounded concurrent collection, malformed contracts/profiles, insufficient samples, unverified devices/calibration and nonpassing HIL reporting. These inputs are typed deterministic signals. Capture leases and lifecycle calls are not physical microphone/playback observations.
- [Task 01 harness](../../scripts/voice_suite_task01.py): executes the real pure floor against the independent contract and writes sanitized per-case outcomes and snapshots. Each event has a run ID, fixture ID, opaque correlation/host/clock IDs, sequential event ID and a monotonic nanosecond timestamp. Response-event records retain their actual event kind and response ID. Caller-provided case labels are mapped to content-free fixture/opaque IDs in event records; only declared synthetic case names appear in `cases.jsonl`.
- [Task 01 profiles](../../tests/voice_suite/task01-profile.json), version **1.0.0**: desktop and Jetson profiles require all **228** cases and **zero** verdict, state, revision, rejected-event mutation or snapshot identity errors. Every metric declares units, source, sample count and threshold. Logical invariant limits cannot be relaxed to produce a pass. Trace storage is bounded to **4096** records; local thread joins use a configured **5 s** deadlock watchdog, not a calibrated performance budget.

Before changes, the current checkpoint, floor/controller and their tests, baseline manifests/validator, configured Piper synthesis method, Vosk input/device path, device diagnostics, KPI fields and pytest isolation were inspected. Reuse remains `audio.tts.PiperTTS.synthesize_wave` for future generated speech; no arbitrary WAV, fake recognizer audio or substitute TTS was created. The shared fixture catalog assigns four fixtures to Task 01: `it-greeting`, `it-wake-followups`, `en-greeting`, `en-wake-followups`. They remain **planned acoustic fixtures**, not executed recordings. Local state cases are language independent.

FR traceability is scoped: the floor model and response correlation support the local portions of FR-06 and the lifecycle portions referenced by FR-02. This does not certify other-speaker estimation, uninterrupted acoustic capture, or complete FR satisfaction. The original FR matrix and unverified acoustic evidence statuses remain unchanged.

## Exact final validation gate

Windows 11 AMD64; Python **3.12.10**, pytest **8.4.2**, numpy **2.5.3**, httpx **0.28.1**, Ruff **0.16.8**. The selected PC interpreter currently lacks **Piper, Vosk, sounddevice, PyAudio and ONNX Runtime**, confirmed by package inventory. No packages were installed or production environment settings changed. Actual target/device/native versions remain unverified.

```powershell
$taskPython = Join-Path $env:TEMP 'helios-live-conversation-task00-20260918/Scripts/python.exe'
& $taskPython -m py_compile scripts/voice_suite_task01.py tests/test_voice_suite_events.py tests/test_voice_suite_floor_states.py
& $taskPython -m ruff check scripts/voice_suite_task01.py tests/test_voice_suite_events.py tests/test_voice_suite_floor_states.py
& $taskPython -m pytest -q tests/test_voice_suite_floor_states.py tests/test_voice_suite_events.py --tb=short
& $taskPython -m pytest -q tests/test_voice_suite_floor_states.py tests/test_voice_suite_events.py tests/test_voice_suite_baseline.py tests/test_conversation_control.py tests/test_realtime_conversation.py tests/test_transcripts.py tests/test_full_duplex_capture.py tests/test_tts.py tests/test_recognizer.py tests/test_device_voice_diagnostics.py tests/test_kpi_metrics.py tests/test_assistant.py tests/test_barge_in_integration.py --tb=short
& $taskPython scripts/voice_suite_task01.py --artifact-dir .voice-test-artifacts/task01 --profile desktop --hardware-manifest tests/voice_suite/hardware.example.json
& $taskPython scripts/voice_suite_task01.py --artifact-dir .voice-test-artifacts/task01 --profile jetson --hardware-manifest tests/voice_suite/hardware.example.json
git diff --check
git check-ignore .voice-test-artifacts/task01/
```

Syntax/Ruff passed. Focused: **603 passed, 0 failed, 0 skipped in 1.25 s**. Relevant regressions including those 603: **1177 passed, 0 failed, 0 skipped in 7.59 s**. The baseline's original **51/227** results remain preserved in the Task 00 report. Full repository tests were not repeated for this isolated increment.

The initial local gate passed 594/1168 tests. Review then exposed malformed-contract/profile cases that could claim incomplete coverage, omit invariants or ignore declared revisions. These were fixed and covered before repeating the full applicable local gate. Trace host/clock/fixture fields were aligned with the shared metric contract, and the result manifest is written after its case/event artifacts. No production defect was found and no production threshold was changed to pass.

Both final CLI runs exited **2**, reporting `local_matrix=passed`, `hil=blocked`, overall `blocked`. The Jetson-profile command ran **on Windows**, not on Emilia. Neither command opens devices or synthesizes speech. This is a prerequisite report and local state runner, not a completed acoustic runner. The typed local signal results cannot satisfy the original Task 01 requirement that observed acoustic playback/capture events map to the correct state.

## Measurements and artifacts

The local trace clock is `time.perf_counter_ns`, monotonic, with reported resolution **0.0000001 s**. Opaque host/clock identities are scoped to each run. No raw clocks from different hosts are subtracted. No event timestamp is presented as measured STT/TTS/end-to-end latency.

| Local invariant, per matrix case | n per profile | Median | p95 | Maximum | Failure count | Required maximum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Transition verdict errors | 228 | 0 | 0 | 0 | 0 | 0 |
| Resulting floor/return-state errors | 228 | 0 | 0 | 0 | 0 | 0 |
| Revision errors | 228 | 0 | 0 | 0 | 0 | 0 |
| Mutation after rejected input | 228 | 0 | 0 | 0 | 0 | 0 |
| Snapshot identity errors | 228 | 0 | 0 | 0 | 0 | 0 |

Units are erroneous cases, source `cases.jsonl`, with correlated state observations in `events.jsonl`. Each complete matrix is a separate profile run; the two identical case sets are not counted as extra coverage. End-to-end, STT/TTS, WER/CER, interruption, CPU/RAM/thermal/power, queues and backend-task metrics have **n=0**, null median/p95/max, and **unverified** status. There is no acoustic pass or measured zero latency.

Final ignored local artifact directories, each containing `results.json`, `cases.jsonl` and `events.jsonl`:

- Desktop: `.voice-test-artifacts/task01/d2e2a978-fa4a-4425-bc22-0e2b2d8e178b/`
- Jetson profile on PC: `.voice-test-artifacts/task01/3ffe2ab1-45ec-4e81-9c60-b36b0b234887/`

Earlier rehearsal output under the same ignored Task 01 root is superseded by these final IDs. [Published JSON evidence](task01-results.json) records the final commands, profiles, identities and aggregates. Each final event file has **228** local records; raw event times remain in the ignored artifact directory. Audio retention remains configured as `delete` in the unmodified hardware template; no audio or transcript artifact exists to retain/delete in this increment.

## Required acoustic gate and stop

Task 01 HIL: **blocked**, with **0 exchanges**, **0 devices opened**, and no acoustic assertion executed. Explicit PC speaker/microphone and Helios input/output identities plus a calibration manifest and synthetic-only isolation confirmation were requested while local work continued; no values were supplied during this execution. The template still has null device selections and unverified calibration, and the PC native packages are absent. Target availability was not probed or inferred from historical memory-playback reports.

To finish Task 01 HIL, supply verified device/calibration/isolation prerequisites, complete and validate the actual PC-to-Helios-to-PC acoustic adapter and map its observed events to the controller using the shared correlation ID. That remaining work must use real configured Piper speech, explicit temporary audio storage, a unique marker, calibrated level/self-trigger abort, bounded processes/capture and confirmed cleanup. A filled manifest alone cannot claim calibration or acoustic success. No fabricated calibration or default device selection is used.

Cleanup: **passed for executed local work**. Local concurrent tests assert worker termination; all passed. The CLI creates no workers or streams; its bounded Git identity subprocess returned normally. No hardware stream, audio recording, user speech, live provider call, credentials or raw sensitive transcript was used. Production/worktree and shared baseline specifications are preserved.

Task 00 remains **passed**, with its HIL **not applicable**. Task 01's local coverage is complete; its separate required acoustic gate is pending. Next sequential test task: **02 — Provisional versus authoritative transcripts**, **not started**; finish or explicitly disposition Task 01's HIL requirement before advancing. **STOP: waiting for the next execution turn.**
