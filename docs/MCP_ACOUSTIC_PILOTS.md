# Debian acoustic pilots, 2026-10-07

Issue #32 remains open. Six component pilots did not establish calibrated
recognition or confirmation. See `mcp/debian-acoustic-pilots-2026-10-07.json`.
These are diagnostic measurements, not acceptance-suite samples or latency results.

Calibration is deployment-specific, never a shared default. These measurements
apply only to this Debian microphone route and the recorded Windows playback
setup, including distance, gain, channel mode and model versions. They must not
set thresholds for other PCs, Jetson targets or microphones. Store any eventual
validated calibration in an explicitly selected local device profile. A changed
microphone, route or relevant audio/model setting requires new validation; a
missing or mismatched profile must not authorize voice writes. This is a required
deployment contract, not a claim that runtime identity checks are implemented.

The user confirmed a quiet room, unobstructed devices, monitoring disabled and
98 cm separation. Windows played generated Italian Piper speech through its
Realtek speaker; the user confirmed hearing the three repetitions clearly.
Debian captured its explicitly selected integrated microphone. Helios was stopped
during each acquisition and automatically restarted afterward. No user audio or
observed microphone transcripts were retained; no physical actions were executed.

The original synthetic WAV was recognized exactly by Vosk small Italian 0.22
when supplied directly after resampling to 16 kHz. This isolates a failing acoustic
path, but does not identify the defective hardware or a single root cause.

| Pilot | Temporary mic boost | Playback gain | Quiet RMS median | Result |
| --- | --- | --- | --- | --- |
| 001 | +36 dB | No playback | 0.999914 | Aborted at opening frame |
| 002 | +36 dB | 0.2 | 0.134175 | Aborted above RMS 0.4 |
| 003 | +24 dB | 0.2 | 0.028789 | No exact recognized segment |
| 004 | +12 dB | 0.4 | 0.007486 | No exact recognized segment |
| 005 | +12 dB | 0.4 | 0.014695 | Stronger stereo channel; no exact segment |
| 006 | +12 dB | 0.4 | 0.006789 | At 50 cm, stronger channel; no exact segment |

For pilot 006 the user moved the Windows speaker to 50 cm. Playback and capture
parameters matched pilot 005. Three recognized final segments had respectively
3, 2 and 3 word edits against the five-word reference; none matched exactly.
This is consistent with distance contributing to recognition failure, but a
single repeat with a different measured quiet level does not isolate distance
as the only cause or establish reliable recognition. The microphone boost was
again restored to +36 dB and both services were active after the trial.

The playback gain is a multiplier on generated PCM; Windows master volume was
1.0 and unmuted. Pilot 001 aborted before playback. Later pilots explicitly
measured a three-second opening phase before the quiet baseline and playback.
A separate opening-capture inspection found a large positive DC transient and
99.94% rail samples in the first 100 ms, decaying over subsequent buffers.
The startup transient remains unresolved; excluding its opening phase from the
quiet baseline does not make startup pass.

Recognized final segments were not reliably aligned with the three utterances.
Their edit counts and confidence values must not be presented as per-utterance
word error rates, calibrated confidence or response latency. Lower boost reduced
the observed quiet level in these pilots, but did not produce correct recognition.
Selecting the stronger stereo channel also failed to establish correct recognition.
No recognition threshold was relaxed and no gain/channel change was persisted.

After the pilots, Internal Mic Boost was restored to +36 dB. Both user services
were active; Helios emitted `voice_ready`, resolved the intended mono 16 kHz input,
and had no startup errors or fallback events. This is runtime recovery evidence,
not evidence that microphone recognition works correctly.

Next, diagnose channel integrity, DC/noise and the physical input path before
calibration and the confirmation/negative/echo/interruption suite. Controlled
reboot with the new configuration and designated test writes remain unverified.
Voice writes remain disabled; current Home Assistant credentials are read-only.
