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
  --source '<exact-pulse-source>' \
  --model /absolute/path/to/vosk-model-small-it-0.22 \
  --rate 16000 --channel-mode mono \
  --candidate-output "/private/audio-candidate.json" \
  --profile-id local-candidate
```

The parent directory must already exist. Creation records current gains without
changing them. A changed gain or route requires a new private candidate.

Select the file explicitly for a diagnostic check:

```sh
HELIOS_AUDIO_CALIBRATION_CONFIG="/private/audio-candidate.json" \
  python scripts/audio_calibration.py \
  --source '<exact-pulse-source>' \
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
