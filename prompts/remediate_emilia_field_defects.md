# Remediation prompt: defects found in the Emilia field inspection

You are **GPT Astra**, continuing work on the **Helios AI** repository. This is a remediation
work order, not a feature request. A parallel read-only inspection of the Emilia Jetson target
on 2026-09-24, plus a live follow-up query, produced reproducible evidence for defects across
four layers: kernel/hardware, host audio configuration, model providers, and this repository.

Fix what belongs to this repository. **Prepare — but do not apply — anything that belongs to
the device.** The evidence is quoted inline. Do not re-derive it.

The execution protocol, mandatory test gate, target-device access rules, and global invariants
from `prompts/implement_live_conversation_architecture.md` remain in force. Three amendments
apply here and override the general protocol where they conflict:

1. Defects are addressed in the order given in "Sequencing", not in file order.
2. Every defect carries an **Owner**. Where the owner is `device operator`, prepare the exact
   command, the rollback, **and the verification command**, then stop and request approval.
   You may not change device system configuration on your own.
3. **Read "Already implemented" first.** Part of what the inspection reported as missing has
   since been built. Verifying that existing code behaves correctly on the target is real
   work; rebuilding it is waste.

---

## Evidence provenance

Two sources, deliberately kept distinct because they disagree in one place:

- **[INSP]** — the parallel read-only inspection, 2026-09-24, four dimensions, 34 findings.
  It read the **deployed tree** on the device, which lags this repository.
- **[LIVE]** — direct `pactl` queries run against the device on 2026-09-24 while Helios
  PID 15966 was actively capturing.

Where they conflict, [LIVE] is newer but narrower: it is one observation at one moment, and
[INSP] finding [0.4] gives a specific reason why one observation may not generalise. Both are
recorded below. Do not collapse them.

| Item | Value |
| --- | --- |
| Device | `emilia@192.168.1.100`, hostname `ondasolare`, NVIDIA Jetson Nano Developer Kit |
| Kernel | Linux 4.9.253-tegra aarch64, L4T R32.7.1 |
| Validation deployment | `/home/emilia/helios-live-conversation-validation-20260919-claude` (no `.git`) |
| Framework tree | `/home/emilia/helios-ai-jetson-framework` (has `.git`) |
| USB audio | Solid State System Co.,Ltd. "USB PnP Audio Device", `0c76:1203`, at `usb-70090000.xusb-2.1`, `maxpower=100mA`, `speed=12` |
| Local model | Ollama `emilia-gemma3:1b`, Q4_K_M |
| Remote provider | `openai-codex` via `codex_app_server`, codex-cli 0.144.4 |

Suite state when this was written: `2058 passed, 1 skipped` on the target. Keep it green and
state the new counts in every report.

---

## Already implemented — verify on target, do not rebuild

The inspection read the deployed tree and reported several capture-layer gaps. They have since
been closed in this repository. **Verified by reading the current source while writing this
document.** Your job for each is to confirm it behaves correctly *on the device*, not to
implement it again.

| Capability | Where it lives now |
| --- | --- |
| Capture identity logging | `recognizer/speech_recognizer.py:487` `_log_capture_identity()` emits `event=capture_device_resolved` with `requested`, `index`, `name`, `input_channels`, `device_rate`, `capture_channels`, `capture_rate`, `downmix`, `pulse_source`, `active_port` |
| Selection by index | `_resolve_input_device_index()` — `isinstance(configured, int)` branch |
| Selection by PulseAudio source | same function — `configured.startswith("pulse:")` branch, backed by `_prepare_pulse_source()` and `_selected_pulse_source` |
| Loud, non-silent fallback | `event=capture_device_fallback requested=%s available=unknown` |
| Fail-closed mode | `input_device_strict` constructor flag, raises `SpeechRecognitionError` instead of falling back |
| Capture level sanity check | `sanity_rms_threshold` constructor parameter, with `pcm16_rms` from `recognizer/barge_in_detector.py` |
| Configurable stereo downmix | `downmix_stereo_pcm16(data, mode)` at `recognizer/speech_recognizer.py:31`, `channel_mode` attribute |
| Run identity at startup | `main.py:65-74` emits `event=helios_run_identity tree=… commit=… dirty=… routing_config=… kpi_enabled=… kpi_store=… log_destination=…`, plus `event=helios_version_unknown` and `event=remote_routing_requested_without_config` |

### What is actually still missing here

- **`input_device_strict` and `sanity_rms_threshold` have no environment binding.** `config.py`
  reads `HELIOS_AUDIO_INPUT_DEVICE` (line 1954) but nothing sets the other two, so an operator
  cannot turn on fail-closed selection or the level check without editing code. Add validated
  configuration for both, defaulting to current behavior.
- **The deployment carries no version stamp.** `main.py` calls `read_identity(project_root)`,
  which depends on git metadata; the validation deployment has no `.git`, so every run there
  logs `commit=unknown` and `event=helios_version_unknown`. Make the sync step write a stamp
  file (commit SHA plus dirty flag) that `read_identity` can read when `.git` is absent.

---

## P1 — Kernel panic: the USB audio device wedges the xHCI controller

**Severity:** critical. Every run can end in an uncontrolled reboot.
**Owner:** device operator, with a limited repository-side contribution.

### Evidence [INSP]

From `/sys/fs/pstore` (Panic#1, 2026-09-23 23:49; the inspection cites the pstore carveout —
confirm whether the panic text is in `console-ramoops-0` or `dmesg-ramoops-0` before quoting a
filename in any report):

```
[15150.334874] mmc1: Data CRC error
[15168.400186] usb 1-2.1: timeout: still 3 active urbs on EP #1
[15169.404112] usb 1-2.1: timeout: still 3 active urbs on EP #1
[15171.435935] INFO: rcu_preempt detected stalls on CPUs/tasks:
[15171.441822]  0-...: (1 GPs behind) idle=091/2/0 softirq=153942/153942 fqs=2446
[15172.580094] tegra-xusb 70090000.xusb: xHCI host not responding to stop endpoint command.
[15172.588356] tegra-xusb 70090000.xusb: Assuming host is dying, halting host.
[15172.597356] tegra-xusb 70090000.xusb: HC died; cleaning up
[15180.180099] Kernel panic - not syncing: Watchdog detected hard LOCKUP on cpu 0
[15180.466662] Rebooting in 5 seconds..
```

`usb 1-2.1` is the capture device. Watchdog: `nmi_watchdog=1`, `watchdog_thresh=10`,
`panic=5`. All five recent boots end abruptly mid-log with no shutdown sequence. The USB
microphone and the WiFi adapter share one hub and one xHCI controller, which is why audio and
network die together.

**The ramoops buffer holds exactly one panic.** The next panic overwrites the only record any
of this rests on.

Also on the same 5 V rail: power mode is `MAXN` although `/etc/nvpmodel.conf` declares
`PM_CONFIG DEFAULT=1` (the 5 W profile), all four CPUs are at `scaling_max_freq`, and
`NVPM WARN: fan mode is not set!`.

### What to do

**Repository side — deliberately small.**

Be honest about efficacy before writing code: the panic happened *with URBs in flight*. A
capture-stream recycle closes and reopens the stream, which means more URB teardown, not less.
It is as likely to trigger the failure as to avoid it. **Do not implement a stream recycle as
a mitigation.** If you implement one at all, justify it on other grounds and default it off.

What is defensible:

- **Stall detection.** If no capture frame arrives within a configurable timeout while the
  stream is supposed to be running, emit `event=capture_stall_detected` with elapsed
  milliseconds. Do not automatically reopen — on this failure the controller is already dying
  and a reopen will block. Surface it and let the operator decide. Never spin.
- Nothing else. This is a driver-level fault and the repository cannot fix it.

**Device side (operator approval required — prepare, do not apply).** Each needs command,
rollback, verification, and honest risk:

- `usbcore.autosuspend=-1` on the kernel command line. State plainly that this is unlikely to
  address the recorded failure, since the panic occurred with three URBs actively in flight
  rather than during a suspend transition.
- Move the dongle to a different physical port and onto a **powered** hub, so the 100 mA mic
  and the WiFi dongle stop sharing a bus-powered hub. Specify exactly which port, and check
  first whether the device can still reach the network from the new topology — a bad choice
  here can strand the device with no way back in.
- `sudo nvpmodel -m 1` to return to the declared 5 W default, rollback `sudo nvpmodel -m 0`,
  verification `nvpmodel -q`. This lowers peak rail current feeding the hub. **State the
  conflict with P6 explicitly**: it also lowers CPU clocks, and P6 shows CPU speed is already
  the bottleneck for local inference. Do not recommend one over the other — present the
  trade-off and let the operator choose.
- Raising `watchdog_thresh`. Label it clearly as hiding the symptom, not fixing the cause, and
  note it lengthens the unresponsive window.

### Verification

**Step zero, before any soak run:** copy `/sys/fs/pstore/*` off the device. It holds exactly
one panic and the soak is designed to provoke another. Then clear pstore so a new panic has
somewhere to land. Skipping this destroys the only evidence.

Then: the longest observed survival before the recorded panic was roughly 15 150 s (about
4 h 12 m). A two-hour soak proves nothing. Run at least twice the longest observed survival,
or state the exposure honestly as insufficient. Record kernel uptime at start and end.

---

## P2 — Root filesystem SD card is failing

**Severity:** critical. Data-loss risk and a plausible contributor to P1.
**Owner:** device operator. Repository side: write-pressure reduction only, via P7 and P8.

### Evidence [INSP]

Counts from the current boot's `dmesg` — note the ring buffer had **already wrapped** (it
starts at t=229 s, past the boot banner), so these are lower bounds, not totals:

```
error -110 (timeout): 74
error -84 (CRC/EILSEQ): 74
Data CRC error: 73
single block retry: 32

[  229.172303] mmcblk1: error -110 sending stop command, original cmd response 0x900, card status 0x400900
[  229.172319] mmcblk1: error -84 transferring data, sector 71845456, nr 32, cmd response 0x900, card status 0x0
```

Card: `LX128`, manfid `0x0000ad`, date 08/2024, 117 GiB, SDR104 SDXC. `mmcblk1p1` is `/`
(116 G, 73% used) and hosts a 15 GB `/swapfile`. The previous three boots recorded zero such
lines. An `mmc1: Data CRC error` appears 18 seconds before the first stuck URB in the panic log.

Retries are currently succeeding: no `EXT4-fs error`, no I/O error, no read-only remount.

### What to do

**Repository side:** nothing specific. Reduce writes via P7 and P8 and cross-reference.

**Device side (operator approval required — prepare, do not apply):** present the options with
command, rollback, verification and risk for each — image and replace the card; move
`/swapfile` off the card or disable swap; relocate log and KPI directories. Do not choose for
the operator. State that a card producing CRC errors is expected to degrade further, and that
a 15 GB swapfile on the same card drives memory pressure straight into the faulty device.

### Verification

Because the ring buffer wraps, per-boot `dmesg` counts are not comparable. Either persist
counts to storage at a fixed interval, or read from the journal rather than `dmesg`, and say
which you used. Do not claim improvement from one quiet boot.

---

## P3 — Capture routing is wrong and is not pinned

**Severity:** critical for usability. The most likely cause of the weak-signal barge-in failures.
**Owner:** device operator. Repository side is already built — verify it on target.

This defect has two parts that the two evidence sources describe differently. Both are real.
Do not treat either as settled.

### Evidence part A — the card runs the digital input profile [LIVE]

Observed by direct `pactl` query on 2026-09-24 while Helios PID 15966 was capturing:

```
Source Output #43
    Source: 2
    application.name = "ALSA plug-in [python3.10]"
    application.process.id = "15966"

Active Profile: output:iec958-stereo+input:iec958-stereo
Active Port:    iec958-stereo-input: Digital Input (S/PDIF) (priority: 0)
device.string = "iec958:2"
device.profile.name = "iec958-stereo"

# card profile list, from the same query:
output:analog-stereo+input:analog-stereo   priority: 6060   available: yes   <- not selected
output:iec958-stereo+input:iec958-stereo   priority: 5555   available: yes   <- active
input:analog-stereo                        priority: 60     available: yes
input:iec958-stereo                        priority: 55     available: yes
```

The `6060`/`5555` figures are **[LIVE] only** — they do not appear in the [INSP] evidence,
which recorded the port-level `60`/`55` priorities. Re-confirm them before relying on them.

At that moment Helios was on source 2, the USB device — **not** the Tegra onboard input.

### Evidence part B — PulseAudio's default source points at the wrong card [INSP]

The inspection's highest-ranked finding says the opposite of part A, and its evidence stands:

```
pactl info
  Default Source: alsa_input.platform-sound.analog-mono

pactl list short sources
  4  alsa_input.platform-sound.analog-mono  module-alsa-card.c  s16le 1ch 44100Hz  SUSPENDED
```

Helios logs `input_device_index=default` 660 times and never a numeric index, so which source
it lands on is decided by PulseAudio, not by Helios.

**Why both can be true, and why this matters:** [INSP] finding [0.4] records that PulseAudio
started roughly 73 seconds *after* Helios (process ELAPSED: helios 13:23, pulseaudio 12:10),
with `module-device-restore`, `module-stream-restore`, `module-switch-on-connect` and
`module-default-device-restore` all loaded. `module-stream-restore` remembers per-application
routing, which plausibly explains why this particular run landed on source 2 despite the
default pointing elsewhere. The inspection's own conclusion applies: **capture routing is not
reproducible across runs, so a single observation may not represent the next run.**

Treat part A as "the profile is wrong" and part B as "the routing is not pinned". Fixing one
without the other leaves the defect half-open.

### Evidence — gain is NOT the problem [INSP]

```
numid=8 'Mic Capture Volume': values=464, min=0, max=496   ->  93.5%, +29.00 dB of a 31.00 dB max
numid=7 'Mic Capture Switch': on
Mono: Playback 280 [56%] [17.50dB] [off]  Capture 464 [94%] [29.00dB] [on]
```

About 2 dB of headroom remains and the card exposes no AGC or boost control. Observed levels:
`frame_rms=0.002 peak_rms=0.033` and `frame_rms=0.016 peak_rms=0.043` (peak 0.043 FS =
-27.3 dBFS), `strong=False`, candidates rejected `reason=final_not_confirmed`.

Do not "fix" this by raising gain. There is nowhere to raise it to.

### What to do

**Device side (operator approval required — prepare, do not apply).**

```
# A: switch the USB card to the analog duplex profile
pactl set-card-profile \
  alsa_card.usb-Solid_State_System_Co._Ltd._USB_PnP_Audio_Device_000000000000-00 \
  output:analog-stereo+input:analog-stereo

# A rollback
pactl set-card-profile \
  alsa_card.usb-Solid_State_System_Co._Ltd._USB_PnP_Audio_Device_000000000000-00 \
  output:iec958-stereo+input:iec958-stereo

# B: pin the default source (run AFTER A — the source name changes)
pactl set-default-source \
  alsa_input.usb-Solid_State_System_Co._Ltd._USB_PnP_Audio_Device_000000000000-00.analog-stereo

# B rollback
pactl set-default-source alsa_input.platform-sound.analog-mono

# verification for both
pactl list cards | grep -A1 'Active Profile'
pactl list short sources
pactl info | grep -E 'Default Source|Default Sink'
```

Order matters and must be stated to the operator: **switching the profile destroys the
`.iec958-stereo` source and creates a `.analog-stereo` one under a different name.** Any saved
default referring to the old name stops resolving, so step B must run after step A, and the
verification must confirm it. Also verify the card name is character-exact — a typo makes the
command fail silently from the operator's point of view.

Establish whether `module-device-restore` persists either choice across reboot. If it does not,
prepare the persistent form for separate approval. Note that both commands restart the PCM
stream and will disturb a running Helios.

**Repository side — verification, not construction.** The logging, level check, downmix control
and pulse-source selection already exist (see "Already implemented"). What you must do:

- Confirm `event=capture_device_resolved` reports the **true** `pulse_source` and `active_port`
  on the target, before and after the device change. If it reports `unknown`, that is a defect
  in `_pulse_identity()` worth fixing.
- Wire `sanity_rms_threshold` to configuration so the level check can actually be enabled, and
  calibrate a default from measurements taken **after** the routing is fixed.
- Assert the resolved capture source **at every stream open**, not only at startup — part B
  above shows PulseAudio can move it mid-session.

### Verification

Re-measure `frame_rms` and `peak_rms` on identical spoken phrases before and after, and report
both. The barge-in thresholds must **not** be lowered to compensate.

Anticipate the opposite problem: if the signal jumps from -27 dBFS to a normal level, echo
suppression and barge-in thresholds calibrated against the weak signal may start producing
false interruptions. Measure the false-barge-in rate after the change. If recalibration is
genuinely needed, that is a separate change with its own evidence — not a quiet edit.

---

## P4 — Codex failures are an empty premium credit pool, not a rate-limit window

**Severity:** critical for the remote path. Waiting does not fix it.
**Owner:** user decision for the account; repository for correct classification.

### Evidence [INSP]

Rate-limit snapshots from `/home/emilia/.helios-codex/sessions`, numeric fields only:

```
2026-09-22 13:22 (healthy run):
  {"limit_id":"codex","plan_type":"plus",
   "primary":{"resets_at":1790088601,"used_percent":58.0,"window_minutes":300},
   "secondary":{"resets_at":1790539837,"used_percent":40.0,"window_minutes":10080},
   "credits":{"balance":"0","has_credits":false}}

2026-09-22 13:47 (failing run):
  {"limit_id":"premium","plan_type":null,"primary":null,"secondary":null,
   "rate_limit_reached_type":null,
   "credits":{"balance":"0","has_credits":false,"unlimited":false}}

2026-09-20 10:38 (failing run): same shape — limit_id "premium", credits balance "0"
```

`resets_at` 1790088601 = 2026-09-22 16:50:01 CEST, the 300-minute window — **not** the pool
that refused. About twenty-five minutes before the failure the standard pools were at 58% (5 h)
and 40% (weekly).

Authentication is not the blocker: `Logged in using ChatGPT`, `auth_mode chatgpt`, both
profiles aligned, network to OpenAI healthy.

`grep` over `api/providers/codex_app_server.py` finds **no** handling of `credits`,
`has_credits`, or `rate_limit` today — everything collapses into `category=quota_exhausted`.

### What to do

First establish what the provider layer actually receives. The snapshots above were read from
session files on disk; if that structure never reaches `codex_app_server.py`, the distinction
cannot be made from the inputs at hand and **the honest answer is to say so** rather than to
start parsing session files as a side channel. Report which it is before implementing.

If the information is available:

- Distinguish an exhausted rate-limit window (retry after `resets_at` is meaningful) from an
  empty credit balance (retry is guaranteed to fail), in the log event and in any
  operator-facing message. Never log tokens or raw session content.
- Do not auto-retry a credit-exhaustion refusal within a session.
- Check how this interacts with the existing provider-health circuit breaker before adding new
  retry logic, so the two do not fight.
- Add tests over recorded, sanitized responses of both shapes.

### Verification

Unit tests over both shapes. **Do not run `codex exec` or any live inference** — it consumes
the user's remaining quota, and the pool is already empty.

---

## P5 — Ollama runs on CPU and the local first-token budget is unreachable

**Severity:** critical. The fallback fails precisely when it is needed.
**Owner:** shared. The repository can fix the budget logic; the CUDA mismatch is structural.

### Evidence [INSP]

```
level=INFO source=gpu.go:612 msg="Unable to load cudart library /usr/lib/aarch64-linux-gnu/tegra/libcuda.so.1.1:
  symbol lookup for cuCtxCreate_v3 failed: undefined symbol: cuCtxCreate_v3"
level=INFO source=cuda_common.go:54 msg="unsupported L4T version"
  nv_tegra_release="# R32 (release), REVISION: 7.1, ... BOARD: t210ref"
level=INFO source=server.go:175 msg=offload library=cuda layers.requested=-1 layers.model=27 layers.offload=0
load_backend: loaded CPU backend from /usr/local/lib/ollama/libggml-cpu.so
level=INFO source=ggml.go:362 msg="model weights" buffer=CPU size="1.0 GiB"
```

`layers.offload=0` is decisive: no GPU acceleration. One CPU core at 98.3%.

The 2026-09-22 failure:

```
first_visible_token_seconds = 30.0     (per-target, already configurable in llm-routing.device.toml)
OLLAMA_KEEP_ALIVE = 5m0s

13:45:55,335  request sent
13:46:09      ollama: "llama runner started in 13.09 seconds"
13:46:25,330  connection close.started
13:46:26,331  WARNING stream_worker_stop_unacknowledged stop_reason=timeout
13:46:26,335  assistant_turn_failed
llm-metrics.jsonl: {"error_category":"first_token_timeout","latency_ms":31006.8}
```

Cold start alone measured 37.32 s on 2026-09-24 and 44.33 s on 2026-09-21 — larger than the
entire 30 s budget. With a 5-minute keep-alive the model unloads during any normal pause.

Note that `api/providers/contracts.py:93` still carries `first_token_seconds: float = 20.0` as
the built-in default; the 30 s value is a per-target override.

Memory pressure: the inspection recorded two MemFree snapshots three minutes apart that differ
substantially. Quote both or quote the range — do not cite only the worst. `open-webui` spawns
a second `ollama serve` driving traffic into the same single-instance Ollama.

### What to do

**Repository side:**

- Separate **cold-start time from first-token time**. A budget that must also absorb an
  unpredictable model load is not a first-token budget. Either detect the load phase and apply
  a distinct allowance, or pre-warm before the budget starts. Make both phases separately
  observable in metrics.
- Emit a distinct `error_category` for a cold-load-induced timeout, so it stops being
  indistinguishable from a genuinely stalled stream.
- The per-target timeout is already configurable — do not add a second knob. Instead document
  that 30 s is **not** calibrated for CPU inference on this hardware, and propose a calibrated
  value from your measurements.

**Device side (operator approval required — prepare, do not apply):**

- Longer `OLLAMA_KEEP_ALIVE`, with its memory cost stated against the measured free-memory
  range.
- Stopping `open-webui` and its second `ollama serve` during voice testing.
- Rebuilding Ollama against L4T R32.7.1 is **out of scope** — record it as a known structural
  limit.

### Verification

Report cold-start and warm first-token latencies **separately**, with median, p95 and maximum
over at least 20 samples each. Do not average them together.

---

## P6 — KPI telemetry drowns the voice events it exists to expose

**Severity:** warning, but it obstructs diagnosis and writes to the failing SD card.
**Owner:** repository.

### Evidence [INSP]

All-time, 12 189 rows over 19–24 September:

```
network_probe_completed   6047
resource_sample           3143
voice_listen_completed    2041
...
wake_word_detected          17
tts_completed               10
voice_command_failed         1
```

In the first ~150 s after the 00:08 restart, 71 rows: 32 `network_probe_completed`,
21 `resource_sample`, 6 `voice_listen_completed`, 5 `network_state_changed`, and one each of
`assistant_started`, `llm_route_decided`, `tts_completed`, `voice_command_completed`,
`wake_word_detected` — 70 enumerated. Treat the breakdown as near-complete, not exact, and
re-query if the exact mix matters.

Background telemetry is 9 190 of 12 189 rows all-time. The database reached 10.4 MB and its WAL
regrew to 1.4 MB within two minutes of restart; `pragma wal_autocheckpoint = 1000` pages.

`resource_sample_interval_seconds` is already configurable (`config.py:76`). The network probe
persistence rate is not.

### What to do

- Make the **persistence** rate of network probes configurable and independent of the probe
  interval that drives routing. The routing decision needs frequent probes; the database does
  not need every one.
- Consider recording `network_probe_completed` only on change, with `network_state_changed` as
  the event of record — but keep probe health measurable by adding a periodic summary row.
- Add retention/rollup for background telemetry separate from voice events.
- Cross-reference P2: fewer writes is less pressure on a failing card.

### Verification

Re-measure the event mix over a comparable window; report the ratio before and after.

---

## P7 — Log and database hygiene on the device

**Severity:** warning.
**Owner:** mixed — read each item, the owner differs.

### Evidence [INSP]

```
head -1 app.log   -> 2026-07-28 02:03:24,677 - INFO
last entry        -> 2026-09-24 00:05:31,741 - INFO
7333 lines contain NUL bytes
grep -nE 'Traceback|CRITICAL|ERROR' app.log | tail -3
  3310:2026-08-12 20:36:50,396 - ERROR - assistant - Recoverable runtime error
  3311:Traceback (most recent call last):
  Binary file /home/emilia/helios-ai-jetson-framework/app.log matches
```

`app.log` spans two months with no rotation. Because of the NUL bytes, plain `grep` stops at
the binary content and silently returns only entries up to 12 August.

**Do not assume the repository writes those NUL bytes.** The likelier explanation is truncated
writes from the uncontrolled reboots in P1 — a file left mid-write is zero-filled by the
filesystem. Establish which before hunting for a logging defect: if the NUL runs cluster at
crash boundaries, this is a P1 artifact and the fix is rotation plus the crash fix, not a
logging change.

Abandoned KPI database in the framework logs directory, last written 9 August:

```
-rw-r--r-- 3805184 ago  9 17:33 helios-kpi.sqlite3
-rw-r--r--   32768 ago  9 17:33 helios-kpi.sqlite3-shm
-rw-r--r-- 2109472 ago  9 17:33 helios-kpi.sqlite3-wal
```

Orphaned `-shm`/`-wal` means the owning process was killed rather than closed. Again: with P1
producing hard lockups, a clean-shutdown path cannot prevent this — an unclean WAL is expected
after a panic and SQLite recovers it on next open. Scope the fix accordingly.

A stale `kpi-dash.pid` from 22 September names dead PID 18168 while port 8765 is unbound.
[INSP] finding [3.6] records that log rotation on restart already works correctly and no logs
were lost.

### What to do

- **Size-bounded rotation** for the application log — this is the durable fix and it is
  repository work.
- Investigate the NUL bytes' provenance before treating them as a logging defect; report what
  you find either way.
- Make pid-file liveness checks verify process identity, not just PID existence.
- Make the active KPI store path unambiguous — `event=helios_run_identity` already logs
  `kpi_store`, so confirm it is sufficient rather than adding another line.
- Existing on-device artifacts (the August database, the stale pid file) are **operator**
  cleanup, not repository work. List them; do not delete them yourself.

### Verification

A rotation test asserting the size bound. For WAL handling, test that a store opened after an
unclean close recovers without data loss — that is the reachable guarantee, not "no orphaned
WAL ever exists".

---

## Sequencing

1. **P3, device side.** The single highest-value change for usability, and everything about
   audio measurement downstream depends on the signal being real. Prepare both commands and
   request approval first, because approval takes wall-clock time you can spend elsewhere.
2. **Verify "Already implemented" on target.** Confirm `event=capture_device_resolved` reports
   truthfully, and add the missing configuration bindings and the version stamp. Cheap, and it
   makes every later measurement trustworthy.
3. **P4 and P5** — provider classification and the cold-start/first-token split. Together these
   decide whether any turn completes at all.
4. **P6 and P7** — telemetry ratio and log rotation. These reduce SD-card write pressure.
5. **P2** — prepare the card options for the operator. Do this **before** P1's soak run, not
   after: the soak deliberately provokes the failure that is degrading the card.
6. **P1** — stall detection, and the prepared device changes with the pstore preservation step.
   Last, because it mitigates a fault the repository cannot fix and its verification requires
   everything above to work.

---

## Constraints specific to this work order

- **Never lower a detection threshold to make a symptom disappear.** If a threshold genuinely
  needs recalibration after an upstream fix, that is a separate change with its own
  measurement.
- **Never record or retain audio, transcripts, or token material** while implementing capture
  diagnostics. Scalars only.
- **Do not apply device system changes.** Prepare command, rollback, verification and risk;
  then stop and ask.
- **Codex quota is exhausted and costs real money.** Do not run `codex exec`, and do not design
  any verification that routes live turns to Codex. Where a verification would otherwise need
  the remote path, state that it is blocked on credits rather than spending them.
- Keep the suite green and state the counts in every report.
- Where a fix cannot be validated because the device is unreachable or has rebooted, mark it
  `blocked` with evidence. Do not convert a local pass into a target claim.

## Explicitly out of scope

Rebuilding Ollama against L4T R32.7.1; replacing the SD card; purchasing Codex credits;
changing the kernel command line or watchdog settings without approval; and any optional future
track from the original implementation plan.

## Required report

Use the standard report block from the primary prompt, plus one of these per defect. `Owner`
values are exactly: `repository`, `device operator`, `shared`, or `user`.

```text
Defect: Pn — title
Owner: repository | device operator | shared | user
Status: fixed | prepared-awaiting-approval | blocked | out-of-scope | already-implemented-verified
Evidence before: the measurement that showed the defect
Evidence after: the same measurement after the change, or why it could not be taken
Device changes prepared: command, rollback, verification, risk — or "none"
Thresholds touched: none, or which and why, with measurement
```
