# Emilia field-remediation checkpoint

Source: the 2026-09-24 remediation work order in
`prompts/remediate_emilia_field_defects.md`. This is a repository worktree
checkpoint, not a claim that code has been deployed to the live framework tree.
The live framework, kernel command line, USB topology, watchdog, SD card,
swapfile, services, and credentials were not changed. The USB PulseAudio profile
was changed to analog after explicit user approval; Helios was stopped first.

## Resume state

- P9: repository fix implemented and locally/isolated-target validated.
- P4: repository fix implemented and locally/isolated-target validated.
- P3: repository logging, scalar level check and selectable downmix implemented;
  target hardware before/after measurement awaits the profile change.
- P3 device change: approved and applied; USB analog source/default/port verified.
- P5-P8 and the repository part of P1: implemented and isolated-target tested.
- P2 and P1 device actions: prepared below; no action applied.

## P9 — run identity and version stamp

`main.py` logs an absolute tree, resolved routing file or explicit local-only
default, KPI enablement/store, log destination, and commit/dirty identity.
It warns when remote routing is requested without `HELIOS_LLM_CONFIG`.
`runtime_identity.py` reads a deployment stamp or Git identity. A source sync
should invoke `python -m scripts.stamp_deployment SOURCE DEPLOYMENT` **after**
copying the source into the deployment; it writes only commit SHA and dirty
boolean to `DEPLOYMENT/.helios-version.json`. Untracked source files count as
dirty. A deployment without `.git` and without a valid stamp logs `unknown`.

Local syntax/Ruff and focused tests passed. The full local gate at P9 was
**2,060 passed, 2 existing skips**. The final combined remediation gate was
**191 focused passed**, **2,075 full passed, 2 existing skips**. Isolated Emilia gate:
`/home/emilia/helios-remediation-validation-20260924`, static checks passed,
**8 focused passed**, **1,143 full passed** at P9; the final combined gate was
**191 focused passed**, **1,158 full passed**. The stamp command wrote the
framework source's Git SHA and dirty flag to the isolated copy only. No live
Helios launch from either tree was attempted because an active USB capture can
trigger the documented kernel panic; the startup-line comparison remains a
deployment verification, not a measured runtime result.

## P4 — capture-device selection

`HELIOS_AUDIO_INPUT_DEVICE` accepts a PortAudio index, a whole or substring
input-device name, or `pulse:<exact PulseAudio source name>`. The PulseAudio
source is checked through read-only `pactl list short sources` and applied to
the Helios process's `PULSE_SOURCE` before PortAudio initialization. The
process-local value is restored on recognizer close. Exact names take priority
over substrings. Indices must be in range and input-capable. On any unmatched
or ambiguous selection, `event=capture_device_fallback` names the request and
enumerated devices. `HELIOS_AUDIO_INPUT_STRICT=true` instead fails closed;
default `false` preserves the requested fallback behavior. This changes the
earlier source's strict default, so operators should opt into strict mode for
production microphone pinning.

`event=capture_device_resolved` records requested selector, resolved PortAudio
index/name, input channels, device/capture rates, downmix mode, PulseAudio
source and active port where queryable. No audio is logged.

Local and isolated Emilia static checks passed. Final focused/full counts are
recorded above. A read-only no-stream target probe emitted:
`requested=default index=23 name=default input_channels=32
device_rate=44100.0 capture_channels=1 capture_rate=16000 downmix=mono
pulse_source=alsa_input.platform-sound.analog-mono active_port=analog-input`.
This is a later boot than the field evidence and does not replace its USB
source-output observation. No PCM capture was opened for this probe.

## P3 — signal visibility and channel choice

Each opened capture stream logs the resolved identity. During the first
configured window, only peak RMS is retained; after one second, a peak below
one tenth of the existing minimum interruption energy produces
`event=capture_level_low`. This is a sanity warning during quiet startup, not
proof of a hardware fault. No raw audio or transcript is retained. The
default `HELIOS_AUDIO_INPUT_CHANNEL_MODE=mono` preserves current PortAudio
downmix. `average`, `sum`, and `stronger` request two channels then convert
interleaved PCM16 to mono before Vosk and energy checks; `stronger` chooses the
channel with greater frame energy, preserving a one-channel microphone signal
without the 6 dB average penalty. No barge-in/endpoint threshold was lowered.
Synthetic local and Emilia tests passed as part of P4's gates. A real capture
level before/after measurement is pending operator action.

Read-only pre-approval check on 2026-09-24: the USB card still reports
`output:iec958-stereo+input:iec958-stereo`; Helios PID 15966 is running from
`/home/emilia/helios-live-conversation-validation-20260919-claude` (verified
from `/proc/15966/cwd` immediately before the switch). The profile switch
would disturb that live process.

### Device change — approved and applied

Before the command, stop Helios through its normal shutdown path because
switching the card profile restarts the PCM stream. In the device operator's
PulseAudio session:

```sh
pactl set-card-profile \
  alsa_card.usb-Solid_State_System_Co._Ltd._USB_PnP_Audio_Device_000000000000-00 \
  output:analog-stereo+input:analog-stereo
```

Rollback:

```sh
pactl set-card-profile \
  alsa_card.usb-Solid_State_System_Co._Ltd._USB_PnP_Audio_Device_000000000000-00 \
  output:iec958-stereo+input:iec958-stereo
```

Verification, before and after restarting Helios: `pactl list cards` must show
`Active Profile: output:analog-stereo+input:analog-stereo`; `pactl list
sources` must show the USB analog source and `Active Port:
analog-input-mic` or the card's actual analog input port. Use
`pactl info` to confirm the default source, or set the exact source through
`HELIOS_AUDIO_INPUT_DEVICE=pulse:<source>` and
`HELIOS_AUDIO_INPUT_STRICT=true`. Then measure `frame_rms` and `peak_rms` on the
same spoken phrases; compare with field values 0.002/0.033 and 0.016/0.043.
Do not raise capture gain or lower barge-in thresholds. After reboot, repeat
`pactl list cards` to test persistence. If the profile reverts, prepare a
persistent PulseAudio configuration for separate approval.

Risk: the profile switch interrupts an active microphone/speaker stream, and
the USB controller has already panicked under capture. It may not persist
across reboot. With explicit user approval, Helios PID 15966 was checked by
its command and actual working directory, then stopped with SIGINT; it exited
within three seconds. `pactl set-card-profile` succeeded. The USB card now
reports `output:analog-stereo+input:analog-stereo`; the default source is
`alsa_input.usb-Solid_State_System_Co._Ltd._USB_PnP_Audio_Device_000000000000-00.analog-stereo`,
with `s16le 2ch 44100Hz` and active port `analog-input-mic`. No speech was
recorded or retained. Same-phrase after-switch RMS, reboot persistence and
long-run USB stability remain unverified.

## P5 — Codex credit classification

`credit_exhausted` now identifies the nonrenewing premium pool, distinct from
`rate_limited` usage windows. The field snapshots had zero premium credits
while standard 5-hour and weekly pools were 58% and 40% used. A credit refusal
blocks provider health until new evidence clears it; no paid Codex request was
made. Local full gate: **2,077 passed, 2 existing skips**. Isolated Emilia:
Ruff clean, **90 focused**, **1,189 full tests**.

## P6 — local Ollama cold load

The local adapter checks `ollama ps` for model residency. An unloaded model
gets an empty local-only warm-up before the visible-token clock starts; that
phase remains inside the total request budget. A cold timeout is
`cold_load_timeout`. Successful turns carry separate `cold_load_ms` and
`warm_first_token_ms` content-free metrics. The 30-second mode override is
uncalibrated for CPU-only inference. Two recorded cold loads were 37.32 and
44.33 seconds; 20 cold and 20 warm samples are still needed for median, p95,
and maximum. A provisional 60-second visible-token and at least 120-second
total setting is a measurement candidate, not a calibration. No live inference
was run. Isolated Emilia: Ruff clean, **94 focused**, **1,213 full tests**.

Device actions for separate approval: set `OLLAMA_KEEP_ALIVE=30m` in the
Ollama service environment, then `sudo systemctl daemon-reload && sudo
systemctl restart ollama`; rollback to the current `5m` and restart; verify
`systemctl show ollama -p Environment` and `ollama ps` after five minutes.
Keeping roughly 1.0 GiB of model weights resident longer costs memory on a
4 GiB device; remeasure free memory at more than one point. Confirm the unit
with `systemctl list-units '*open-webui*'`, then `sudo systemctl stop
open-webui` for voice testing; rollback `sudo systemctl start open-webui`;
verify `systemctl is-active open-webui` and `pgrep -af 'ollama serve|open-webui'`.
Check the second Ollama process's parent before stopping it. None applied.
CUDA/L4T R32.7.1 incompatibility remains out of scope.

## P7 — KPI write pressure

Routing probes keep their cadence. Routine probe *persistence* defaults to
once per 60 seconds (`HELIOS_KPI_NETWORK_PERSIST_INTERVAL_SECONDS`); each
saved row counts skipped probes. Every network state change persists.
`network_probe_completed` and `resource_sample` raw rows roll up after one day
(`HELIOS_KPI_BACKGROUND_RETENTION_DAYS`); voice raw rows retain the configured
14-day default. This reduces writes to the failing SD card (P2). The field
baseline was 9,190 background rows out of 12,189 (75.4%). A comparable
post-deployment ratio is pending. Isolated focused gate: **64 passed**.

## P8 — log and database hygiene

`app.log` rotates at 5 MiB with three backups; a size-bound test passed. The
current framework log has two NUL runs: 2,382 bytes at offsets
366,258–368,640 around 2026-08-12 timestamps, and 417 bytes at
454,239–454,656 between 2026-09-23 23:47:41 and 23:52:01. The latter
straddles the 23:49 panic, supporting crash truncation as a hypothesis, not
proof. A subprocess test wrote a SQLite WAL transaction and exited without
closing; reopening recovered the voice event.

The framework tree's `logs/helios-kpi.sqlite3` is 3,825,664 bytes and had no
WAL/SHM at the latest read. The active standalone dashboard PID 17904 runs
from `/home/emilia/helios-live-conversation-validation-20260919-claude` and
has that tree's KPI database/WAL/SHM open. The stale `kpi-dash.pid` from the
inspection was absent. No repository code consumes pid files. An operator
must verify both `ps -p "$pid" -o args=` and `readlink -f "/proc/$pid/cwd"`
against the expected command and tree before acting on any saved PID. Existing
artifact cleanup needs separate approval. The Helios startup identity reports
its active `kpi_store`; standalone dashboard identity requires checking its
open file descriptors as above.

## P2 — root SD card options, prepared only

`/dev/mmcblk1p1` is the ext4 root. `/swapfile` is 15 GiB; it had zero bytes
used at the latest read, but a future RAM shortfall would write to the faulty
card. The wrapped dmesg sample had at least 74 timeout and 74 CRC/EILSEQ
lines. A card already producing CRC errors can degrade further.

1. Image and replace **offline**. `sudo poweroff`, remove the card, identify
   its whole-device by-id path on another host with `lsblk -o NAME,SIZE,MODEL,SERIAL`,
   then `sudo ddrescue -f -n /dev/disk/by-id/<verified-card> emilia-sd.img
   emilia-sd.map` followed by `sudo ddrescue -d -r3
   /dev/disk/by-id/<verified-card> emilia-sd.img emilia-sd.map`. Verify image
   size and SHA-256; retain the old card as rollback. Risk: a wrong device
   path can destroy data, and live-root imaging is inconsistent. The by-id
   path cannot be fixed until the imaging host sees the card.
2. Temporarily disable SD swap with `sudo swapoff /swapfile`; rollback `sudo
   swapon /swapfile`; verify `swapon --show --bytes` and `free -h` before and
   after. Risk: out-of-memory failure when RAM/zram fills. A persistent fstab
   edit needs separate approval.
3. Relocate logs/KPI to a verified mounted external filesystem by setting
   `HELIOS_LOG_FILE` and `HELIOS_KPI_STORAGE_PATH` for the next launch.
   Rollback to `app.log` and `logs/helios-kpi.sqlite3`; verify `findmnt
   <mountpoint>`, `event=helios_run_identity`, and `/proc/<pid>/fd` targets.
   Risk: media loss interrupts writes; moving live SQLite needs a stopped
   process and checkpoint. No file was moved.

Track kernel CRC/timeout counts through `journalctl -k -b` with the boot ID
(`cat /proc/sys/kernel/random/boot_id`) at fixed intervals. Wrapped `dmesg`
counts are not comparable across boots.

## P1 — USB panic: diagnostic and prepared operator choices

The repository logs `event=capture_stall_detected elapsed_ms=...` if a running
stream returns no frame for `HELIOS_AUDIO_CAPTURE_STALL_SECONDS` (default 5).
It logs once and never reopens the stream: the panic had three active URBs,
so teardown could trigger it. Final isolated Emilia gate: changed-file Ruff
clean, **63 focused**, **1,244 full tests**. Whole-tree Ruff reports 50
preexisting errors in the isolated copy's `validate_task10_native.py`; no
remediation file failed. No live capture or soak was attempted.

Before any soak, preserve both actual panic files:
`/sys/fs/pstore/console-ramoops-0` (4,717 bytes) and
`dmesg-ramoops-0` (99,112 bytes). Create
`~/helios-pstore-archive/$(date +%Y%m%d-%H%M%S)`, `sudo cp -a
/sys/fs/pstore/* <created-directory>/`, then verify sizes and
`sha256sum <created-directory>/*`. Only after the copies are verified, clear
the originals with `sudo rm /sys/fs/pstore/*`; that destructive clear needs
explicit approval. Record boot ID and `cat /proc/uptime` at start/end. Soak
longer than 30,300 seconds, twice the prior 15,150-second survival;
two hours is insufficient.

An attempted off-device copy into this workspace's ignored `logs/` directory
was rejected by automatic approval review because kernel panic logs may contain
sensitive data and that destination was not specifically authorized. At that
point no copy or clear occurred; the later approvals and actions are recorded
below.

### Operator approvals and live soak, 2026-09-24

The operator subsequently approved copying exactly `console-ramoops-0` and
`dmesg-ramoops-0` to this workspace's ignored
`logs/pstore-archive-20260924/`. SHA-256 matched the originals before and
after copying: `a1593da3112b0c32965d4ca6783018cd30348e3d8924c127e7d78b3e98a5b84b`
and `c615998e2313ecef6c6f3a7f7ccdcf4e79c93dfa2bec3e299b9c0cc886c78626`.
With separate approval, only those two originals were removed; pstore was
verified empty. Separate approval then authorized the soak exceeding 30,300 s.

Two initial launch attempts exited with code 2 before opening audio because
the isolated test copy lacked a launcher and runtime assets. Its launcher was
copied and its models/static assets linked from the framework tree without
duplicating model weights on the SD card. The no-audio doctor reported path
escape errors for those symlinks and exceeded its 90-second native-import
budget; this is a limitation of that validation layout, not a passing doctor
result. No framework source or system configuration was changed.

The actual bounded soak started **2026-09-24 01:27:19 CEST** from
`/home/emilia/helios-remediation-validation-20260924`, PID 31472, boot ID
`36f91cad-dc4c-4f9b-9172-039858fab65c`, uptime 5,879.59 s. The source
stamp is commit `d0446f0bb04ac74aaebd59f858ee0ba5170a4fb2`, dirty true.
`logs/emilia-soak-supervisor.py` in the target copy checks the process, boot
ID and pstore, writes content-free status to `logs/emilia-soak-status.json`
every five minutes, and stops the process after **30,360 s** (planned about
09:53:19 CEST) if it survives. Remote Codex routing is disabled. Capture
identity showed the USB analog PulseAudio source at `analog-input-mic`; no
capture stall or new pstore file appeared in the first minutes. A quiet-start
`capture_level_low` warning does not measure same-phrase speech. The first
150 s yielded 5 `resource_sample` rows among 22 KPI rows, but the local-only
routing run has no network probes, so that ratio cannot isolate P7's network
persist-rate effect. **The soak is running; survival is not yet verified.**
At its first 300-second checkpoint the supervisor still reported `running`;
Helios PID 31472 was alive, the boot ID matched, and pstore remained empty.
The user then requested an immediate stop before powering off the device.
After verifying PID 31472's command and working directory, SIGINT stopped
Helios cleanly with exit code 0. The supervisor exited too. Its raw status
is `failed_helios_exited` because it does not distinguish an operator stop;
the observed cause was the user's stop request. Elapsed soak time was
585.57 seconds, far below the required 30,300 seconds, so this run proves
nothing about long-run USB stability. Boot ID remained unchanged and pstore
was empty after shutdown. No power-off command was issued by Codex.

- Autosuspend: back up `/boot/extlinux/extlinux.conf`, add
  `usbcore.autosuspend=-1` to its `APPEND` line, reboot. Rollback: restore the
  backup and reboot. Verify `cat /proc/cmdline` and
  `cat /sys/module/usbcore/parameters/autosuspend`. This is unlikely to fix
  URBs stuck while active; a bad boot edit can prevent startup.
- Powered hub: the mic is at Bus 01 Port 2.1 and WiFi at Bus 01 Port 2.4
  under the same hub. Move the mic to a powered hub on a different physical
  Jetson USB port only after the operator identifies that port and confirms
  Ethernet/local-console recovery. Rollback: original port. Verify `lsusb -t`,
  `pactl list short sources`, and SSH connectivity. Bus numbering alone
  cannot identify the exact physical receptacle; a bad move can strand SSH.
- Power mode: `sudo nvpmodel -m 1`; rollback `sudo nvpmodel -m 0`; verify
  `nvpmodel -q`. Current mode is MAXN. Lower current may aid the hub but lower
  CPU clocks worsen P6's CPU-bound local inference; the operator must weigh
  both effects.
- Watchdog: `sudo sysctl -w kernel.watchdog_thresh=20`; rollback to `10`;
  verify `sysctl kernel.watchdog_thresh`. This hides the symptom and extends
  the unresponsive window; it cannot fix USB or storage.
