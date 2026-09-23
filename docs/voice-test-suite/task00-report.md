# Test Task 00 — Baseline and requirement traceability

Status: **passed**. Task 00 is documentation/specification-only. Its local baseline and exhaustive applicable gate passed on the documented local checks; HIL is **not applicable** because this task executes no runtime audio. The HIL requirement is deferred to the first test-suite task that actually exercises audio. No conversation behavior or acoustic requirement is marked verified. Task 01 has not started.

Implementation under test: `d0446f0bb04ac74aaebd59f858ee0ba5170a4fb2` plus the existing worktree. [Sanitized results](task00-results.json) identify 16 production files by SHA-256 and record the harness, specifications and voice model/config hashes. This increment changes only `scripts/voice_test_suite.py`, `tests/test_voice_suite_baseline.py`, `tests/voice_suite/`, pytest markers, the artifact ignore rule and these documents. Existing production modifications and deletions were preserved.

## Baseline contents and inspection

- [Fixtures](../../tests/voice_suite/fixtures.json), version **1.0.0**: **46**, comprising **23 Italian and 23 English** specifications. All seven fixture categories and all seven control intents occur in both languages. Recipes declare pauses, corrections, repetition, overlapping Piper speech, seeded noise, silence and TTS echo. These are catalog entries, not 46 executed acoustic tests.
- [Metrics](../../tests/voice_suite/metrics.json), version **1.0.0**: **52** definitions and **38** timestamp events, with units, source, sample unit, aggregation and direction. PC acoustic intervals and target stages use their own monotonic clock domains. Raw cross-host timestamps must never be subtracted without calibrated synchronization. A marker, wake acknowledgement, echo or backchannel cannot satisfy first valid answer audio.
- [Thresholds](../../tests/voice_suite/thresholds.json), version **1.0.0**: separate desktop and Jetson profiles, proposed for review. First answer audio has a configurable **5000 ms p95 provisional planning limit**, with minimum **20** eligible samples. It is not an Emilia measurement or an accepted deployment limit. Other acoustic/resource limits are null until calibrated. Logical invariants require zero prohibited events or complete correctness; count metrics require at least one run. Provisional or missing thresholds cannot establish a calibrated pass.
- [Hardware template](../../tests/voice_suite/hardware.example.json), version **1.0.0**: four explicit roles, unset device IDs, unverified calibration, isolation requirements, bounded exchange/session/shutdown limits and default audio deletion. It is a specification template, not a device selector or implemented calibration command. Future HIL must bind each role to API/index/name/rate/channels and calibration evidence before opening a stream, and implement level/retrigger abort and cleanup.
- [FR traceability](../../tests/voice_suite/traceability.json): all **FR-01–FR-45**, source text/lines and planned test-task mappings, with separate `local_evidence=not_run` and `hil_evidence=blocked`. The source-row SHA-256 is `255c8ec167bb92b7ae644046dc07aedeb4fdcfd8f5db70e9cc89fa3b4d1faaa5`. The baseline validator checks the requirement IDs, source-row hash and exact repository criteria. Original long requirement text is a preserved source snapshot, not an independently re-fetched document on every run.

Inspected `config.py` language/device profiles, `audio/tts.py` synthesis and playback, `recognizer/speech_recognizer.py` Vosk/device handling, `scripts/doctor.py`, `scripts/device_voice_diagnostics.py`, `scripts/benchmark_kpi.py`, `api/metrics.py`, `observability/resources.py`, their relevant tests, and earlier inventory/corpus/acoustic probes under `audit/jetson_gates_20260903/scripts/`. Existing diagnostic playback is not a PC-to-Helios-to-PC conversation test. Earlier corpus generation also used eSpeak near-end speech; this catalog requires the repository Piper path for every speech segment. Earlier Emilia validation used memory playback and cannot count as physical HIL.

The implementation checkpoint reports implementation tasks 00–10 complete and 11 next. Its FR ratings are historical. Optional or unsupported requirements mapped only to 00/16 remain explicit audit gaps; task delegation/steering prerequisites are not implemented by this test-suite baseline. Pending runtime evidence in the FR catalog does not block Task 00's structural baseline validation.

## Reproduction and local validation

Windows 11 AMD64; Python **3.12.10**, pytest **8.4.2**, numpy **2.5.3**, httpx **0.28.1**, Ruff **0.16.8**. The base `python` executable lacks pytest, so validation used the existing isolated interpreter below. No dependency install or environment change was needed. Piper, Vosk, sounddevice, PyAudio and ONNX Runtime distributions are absent from this interpreter. Current device/JetPack/native versions are unverified; historical target versions are not reused as fresh evidence.

```powershell
$taskPython = Join-Path $env:TEMP 'helios-live-conversation-task00-20260918/Scripts/python.exe'
& $taskPython -m py_compile scripts/voice_test_suite.py tests/test_voice_suite_baseline.py
& $taskPython -m ruff check scripts/voice_test_suite.py tests/test_voice_suite_baseline.py
& $taskPython -m pytest -q tests/test_voice_suite_baseline.py --tb=short
& $taskPython -m pytest -q tests/test_voice_suite_baseline.py tests/test_doctor.py tests/test_device_voice_diagnostics.py tests/test_kpi_metrics.py tests/test_kpi_benchmark.py tests/test_kpi_resources.py tests/test_pytest_bootstrap.py tests/test_tts.py tests/test_recognizer.py tests/test_transcripts.py --tb=short
& $taskPython scripts/voice_test_suite.py --artifact-dir .voice-test-artifacts/task00 --profile desktop
& $taskPython scripts/voice_test_suite.py --artifact-dir .voice-test-artifacts/task00 --profile desktop
& $taskPython scripts/voice_test_suite.py --artifact-dir .voice-test-artifacts/task00 --profile jetson --require-hil
git diff --check
git check-ignore .voice-test-artifacts/task00/16e453ec-f634-4f10-8813-170703aed487/results.json
```

Syntax and Ruff passed. Focused: **51 passed, 0 failed, 0 skipped in 1.55 s**. Complete relevant regression gate including those cases: **227 passed, 0 failed, 0 skipped in 2.63 s**. The 51 are included in 227, not additional successes. Full repository tests were not required for this isolated baseline increment. `voice_local` and `voice_hil` markers are registered separately; only local baseline tests exist in this increment. There is no fake passing/skipped acoustic test.

Negative coverage includes duplicate JSON keys/IDs, missing FRs/metrics/events, source drift, non-Piper recipe identifiers, wrong sample rates, private fixtures, repository path escapes, unintended device defaults, missing/unbounded timeouts, calibration without evidence, nonfinite values, insufficient samples, weighted WER denominators, p95 outliers, and false HIL pass claims. The initial run exposed a validator column-index error against the requirement table; correcting the parser to read the criterion column resolved the failures. No production change or threshold relaxation was used.

Both desktop invocations exited **0** for local baseline validation. The historical `--require-hil` guard exited **2**, reporting `blocked` and local `passed`. This observed result is preserved in the raw artifacts and published historical guard record; it is **not an applicable Task 00 acceptance gate**. The command ran on the same Windows PC with the Jetson threshold profile; it is **not a Jetson execution or acoustic attempt**. The command always refuses acoustic execution in Task 00. No SSH or remote provider was invoked. The documented local checks satisfy the **Task 00 exhaustive gate: passed**. This documentation correction preserves the original 51 focused and 227 regression results without rerunning tests or changing production code, the harness, thresholds or calibration.

Three runs had identical specification, harness, production-file, model/config and environment fingerprints, with distinct opaque correlation IDs. Both models have **22050 Hz** metadata: `it_IT-paola-medium.onnx` and `en_GB-alba-medium.onnx`. The configured synthesis method is `audio.tts.PiperTTS.synthesize_wave`. It exposes no seed guarantee; actual reproducible stimuli require future real Piper generation with exact dependency metadata and pinned PCM/WAV hashes. No audio was generated in this baseline.

## Metrics, artifacts and deferred HIL

| Measurement | Samples | Median | p95 | Maximum | Disposition |
| --- | ---: | ---: | ---: | ---: | --- |
| Baseline validation duration, ms | 3 | 359 | 430.1 | 438 | Observational only; no acceptance threshold |
| First answer/final answer/STT/model/TTS/endpointing/interruption | 0 | null | null | null | Unverified |
| WER/CER/exact-final, dispatch, barge-in, completeness/overlap | 0 | null | null | null | Unverified |
| Task acknowledgements/transitions, CPU/RAM/thermal/power/queues | 0 | null | null | null | Unverified |

Percentiles use R-7 interpolation, matching the existing KPI benchmark convention. The measured baseline clock is `time.monotonic_ns`, with reported resolution **0.015625 s**; these validation timings must not be mistaken for voice latency. Individual metric definitions/units/thresholds and zero-sample records appear in each ignored `metrics.jsonl`. No acoustic sample or absent resource is represented as a measured zero.

Each local run directory contains sanitized `results.json` and `metrics.jsonl`:

- `.voice-test-artifacts/task00/16e453ec-f634-4f10-8813-170703aed487/`
- `.voice-test-artifacts/task00/60392c5a-425e-4c60-8294-a5d69d987ccb/`
- `.voice-test-artifacts/task00/21a7738f-fa6b-4445-9d78-e32f3fac1df6/`

[Published results](task00-results.json) preserve environment, fingerprints, commands and sanitized aggregate evidence. The artifact root is git-ignored. The CLI requires an explicit output directory, rejects repository source directories, and creates a new UUID child for each run. Only sanitized JSON is produced; the template's audio retention policy will apply when an acoustic runner is implemented.

Task 00 HIL gate: **not applicable**; **0 acoustic exchanges, 0 failed or blocked gates**. HIL is deferred to the **first test-suite task that actually exercises audio**. Explicit PC/Helios devices, calibration/isolation confirmation, native dependencies and acoustic runner implementation are prerequisites for that future task, not blockers for this specification-only baseline. Target availability was not probed and is not inferred from old reports. Before real HIL, that task must confirm device identities, isolation/no real speech, monitoring disabled, calibrated response/runaway detection, unique markers, retention, bounded processes and device release. No calibration is invented for Task 00.

Cleanup: **passed**. The baseline opened **0** streams, started **0** audio/model workers, generated **0** audio files and recorded **0** transcripts. The bounded Git identity subprocess completed. No real speech, credentials, environment secrets or raw sensitive transcripts were collected.

Next test task: **01 — Conversation events and floor states**, not started. Apply the deferred HIL requirement when a task first exercises audio. Stop and wait for the next execution turn.
