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

## Follow-up channel diagnostics at 50 cm

Two further acquisitions kept playback gain 0.4 and temporary mic boost +12 dB.
Each opened the explicit Pulse source as stereo PCM16 at 16 kHz, with 1600-frame
reads. Left, right and arithmetic-average signals were submitted to separate
Vosk recognizers from the same acquisition; these are not independent trials.
Only aggregate levels and edit counts were retained. Evidence is in
`mcp/debian-channel-diagnostics-2026-10-07.json`.

Each acquisition produced three nonempty final segments per channel, with exactly
one matching the reference on each channel. Other segments still had word errors.
Quiet median RMS for the averaged channel was 0.005714 and 0.007793. Median
uncentered channel correlation in the playback wall-clock window was 0.973246
and 0.990657. The phase medians for rail fraction were zero; this does not assert
that every individual sample was unclipped. These observations do not suggest
destructive stereo cancellation or establish a consistently superior channel.

The opening transient observed in earlier pilots remains unresolved; these
opening-phase median summaries cannot rule out brief transients. Processing
three recognizers also changes diagnostic workload. Phase windows use local
processing time rather than synchronized acoustic timestamps, so their levels
are descriptive and must not be used for SNR, latency or production performance
claims. Final segments are not explicitly matched to playback trial boundaries.

No calibrated profile or thresholds were installed. Original +36 dB boost was
restored and both services restarted successfully. Recognition remains too
inconsistent to validate voice confirmation; further input-path and stimulus
diagnostics are needed before deployment-specific calibration.

## USB input follow-up

After reconnecting, Debian enumerated USB PnP Sound Device (`8086:0808`), using
`snd_usb_audio`, as the mono Pulse source
`alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.mono-fallback`.
The tests selected that source explicitly and left persistent Helios input
configuration unchanged. The user subsequently confirmed a complete USB
microphone; enumeration alone does not verify capsule function.

Three pilots used the same generated phrase and Windows playback gain 0.4, with
the previous 50 cm bench distance as the comparison setup. The new capsule's
exact position relative to the speaker has not been independently confirmed.

| USB pilot | Hardware Mic level | Auto Gain Control | Quiet median RMS | Nonempty recognized finals |
| --- | --- | --- | --- | --- |
| 001 | 16/16 (+23.81 dB) | On | 0.164349 | 0 |
| 002 | 16/16 | Off temporarily | 0.139994 | 0 |
| 003 | 8/16 | Off temporarily | 0.039199 | 0 |

A separate one-second explicit-source `parec` check measured RMS 0.146015,
DC mean -0.000166, peak 0.281464 and zero rail samples. Ten PortAudio buffers
also showed high RMS without significant DC offset or rail samples. These
observations do not identify a root cause; they do not establish a healthy
physical microphone or justify installing a calibration. The inherited
`startup_transient_is_unresolved` flag tracks the unresolved earlier investigation,
not proof that the USB device reproduced the integrated microphone's transient.

Raw audio and microphone transcripts were not retained. Aggregate pilot evidence
is in `mcp/debian-usb-diagnostics-2026-10-07.json`. Hardware Mic level 16/16 and
Auto Gain Control on were restored, both services were active, and the production
input selector still points to the integrated microphone. No thresholds, local
calibration profile or project defaults were changed. Verify the physical USB
microphone/adapter connection and positioning before further gain trials.

The user then indicated readiness after being asked to move the USB microphone
20–30 cm from the Windows speaker and check mute. Exact distance and model were
not supplied, so pilot 004 records the requested range rather than an invented
measurement. At original USB settings, quiet median RMS was 0.057678 and overall
maximum RMS 0.062531; again no nonempty final was recognized.

Two subsequent two-second quiet captures with explicit-source `parec`, one at
16 kHz and one at 48 kHz, both had a dominant 50 Hz spectral bin. Approximately
84.9% of Hann-windowed FFT power was below 100 Hz, and the 50 Hz bin alone
accounted for about 56.6%. RMS was 0.057384 and 0.056885, respectively. Only
aggregate spectrum measurements were retained. This is compatible with mains
hum, but does not identify electrical versus acoustic coupling or prove it is
the sole recognition problem. The next controlled comparison is Debian on
battery with the same microphone setup, if battery operation is available.
No audio filter or production configuration change has been applied.
