# Test Task 02 — Provisional versus authoritative transcripts

Status: **blocked on required acoustic HIL**. The complete local authority gate passed. Task 01's separate acoustic floor-mapping gate remains blocked, and Task 00 remains passed with HIL not applicable. [Sanitized summary](task02-results.json) and the ignored raw result directories below retain the evidence. Production code and existing thresholds were not changed.

## Implemented local increment

The [typed event contract](../../tests/voice_suite/task02-contract.json) v1.0.0 contains 10 English/Italian scenarios and 36 events: 10 provisional revisions, 14 authoritative finals and 12 ignored stale/duplicate/empty events. It covers revised wording, duplicate finalization, stale capture/segment/revision, capture restart, identical text in distinct identified turns, a final with no preceding partial, and a partial-only timeout. The [profile](../../tests/voice_suite/task02-profile.json) v1.0.0 requires at least 10 actual samples per invariant and zero errors; its desktop and Jetson entries have identical local logical limits. The profile does not calibrate acoustic timing.

The [authority test group](../../tests/test_voice_suite_authority.py) exercises the real transcript aggregator, realtime controller and conversation history with typed synthetic recognizer events. It checks that provisional values cannot enter model, RAG, control/tool, history or provider text boundaries, that one identified final is admitted, and that concurrent duplicates and failures leave no pending transcript. One integration test feeds silent synthetic PCM through the real recognizer/assistant/API wiring with injected Vosk and provider stubs; it verifies that partials do not call the provider and only the declared final reaches its prompt. This is **local logic evidence**, not speech recognition or acoustic evidence.

The [Task 02 runner](../../scripts/voice_suite_task02.py) executes the versioned contract through the real `TranscriptRevisionAggregator`, measures the three local invariants from actual event/case/boundary samples, and writes content-free `events.jsonl`, `cases.jsonl` and `results.json` with one opaque correlation ID per scenario and monotonic `perf_counter_ns` timestamps. It fails closed on missing identity fields, weakened limits, missing coverage and invalid versions. The [runner test group](../../tests/test_voice_suite_task02_runner.py) verifies those failure paths, output-root isolation, no transcript text in reports, true per-metric sample counts, and that a populated synthetic manifest alone never produces a HIL pass. No Piper waveform, audio stream or remote provider was used by the local runner.

## Final local validation

Selected dedicated PC interpreter: Windows 11 AMD64, Python **3.12.10**, pytest **8.4.2**, Ruff **0.16.8**, numpy **2.5.3**. Its monotonic `perf_counter_ns` resolution was **0.0000001 s**. Only `sounddevice` **0.5.6** was added to this dedicated environment for read-only PC device enumeration; PortAudio reported **V19.7.0-devel**. Piper, Vosk, PyAudio and ONNX Runtime remain absent there. The existing Emilia virtual environment reported Python **3.10.0**, Piper **1.6.0**, Vosk **0.3.45**, sounddevice **0.5.5**, PyAudio **0.2.14**, ONNX Runtime **1.23.2** and PortAudio **V19.6.0-devel**. These package versions do not establish an acoustic exchange.

Exact final validation commands, from the repository root:

```powershell
$taskPython = Join-Path $env:TEMP 'helios-live-conversation-task00-20260918/Scripts/python.exe'
& $taskPython -m py_compile scripts/voice_suite_task02.py tests/test_voice_suite_authority.py tests/test_voice_suite_task02_runner.py
& $taskPython -m ruff check scripts/voice_suite_task02.py tests/test_voice_suite_authority.py tests/test_voice_suite_task02_runner.py
& $taskPython -m pytest -q tests/test_voice_suite_authority.py tests/test_voice_suite_task02_runner.py --tb=short
& $taskPython -m pytest -q -m 'not hardware and not integration and not remote_live and not voice_hil' --tb=short
& $taskPython scripts/voice_suite_task02.py --artifact-dir .voice-test-artifacts/task02 --profile desktop --hardware-manifest tests/voice_suite/hardware.example.json
& $taskPython scripts/voice_suite_task02.py --artifact-dir .voice-test-artifacts/task02 --profile jetson --hardware-manifest tests/voice_suite/hardware.example.json
git diff --check
git check-ignore .voice-test-artifacts/task02/
```

Syntax and Ruff passed. Focused Task 02: **29 passed, 0 failed, 0 skipped in 1.02 s**. Full repository local gate under the explicit environment marker filter: **2043 passed, 0 failed, 1 skipped, 1 deselected in 23.57 s**. The skip is `tests/test_jetson_launcher.py:45`, because this Windows account cannot create the test's Unix-style symlink. The deselected case is the `remote_live` provider test `tests/test_live_llm.py::test_configured_remote_provider_streams_without_exposing_content`. `git diff --check` passed. The historical Task 00 **51 focused / 227 relevant** and Task 01 **603 focused / 1177 relevant** results remain preserved in their reports; they were not rewritten as new measurements.

Both CLI commands exited **2** as designed: local authority `passed`, HIL `blocked`, overall `blocked`. The Jetson profile was evaluated on the PC and is not a target run. Their final ignored artifact directories are:

- Desktop: `.voice-test-artifacts/task02/8250499e-9985-4401-ae73-1c80698a0afb/`
- Jetson profile on PC: `.voice-test-artifacts/task02/09d45154-39e7-444a-8271-13a4f8b60c59/`

Each has 10 content-free case records and 36 content-free event records. Every event carries run, scenario correlation, host and clock IDs plus a monotonic timestamp. The raw files contain no transcript text. Both results retain source/spec/harness hashes and the exact profile. Local invariant measurements are the same in each independent run:

| Invariant | Source/unit | n | Median | p95 | Maximum | Failures | Limit |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Authority classification mismatches | `events.jsonl` / events | 36 | 0 | 0 | 0 | 0 | 0 |
| Authoritative final-count mismatches | `cases.jsonl` / scenarios | 10 | 0 | 0 | 0 | 0 | 0 |
| Provisional boundary admissions | `events.jsonl` / observed provisional events | 10 | 0 | 0 | 0 | 0 | 0 |

These are logical error counts, not zero latency. Acoustic end-to-end, STT, TTS, WER/CER, playback/capture-to-floor mapping, response completeness and acoustic cleanup measurements have **n=0**, null median/p95/maximum, and `unverified` status.

## Device inventory and acoustic gate

The PC `sounddevice`/PortAudio inventory was read without opening a stream. It exposed **25** endpoints across MME, DirectSound, WASAPI and WDM-KS. WASAPI candidates included index **10** `SAMSUNG (NVIDIA High Definition Audio)` and index **11** `Speaker (Realtek(R) Audio)` (each 2 output channels, default 48 kHz), plus index **12** for the Intel Smart Sound microphone group (2 input channels, default 48 kHz). The microphone name was corrupted in PortAudio's returned text encoding, so it cannot be used as an exact-name selection. WDM-KS also exposed `Missaggio stereo`, a loopback input that must never be selected. No PC endpoint was chosen as an isolated physical role.

Emilia was enumerated read-only over SSH. One query returned **13** ALSA endpoints; a subsequent query returned **24** and shifted indices. The second included `USB PnP Audio Device: Audio (hw:2,0)` at ALSA index **11**, with 2 input and 2 output channels and a default 44.1 kHz rate, but its physical microphone/speaker wiring was not verified. Other ALSA/virtual endpoints were also present. No default or candidate was promoted to a selected role, and no target stream was opened. The read-only commands used were:

```powershell
& $taskPython -m pip install sounddevice --no-input --disable-pip-version-check --quiet --retries 0
& $taskPython -c "import sounddevice as sd; print(sd.query_hostapis()); print(sd.query_devices())"
ssh emilia@192.168.1.100 "/home/emilia/helios-ai-jetson-framework/venv/bin/python3 -c 'import sounddevice as sd; print(sd.query_devices())'"
ssh emilia@192.168.1.100 "cd /home/emilia/helios-ai-jetson-framework && venv/bin/python3 -c 'import sounddevice as sd; print(sd.get_portaudio_version()); print(sd.query_hostapis()); [print(i, sd.query_devices(i)) for i in range(len(sd.query_devices()))]' && aplay -l && arecord -l"
```

The four explicit PC/Emilia input/output selections, physical isolation, monitoring/loopback disablement, absence of people from the capture area and generated-Piper-only capture remain unconfirmed. No `hardware.task02.json` with selected roles or calibration artifact was produced because that would imply facts the inventory cannot establish. A valid future calibration must bind the four freshly re-enumerated exact device identities and sample-rate/channel choices to a versioned, hashed manifest, then record measured response level, peak limit, clock uncertainty and procedure evidence before setting `calibrated`. Only after those checks can a bounded, feedback-aborting, cleanup-verified PC → Emilia → PC adapter run. The current local runner cannot make that claim from a filled manifest and always reports acoustic execution as blocked.

HIL: **0 exchanges**, **0 streams opened**, no generated WAV and no captured audio. The PC's remaining native packages were not installed because the physical prerequisites and calibration were unavailable. No audio or transcript files were created. Cleanup passed for the executed local tests and runner, with pending transcript buffers cleared and test workers joined. Task 02 remains **blocked**, as does Task 01's earlier acoustic gate. Task 03 is next but **not started**; stop here.
