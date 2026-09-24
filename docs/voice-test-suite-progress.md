# Voice test suite progress

## Repository baseline repair — requirement source, 2026-09-25

Root cause: commit `0759dd4` deleted `docs/live-conversation-progress.md` without
changing the 45 `source` paths in `tests/voice_suite/traceability.json`. The
current voice-suite checkpoint is this file; the final implementation audit
also still links to the deleted source. No replacement containing the original
requirement rows was found. The deletion was committed, not an unstaged
working-tree deletion; its intent cannot be inferred from the commit message.
The historical Git blob has all 45 rows, and their SHA-256 matches the
traceability catalog's `requirement_source_sha256`. Its full-document digest
does not match the catalog's older `source_document_sha256`; the validator
uses the row digest and checks every requirement against those rows.

Changed in this repair: restored `docs/live-conversation-progress.md` byte for
byte from `0759dd4^` and updated only this checkpoint. No test, runtime,
configuration, or deployment file changed. The directly affected test failed
before restoration with `repository_file_missing` and passed afterward (1/1).
Active TTS tests passed (25/25). The earlier full-suite baseline was 2,083
passed, 13 failed, 2 skipped with pytest's temporary directory forced inside
the repository. After restoration, that invocation had 2,091 passed, 6 failed,
2 skipped: all six remaining failures were the voice-suite guard requiring
artifacts under the system temporary directory. With pytest's default system
temporary directory, the complete suite passed: **2,097 passed, 2 skipped**.
The two skips were unavailable Windows symlink privilege and a live remote
test lacking explicit enablement. Remaining baseline test failures under the
supported invocation: **none**. The missing documentation was restored from
Git history; the traceability test reference was not changed.

## A2 deployment preflight — read-only evidence, 2026-09-25

After the public-key-only attempt recorded below failed, the operator explicitly authorized interactive password SSH for this read-only preflight. The credential was not placed in a command argument, file, report, or checkpoint. No device write or deployment occurred.

- Live checkout: `/home/emilia/helios-ai-jetson-framework`, branch `feature/natural-voice-conversation`, commit `b86781c7c424ce3a9972198b3a8f0470025461f1`. Git status: 32 untracked entries (`??`), zero tracked changes; only status metadata was read, not their contents. The separate A2 validation tree exists but is not a Git repository.
- Unit: `/etc/systemd/system/emilia.service`; `WorkingDirectory=/home/emilia/helios-ai-jetson-framework`; `ExecStart=/home/emilia/helios-ai-jetson-framework/venv/bin/python3 /home/emilia/helios-ai-jetson-framework/main.py`. Drop-ins: `20-online-routing.conf` and `30-capture-source.conf`. The service remains `disabled` / `inactive`. Effective audio environment contains the explicit USB PulseAudio source, strict=true, capture floor=0.001, and channel mode=mono.
- Disk: root filesystem `/dev/mmcblk1p1`, 116 GiB total, 80 GiB used, 31 GiB available (73%); 6,935,026 free inodes. Live checkout apparent size 2.1 GiB. This does not remove the previously recorded SD CRC/timeout risk.
- Live SHA-256: `config.py` `c0a78960f26b02a31136b04531c046b1135c8ef1e2f68b4e562583f21e5cb7cb`; `assistant.py` `837eae89a370cad0669297ec05c643c831584f5b28277cb732b21ee999bc072b`; `recognizer/speech_recognizer.py` `c2aeaf8df35f00b503460a07ce659b9a189a7f0c842477bd2b0f6f525f058fca`; `main.py` `9d46b21f64a924360d93dd8bf0bbf27812a4f5b6b34c71c176bf83ed43159186`.
- Drop-in SHA-256: current `30-capture-source.conf` `bebe2720a9c0062fb51171a4fac3a38075d00a9019610b0641516eb4d7ce86ad`; retained `30-capture-source.conf.a2-before` `5953060b1dad12ae4b12d2531ec97b5ed754f22b068c4224615d29d2405d2f81`.
- Proposed paths: `/home/emilia/helios-a2-release-47776cb-20260925` **absent**; `/home/emilia/helios-ai-jetson-framework.a2-before-20260925` **absent**. Existing `/home/emilia/helios-a2-validation-20260924` is present.
- Validation environment: `/home/emilia/helios-ai-jetson-framework/venv/bin/python` 3.10.0; pytest 8.4.2; Ruff 0.16.0; PyAudio 0.2.14; Vosk 0.3.45; piper-tts 1.6.0; ONNX Runtime 1.23.2; NumPy 2.2.6; httpx 0.28.1. Version metadata only was queried; no inference or provider call ran.

Preflight is complete. The older live recognizer lacks the newer `input_device` constructor argument, so overwriting only `config.py` and `assistant.py` would be incompatible. A full compatible, explicitly scoped release and rollback plan is required before deployment. No transfer, staging, promotion, backup, service action, reboot, A3 work, user-audio/credential/log read, or provider call occurred. **A2 deployment awaits separate approval; STOP.**

## A2 deployment preflight — public-key authentication blocked

On the operator's read-only, public-key-only authorization, one connection probe was attempted: `ssh -o BatchMode=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o ConnectTimeout=6 emilia@192.168.1.100 true`. It exited 1 with sanitized error: `emilia@192.168.1.100: Permission denied (publickey,password).` The parenthesized methods are the server's advertised methods; password fallback was disabled on the client and no password was requested or used. No further target command was run. Thus live commit/status, service details, disk space, live file hashes, drop-in hashes, staging/rollback path existence, service state, and dependency versions could not be freshly inspected. Prior A2 checkpoint evidence remains historical and is not presented as this preflight's result. No write, transfer, staging, service action, reboot, provider call, or audio/credential/log read occurred. **A2 deployment remains blocked on authorized public-key SSH access; separate approval is required before deployment. STOP.**

## Phase A — A2: Calibrate the capture level threshold (2026-09-24)

Status: **A2 calibration and repository gate passed; separately approved drop-in values staged; compatible live-code deployment not authorized**. Selected diagnostic floor: **0.001 normalized PCM16 RMS**, independently configured by `HELIOS_AUDIO_CAPTURE_LEVEL_MIN_RMS` with positive finite validation. Selected channel mode: **`mono`**, retaining the existing default. This is calibration of the `capture_level_low` dead-input warning against measured quiet levels, not weakening a speech or barge-in detection threshold. No detection threshold changed. Exact per-trial scalar measurements, definitions, source/sink identities, cleanup and limitations are in the [A2 calibration report](voice-test-suite/a2-calibration.md).

Emilia on boot `8c01da32-a045-45e9-94b5-300373784a83`: 5 paired Piper trials per mode, 10 successful bounded playbacks/captures, 501 100 ms RMS frames total; no recorded PCM or transcript. Lowest quiet median was 0.002329; earlier dead input was 0.000000. Across five trials per mode, median quiet RMS was 0.002925 (`mono`) and 0.003228 (`stronger`); `stronger` increased quiet p95 in 3/5 pairs and improved the stimulus/quiet p95 ratio in only 2/5. Hence `mono` is retained. The generated WAV was removed, no audio streams remained, and the service stayed disabled/inactive. No reboot, service start, drop-in edit, routing edit, or provider request occurred.

Changed: `config.py` adds `audio_capture_level_min_rms` and environment parsing/validation; `assistant.py` passes it to `SpeechRecognizer` instead of deriving it from barge-in energy; `README.md` documents it; focused tests in `tests/test_config_llm.py`, `tests/test_recognizer.py`, and `tests/test_assistant.py` cover default/override/invalid values, boundary warning behavior, and the settings-to-recognizer boundary. Syntax parsing of all five changed Python files, Ruff, and `git diff --check` passed. In a dedicated A2 validation tree on Emilia, `PYTHONDONTWRITEBYTECODE=1 /home/emilia/helios-ai-jetson-framework/venv/bin/python -m pytest -q tests/test_config_llm.py tests/test_recognizer.py tests/test_assistant.py -p no:cacheprovider -m 'not remote_live'` passed **195**; full `PYTHONDONTWRITEBYTECODE=1 timeout 600 /home/emilia/helios-ai-jetson-framework/venv/bin/python -m pytest -q -m 'not remote_live' -p no:cacheprovider` passed **2097**, 1 remote test deselected. Both exited 0. With separate operator approval, the drop-in was backed up byte-for-byte as `30-capture-source.conf.a2-before`, then only the selected floor and `mono` mode were appended and `systemctl daemon-reload` exited 0. Backup SHA-256 `5953060b1dad12ae4b12d2531ec97b5ed754f22b068c4224615d29d2405d2f81`; new drop-in SHA-256 `bebe2720a9c0062fb51171a4fac3a38075d00a9019610b0641516eb4d7ce86ad`. Effective environment reports both new values plus the A1 USB source and strict=true. Service remains `disabled/inactive`; no reboot, service start/enable, live-code deployment, A3 work, or provider call occurred. The service still points to an older development checkout and does not yet consume the new threshold binding. **Next item: A2 — compatible live-code deployment requires separate authorization before A3. STOP.**

## Phase A — A1: Persist the capture configuration (2026-09-24)

Status: **A1 completed after approved reboot and generated-only acoustic capture probe**. The existing repository implementation already binds `HELIOS_AUDIO_INPUT_STRICT` through strict boolean parsing in `config.py`; `Settings.audio_input_strict` reaches `SpeechRecognizer`, whose strict `pulse:` path raises when the named source is unavailable. Existing tests cover true and invalid values. No threshold was changed.

Read-only inspection over interactive SSH succeeded after the operator supplied access. `emilia.service` is disabled/inactive and lacks the explicit input selector. The USB source is present, at 100% volume, with the desired duplex profile and 496/496 capture gain. PulseAudio card/device restore modules and databases are present. The ALSA boot state is stale: `/var/lib/alsa/asound.state` stores USB `Mic Capture Volume` 464/496, while the live control is 496/496. No reboot or post-reboot capture check occurred.

The [A1 device change record](voice-test-suite/a1-device-change-plan.md) contains the approved commands, rollback, verification, risks and SHA-256 evidence. Both backups were absent before creation. Backup of `emilia.service`, backup of `asound.state`, drop-in creation, `systemctl daemon-reload`, and `alsactl store 2` all exited 0. Service and backup hashes match: `61341d7d71a0161e5fc0557104cf9d8cba8513066fabbe6cae07306c729f224d`. ALSA backup hash is `c0623618ee3c276a0123f10f16bd600c039b57c2d79c6449593e0103d84cec8b`; new ALSA state hash is `136266d16d442ae52664bda50f2d5e3b846c2a1fd7ca759ceef6ebc8c5055a9d`; drop-in hash is `5953060b1dad12ae4b12d2531ec97b5ed754f22b068c4224615d29d2405d2f81`. Sanitized read-only output: service `disabled/inactive`; effective `HELIOS_AUDIO_INPUT_DEVICE=pulse:alsa_input.usb-Solid_State_System_Co._Ltd._USB_PnP_Audio_Device_000000000000-00.analog-stereo`; `HELIOS_AUDIO_INPUT_STRICT=true`; live Mic capture 496/496 (+31 dB); stored Mic Capture Volume 496; USB source at 100% and duplex profile active. No audio stream, service start, reboot, routing change, or remote provider request occurred. PulseAudio restore across reboot remains unverified. Repository interfaces and source were unchanged; local pytest was unavailable (`python -m pytest` reported no pytest), so the full validation gate and target post-reboot verification remain pending.

With separate operator approval, Emilia rebooted. Boot ID changed from `19d561b0-2beb-42ca-a828-b17b3755caa9` to `8c01da32-a045-45e9-94b5-300373784a83`; post-boot uptime was about 78 seconds. Without manual audio setup, `emilia.service` remained `disabled/inactive`, its USB PulseAudio selector and strict setting persisted, USB duplex profile and 100% source volume persisted, and live/saved Mic capture both read 496/496. Backup and drop-in hashes were unchanged; details are in the A1 device change record.

With approval for generated-only acoustic verification, a 1.916 s Piper fixture played once through the explicit USB sink and the deployed recognizer captured through the explicit USB source in strict mode. Both `capture_pulse_source_selected` and `capture_device_resolved` identified the USB source and `analog-input-mic`. In 50 100 ms frames, initial-eight-frame median RMS was 0.014027 and maximum RMS was 0.081973; playback and probe exited 0. An empty decoder produced no transcript; no audio samples were retained, the temporary WAV was deleted, and the audio streams closed. The older development checkout rejected the probe's constructor argument before audio opened; the successful run used the current validation deployment. A1 acoustic evidence is one bounded sample and does not calibrate recognition thresholds or prove Phase B HIL metrics. The complete target suite command `PYTHONDONTWRITEBYTECODE=1 timeout 600 /home/emilia/helios-ai-jetson-framework/venv/bin/python -m pytest -q -m 'not remote_live' -p no:cacheprovider` passed **2086 tests, 1 deselected**, exit 0. No Python source, threshold, service running state, routing, or remote provider was changed. The preexisting test-suite checkpoint below records Task 02 local work despite this work order identifying Task 01 as the Phase B resume point. Phase B has not started under this work order. **Next item: A2 — Calibrate the capture level threshold. STOP.**

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
