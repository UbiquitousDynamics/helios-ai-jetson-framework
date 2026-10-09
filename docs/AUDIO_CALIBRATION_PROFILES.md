# Local audio calibration candidates

Issue #32 requires independent calibration for each deployment. This component
adds explicit **diagnostic candidate selection**, not acoustic approval, automatic
gain adjustment or a production confirmation verifier. Settings and the standard
assistant now pass an explicitly selected `HELIOS_AUDIO_CALIBRATION_CONFIG` to the
recognizer. Without selection the existing runtime is unchanged. A selected
candidate checks identity and rejects mismatches; it does not authorize actions
or apply gains or thresholds. No shared defaults change.

The diagnostic command reads the exact Pulse source, active port, ALSA hardware
identity, capture/boost gains and all mixer controls (including AGC and playback
volume). It binds those to a hashed machine identity and all files in the selected
Vosk model directory. Volatile ALSA card indices are used only to query current
controls and are excluded from the stable identity. Missing, muted, monitor or
ambiguous sources and unsupported/asymmetric capture controls fail closed.

`--rate` and `--channel-mode` describe the intended capture settings. This command
does not open an audio stream and cannot prove the running recognizer uses those
settings. The runtime additionally checks its opened Pulse source-output by PID,
requiring one unambiguous capture stream routed to the exact source, PCM16 at the
configured rate/channel count, unmuted/uncorked and unity stream gain. Strict
explicit Pulse input selection is mandatory. Identity and route are checked after
opening and before each final result, including decoder flush, before observers
receive that final. A mismatch closes the capture and raises a speech error.
Acoustic approval still requires playback provenance/echo and startup validation.

On Debian, create a private candidate without overwriting an existing file:

```sh
python scripts/audio_calibration.py \
  --source alsa_input.pci-0000_00_1b.0.analog-stereo \
  --model /absolute/path/to/vosk-model-small-it-0.22 \
  --rate 16000 --channel-mode mono \
  --candidate-output "$HOME/.config/helios/debian-audio-candidate.json" \
  --profile-id debian-integrated-candidate
```

The parent directory must already exist. Creation reads current gains; it does
not apply the temporary +12 dB pilot setting. The tested Debian baseline candidate
therefore records +30 dB capture and +36 dB boost, not an acoustically approved
setting. A candidate based on +12 dB must be measured at that actual setting and
will cease matching when the gain is restored. Never relabel pilot data as verified.

Select the file explicitly for a diagnostic check:

```sh
HELIOS_AUDIO_CALIBRATION_CONFIG="$HOME/.config/helios/debian-audio-candidate.json" \
  python scripts/audio_calibration.py \
  --source alsa_input.pci-0000_00_1b.0.analog-stereo \
  --model /absolute/path/to/vosk-model-small-it-0.22 \
  --rate 16000 --channel-mode mono
```

A match prints `candidate_identity_matches_not_calibrated`; a mismatch or unreadable
selected file exits 1 without falling back. Without selection, the command does
not query hardware. Files are created exclusively with private POSIX permissions.
No audio, transcripts or credentials are stored; the local candidate contains
hardware metadata and should remain on its deployment.

On Jetson, obtain its own explicit Pulse source and model path and create a
separate candidate. Do not copy the Debian file. The current metadata adapter
requires Pulse/ALSA and recognizable capture controls with dB metadata; direct
ALSA-only or unsupported Jetson devices are blocked pending a tested adapter.
Jetson compatibility and acoustic approval have not been verified.

The injectable `DeviceBoundCalibration.require_match()` rechecks identity on each
call. Consumers must call it at the point of use with a live identity provider;
the helper does not run continuously. The standard recognizer now calls the
binding at capture opening and final-result delivery when explicitly configured.
Its `calibration_id` is always empty and `accepts()` always false. Even if injected
into `VoiceActionController`, a candidate cannot arm a pending action. The current
schema rejects `verified` status and arbitrary thresholds altogether.

On 2026-10-07 an isolated copy on Debian created a candidate, matched real current
metadata, rejected a different requested rate, and verified mode 0600. Hardware
settings and the running deployment were unchanged. Remaining #32 work includes
startup-transient diagnosis, confirmed echo/interruption
guards, acoustic limits, authorized low-risk writes and controlled reboot evidence.

## Runtime verification on Debian

At commit `44ab27b`, the Windows suite passed 2309 tests (4 skipped); the Debian
QA checkout passed 2310 (3 skipped). Ruff check and format passed on both.
An actual integrated-microphone pilot selected a locally created candidate at
temporary +12 dB boost with the new capture identity and Pulse source-output
guards enabled. All three nonempty finals passed those guards, but none matched
the reference. One long segment included the pre-playback interval, and final
segments were not aligned one-to-one to stimulus repetitions. Thus this validates
operation of the binding checks, not acoustic quality or confirmation safety.

After restoring +36 dB boost, a second actual recognizer rejected the +12 dB
candidate before delivering recognition. This verifies live gain-mismatch denial,
not just a fixture or a CLI comparison. Both services were active after cleanup;
the production service was not configured to select a calibration file and no
gain/threshold default changed. Evidence is in
`mcp/debian-runtime-profile-2026-10-07.json`. The earlier six exact close-range
state segments remain historical evidence, not a guarantee of repeatability.
Startup stability, room/background variation and runtime echo/interruption
validation still block acoustic approval.
