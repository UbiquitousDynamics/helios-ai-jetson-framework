# Final work order: stabilize the Helios voice stack, then prove it acoustically

You are **GPT Astra**, closing out the Helios AI voice work on the Emilia Jetson target.

This prompt is **self-contained**. The earlier work orders and their checkpoints have been
removed from the repository; everything you need is here. Two documents survive and remain
authoritative:

- `prompts/build_automated_voice_test_suite.md` — the measurement model, fixture catalog
  requirements, threshold policy and per-task test coverage for the voice test suite. Phase B
  below executes it; do not restate or redesign it.
- `docs/voice-test-suite-progress.md` — the test-suite checkpoint. Test Task 00 is complete;
  Test Task 01 is **blocked on acoustic hardware-in-the-loop evidence**. Resume from it.

Work in two phases, in order. **Phase B depends on Phase A** and must not start until Phase A
is complete: an acoustic measurement taken against a configuration that resets on reboot
measures nothing durable.

---

## Execution protocol

Work on exactly one numbered item at a time.

1. Read only the files that item needs.
2. Restate the item's scope and invariants in no more than five bullets.
3. Inspect the current implementation before editing.
4. Make the smallest coherent change that completes only that item.
5. Add or update focused unit, integration, concurrency, failure-path and regression tests.
6. Run the validation gate below and fix every implementation-caused failure until it passes.
7. Update `docs/voice-test-suite-progress.md` with the item ID, decisions, interfaces, test
   evidence, target-device evidence, and the next item ID.
8. Stop. Do not begin the next item in the same execution turn.

On the next turn: read the checkpoint, inspect the working tree, resume only the recorded next
item. Never redo a completed item unless its tests fail or the user asks.

If a prerequisite belongs to a later item, record it rather than implementing it early. If
blocked, report the precise blocker and stop. Keep context small: use symbol search and narrow
file ranges. Do not use sub-agents unless asked.

### Validation gate

No item is complete until all applicable checks pass:

1. Static/syntax validation for every changed Python file.
2. Focused unit tests for each new or changed behavior, including boundary values and invalid
   inputs.
3. Integration tests across every affected component boundary.
4. Cancellation, timeout, concurrency, race, cleanup and failure-injection tests wherever the
   item touches asynchronous or I/O behavior.
5. Relevant pre-existing regressions for conversation, recognition, barge-in, TTS, streaming,
   context, routing, privacy and shutdown.
6. The complete automated suite. A failure may be called unrelated only with reproducible
   evidence; record it, never hide it.
7. Target validation on Emilia whenever the change touches audio hardware, Vosk/Piper
   inference, system resources, Linux networking, Ollama, device timing or shutdown.

Deterministic fakes first, then the real target. Never proceed on a smoke test alone. Never
weaken, skip, delete or xfail a test to make the gate pass.

### Target access

`emilia@192.168.1.100`, SSH key already installed. Treat any credential as secret: never write
one into the repository, a checkpoint, source, tests, shell history, process arguments, logs or
reports. Do not change system packages, services, network configuration or unrelated files
without explicit approval. Deploy only what the current item needs, to a dedicated path.
Inspect target state before overwriting. Run bounded commands. Retain only sanitized,
content-free evidence. **Never record real user speech** — Phase B uses generated fixtures
only. If the device is unreachable, report the blocker and stop.

### Invariants

- Preserve public APIs unless an item explicitly adds a backward-compatible extension.
- Preserve provider-neutral canonical history in `ConversationSession`.
- Never send provisional STT text to an LLM, tool, memory store, RAG or remote service.
- Continue processing microphone input during generation, synthesis and playback.
- One microphone-capture owner; never competing PyAudio streams.
- Preserve privacy, provenance, connectivity, health, budget, fallback and no-replay gates.
- Keep stop, cancel, mute, privacy and session controls local and deterministic.
- Never report an external action complete before authoritative backend confirmation.
- Never log audio, transcripts, prompts, answers, credentials or headers.
- Bounded queues and timeouts, cooperative cancellation, dependency injection, deterministic
  tests.
- Do not modify binary models, voice/image/sound assets, or the RAG corpus.
- No heavy dependencies without approval and Jetson/JetPack resource analysis.
- **Never lower a detection threshold to make a symptom disappear.** If a threshold genuinely
  needs recalibration, that is its own item with its own measurement.
- Keep every existing test green.

---

## State of the system

Verified on the target on 2026-09-24.

| Item | Value |
| --- | --- |
| Device | `emilia@192.168.1.100`, hostname `ondasolare`, Jetson Nano Developer Kit |
| Kernel | Linux 4.9.253-tegra aarch64, L4T R32.7.1 |
| Deployment | `/home/emilia/helios-live-conversation-validation-20260919-claude` |
| venv | `/home/emilia/helios-ai-jetson-framework/venv/bin/python` (3.10.0) |
| Suite | 2,086 passed, 1 skipped on target |
| Commit | `47776cb`, dirty |

Architecture Tasks 00-16 are complete (`docs/live-conversation-final-audit.md`). Field
remediation P1-P9 is implemented in the repository, with P1 and P2 device actions prepared but
**not applied**.

### The microphone fault, and what actually fixed it

Worth reading before Phase A, because two plausible fixes were tried and only the third worked.

The Jetson's onboard analog input is **electrically dead** — 4 s of capture gave
`peak=0.000000` across 62,374 samples. The working microphone is on the USB dongle:

```
alsa_input.platform-sound.analog-mono                     peak 0.000000
alsa_input.usb-...USB_PnP_Audio_Device...analog-stereo    peak 0.085388
```

Setting PulseAudio's default source to the USB device was **not sufficient**. The ALSA→pulse
plugin does not honour it on this host, because `/etc/asound.conf` defines `pcm.!default`
toward the Tegra card while `/usr/share/alsa/alsa.conf.d/pulse.conf` defines `pcm.!default
{ type pulse }`. Same source, same moment:

```
parecord on the USB source          peak 0.027557
PortAudio "default" (Helios's path) peak 0.000000
```

What works is naming the source explicitly, which `_prepare_pulse_source()` already supports:

```
HELIOS_AUDIO_INPUT_DEVICE=pulse:alsa_input.usb-Solid_State_System_Co._Ltd._USB_PnP_Audio_Device_000000000000-00.analog-stereo
```

Confirmed on target:

```
event=capture_pulse_source_selected source=alsa_input.usb-...analog-stereo
Using configured microphone device index=17 name=pulse
event=capture_device_resolved requested=pulse:alsa_input.usb-... active_port=analog-input-mic
```

Also applied on the device, and **not persistent across reboot**: USB card profile
`output:analog-stereo+input:analog-stereo`; `Mic Capture Volume` 496/496 (+31 dB); PulseAudio
source volume 100%.

### Live session evidence, 2026-09-24 22:39-22:41

With the fix in place, one unscripted Italian conversation:

```
9 stt_finalized       5 assistant_turn_completed     0 assistant_turn_failed
1 wake word (first attempt, previously eight)        0 ERROR / Traceback / quota_exhausted
5 follow-ups accepted
```

All turns routed to `openai-codex` / `gpt-5.6-terra`, `first_text_ms` 3,107-4,062, no fallback.

Barge-in signal, before and after the fix:

| | before (22 Sep) | after |
| --- | --- | --- |
| `peak_rms` | 0.033 / 0.043 | 0.063 / 0.072 / **0.132** |
| `confidence` | 0.711 / 0.763 | **1.0** |
| `strong` | always `False` | `True` on the strongest |

Three interruptions cancelled cleanly (`speaking → cancelling → user_turn_finalized`,
`assistant_turn_interrupted`). Eleven suppressions, all legitimate: 5 `pre_playback_segment`,
3 `short_unconfirmed_final`, 3 `energy_only_reemit`. **No false barge-in appeared** with the
stronger signal.

Quiet-room ambient measured 0.0032-0.0059 against a `capture_level_low` threshold of 0.006, so
the warning fires on silence.

---

## Phase A — stabilize

### A1 — Persist the capture configuration

**Why first:** the fix currently lives only in an interactive launch command. On the next
reboot, or from any other launcher, the microphone goes silent again — which is how this fault
survived several days of testing.

Two layers, both needed:

- **Helios configuration.** `HELIOS_AUDIO_INPUT_DEVICE=pulse:<usb source>` must be set wherever
  Helios is launched on this device. `/etc/systemd/system/emilia.service` exists and is
  `disabled/inactive`; it already carries `Environment=` lines. Prepare the change; whether to
  enable the unit is the operator's call, not yours.
- **Host audio.** The USB card profile, the ALSA capture gain and the PulseAudio source volume
  were set interactively and do not survive a reboot. Determine whether
  `module-device-restore` persists them; if it does not, prepare the persistent form.

Both are device changes: prepare command, rollback and verification, then **stop and request
approval**.

Repository side: consider whether an unresolvable `pulse:` source should fail closed by
default rather than fall back to a device that may be silent. `input_device_strict` exists but
has **no environment binding** — add validated configuration for it.

**Verify:** reboot the device, confirm capture still resolves to the USB source and
`peak_rms` is non-zero without any manual step.

### A2 — Calibrate the capture level threshold

Real measurements now exist: ambient 0.0032-0.0059, speech 0.063-0.132, dead input exactly
0.000000. The current threshold of 0.006 sits inside ambient noise and fires on every quiet
start.

Choose a value that separates *dead* from *quiet* rather than *quiet* from *loud*, document the
measurement it came from, and bind it to configuration. This is calibration against evidence,
not threshold-weakening: state that distinction explicitly in the checkpoint.

Evaluate `HELIOS_AUDIO_INPUT_CHANNEL_MODE` while you are here. The default `mono` uses the
PortAudio downmix; `stronger` selects the higher-energy channel and avoids the ~6 dB penalty
when only one channel carries the microphone. Measure both on the target and pick with
evidence.

### A3 — Verify the cancellation unwind path

Every interruption logs `event=interrupted_response_unwound outcome=APIClientError`. That is
the expected shape — cancelling a live stream ends the request in error — but it has never been
confirmed that a genuine provider error cannot arrive by the same path and be misread as a
cancellation.

Add a test that distinguishes them. If they are already distinguishable, record that and move
on; this may be a no-op.

### A4 — Apply the prepared P1/P2 device mitigations, or record why not

These were prepared and never applied. Present each to the operator with command, rollback,
verification and honest risk. **Do not apply any of them yourself.**

- **Autosuspend.** Back up `/boot/extlinux/extlinux.conf`, add `usbcore.autosuspend=-1` to
  `APPEND`, reboot. Rollback: restore and reboot. Verify `cat /proc/cmdline` and
  `/sys/module/usbcore/parameters/autosuspend`. Unlikely to help: the panic had three URBs
  stuck *while active*, not during a suspend transition. A bad boot edit can prevent startup.
- **Powered hub.** Mic at Bus 01 Port 2.1, WiFi at Port 2.4, same hub. Move the mic to a
  powered hub on a different physical port only after the operator identifies that port and
  confirms Ethernet or local-console recovery. Verify `lsusb -t`, `pactl list short sources`,
  SSH. Bus numbering alone cannot identify a physical receptacle; a bad move can strand SSH.
- **Power mode.** `sudo nvpmodel -m 1`, rollback `-m 0`, verify `nvpmodel -q`. Current mode is
  MAXN though `/etc/nvpmodel.conf` declares `PM_CONFIG DEFAULT=1`. Lower rail current may help
  the hub; lower CPU clocks worsen CPU-bound local inference. Present both effects.
- **Watchdog.** `sudo sysctl -w kernel.watchdog_thresh=20`, rollback `10`. Hides the symptom,
  extends the unresponsive window, fixes nothing.
- **SD card.** `/dev/mmcblk1p1` is ext4 root with a 15 GiB `/swapfile`. At least 74 timeout and
  74 CRC lines in a wrapped `dmesg` sample. Options: offline image-and-replace with `ddrescue`
  from a verified by-id path; `sudo swapoff /swapfile` (rollback `swapon`, risk OOM); relocate
  logs and KPI via `HELIOS_LOG_FILE` and `HELIOS_KPI_STORAGE_PATH`. Track counts through
  `journalctl -k -b` with the boot ID, never wrapped `dmesg`.

### A5 — Complete the USB stability soak

The previous soak ran **585 seconds** against a required 30,300 — twice the 15,150 s survival
that preceded the recorded panic. It proves nothing and must not be cited as if it did.

`event=capture_stall_detected` is implemented (`HELIOS_AUDIO_CAPTURE_STALL_SECONDS`, default
5); it logs once and never reopens the stream, because teardown could itself trigger the panic.

**Before any soak:** `/sys/fs/pstore` holds exactly one panic and the soak is designed to
provoke another. Copies of `console-ramoops-0` and `dmesg-ramoops-0` were archived under
`logs/pstore-archive-20260924/` with verified SHA-256, and the originals cleared with approval.
Confirm the archive is intact and pstore is empty before starting, and record boot ID and
`/proc/uptime` at start and end.

If the soak cannot run to completion, say so plainly and mark USB stability **unverified**.
That is an acceptable outcome; a short run reported as a pass is not.

---

## Phase B — prove it acoustically

Only after Phase A. Execute `prompts/build_automated_voice_test_suite.md`, resuming from
`docs/voice-test-suite-progress.md` at Test Task 01, which is blocked on exactly the acoustic
path that Phase A has now made possible.

The full chain the suite must measure end to end:

```
Piper-generated stimulus -> PC output -> Emilia microphone -> Vosk / floor controller
  / LLM / TTS -> Emilia audio output -> PC microphone -> recorded response and metrics
```

Constraints beyond the suite document:

- **Generated fixtures only.** Never record real user speech, not even to calibrate.
- **Prevent acoustic feedback.** Headphones or isolated speakers, monitoring and loopback
  disabled, a unique test marker, and abort on runaway level or repeated self-triggering.
- **Require explicit device identifiers and a calibration step** before any HIL run. Never
  silently use an unintended microphone or speaker — this whole project lost days to exactly
  that.
- **Keep local deterministic tests separate from HIL results** in reporting and CI markers.
- If a threshold is not calibrated on this hardware, report `unverified` or `blocked`. Never
  invent one.
- If the device is unavailable, mark HIL `blocked`. Never convert it into a local pass.

Report per-metric definition, units, sample count, artifact source and threshold, with median,
p95, maximum and failure count. Do not average away outliers.

---

## Budget and provider constraints

**Codex credits are exhausted and cost real money.** Failures report `limit_id=premium` with
`credits.balance "0"` — an empty credit pool, not a rate-limit window, so waiting does not
help. Do not run `codex exec`, and do not design a verification that routes live turns to
Codex. Where the remote path is needed, mark it blocked on credits rather than spending them.

**Ollama has no GPU on this device.** `layers.offload=0`, `unsupported L4T version` — CUDA 12.8
userspace against an L4T R32.7.1 driver. Every local turn is CPU inference on four Tegra cores.
Cold start measured 37-44 s against a 30 s first-token budget, and `OLLAMA_KEEP_ALIVE=5m`
unloads the model during ordinary conversational pauses. Rebuilding Ollama for L4T R32.7.1 is
**out of scope**; treat this as a fixed property of the platform when setting expectations.

---

## Out of scope

Rebuilding Ollama; replacing the SD card yourself; purchasing Codex credits; changing the
kernel command line, USB topology, power mode or watchdog without approval; and the optional
future tracks — semantic camera/world state, persistent user memory, speaker diarization,
cross-device handoff, live translation, real tool or vehicle actuation, and proactive
assistance.

## Required report after every item

```text
Item: A1-A5 or Test Task NN — title
Status: completed | blocked | prepared-awaiting-approval
Changed: files and one-line purpose
Tests: exact commands and pass/fail counts
Gate: passed | blocked, with evidence
Emilia validation: passed | not applicable | blocked, with sanitized evidence
Device changes prepared: command, rollback, verification, risk — or "none"
Thresholds touched: none, or which and why, with the measurement
Invariants checked: concise list
Known limitations: concise list
Checkpoint: docs/voice-test-suite-progress.md updated
Next item: ID — title
STOP: waiting for the next execution turn
```

Stop after Phase B's final audit.
