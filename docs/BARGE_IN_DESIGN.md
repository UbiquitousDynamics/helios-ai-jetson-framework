# Barge-in detection and echo suppression

## Detection strategy

Helios uses a model-free detector with two inputs. Raw microphone frames are
classified by normalized RMS energy, and a signal must remain above the
configured threshold for a minimum duration before it becomes a candidate.
An integration that already has Vosk results may instead submit a non-empty
partial or final `RecognitionResult`; recognized speech is a stronger signal
than energy alone and becomes a candidate immediately. Because the existing
`listen_events()` boundary does not attach PCM energy, the event path uses a
configurable nominal energy when none is supplied. It still consults the echo
policy on every non-empty event. Integrations that retain the corresponding PCM
frame should pass its measured energy explicitly for a better-informed veto.
The detector is one-shot during each playback interval and must be rearmed with
`reset()`.

The defaults match the existing recognizer's 16 kHz, signed 16-bit mono PCM.
With its current 4,000-sample reads, the default 120 ms duration is satisfied
when one 250 ms frame arrives. Smaller future capture frames accumulate until
they cover the same duration. This bounds the detector-side response without
adding a neural model, inference runtime, network access, or native package.

Vosk partial events alone are useful but not sufficient for every integration:
they arrive only after the recognizer has decoded speech, and speaker leakage
can itself decode as text. The PCM path gives predictable early triggering;
the event path remains available when an integration already runs Vosk. Its
nominal energy is necessarily a coarse assumption: setting it too high can
admit decoded playback echo, while setting it too low can suppress quiet real
interruptions. A dedicated VAD such as Silero was rejected for the first
implementation because the synthetic-energy approach is deterministic and adds
no model asset or runtime cost. It can be reconsidered after hardware
measurements show that RMS cannot adequately distinguish environmental noise
from speech.

## Self-hearing mitigation

`BargeInDetector` accepts an `EchoSuppressionPolicy` and defaults to
`NoEchoSuppressionPolicy`. That default is intentional: detection can be used
and tested independently, and deployments must explicitly opt into assumptions
about their microphone and speaker placement.

The provided `ConservativeEchoSuppressionPolicy` is a pure, configurable soft
gate. It suppresses candidates at or below the larger of:

- an absolute minimum interruption energy; and
- the measured speaker-leakage energy multiplied by an echo margin.

For a short window after TTS begins it raises that boundary again to tolerate
playback startup transients. It accepts normalized frame energy and elapsed
time since playback started and performs no audio I/O. The expected echo level
should be calibrated on the target enclosure by playing representative Piper
output and measuring microphone RMS with nobody speaking. The shipped values
favor avoiding self-interruption, accepting that quiet users may occasionally
need to repeat an interruption.

Full acoustic echo cancellation using `speexdsp` or
`webrtc-audio-processing` was not selected. Those options add native build and
Jetson packaging risk, require a time-aligned playback reference, and depend
strongly on device latency and acoustic geometry. A single microphone and
speaker are not guaranteed to expose the stable timing needed for reliable
software AEC. Hardware AEC remains preferable when a deployed audio device
provides it; a software AEC stage can later feed cleaner PCM into the same
detector interface without changing the policy contract.

## Integration contract

During each TTS playback interval, reset one detector and pass it microphone
frames or Vosk events together with elapsed playback time. A `true` result is a
single edge-triggered interruption request; the integration is responsible for
stopping playback and cancelling generation. All thresholds and timing values
are constructor arguments so hardware tuning does not require detector changes.

`VoiceAssistant` implements that contract only when
`HELIOS_BARGE_IN_ENABLED=true`. It submits the response path to its existing
injectable executor and consumes bounded 250 ms `listen_events()` slices on the
coordinating thread while `PiperTTS.is_speaking` is true. A detection calls both
`PiperTTS.interrupt()` and `APIClient.cancel_current()`, waits for the cancelled
worker to unwind, then treats the finalized utterance as an authorized immediate
follow-up. The follow-up does not require the wake word, and its response is
monitored in the same way so more than one consecutive interruption is possible.
Returning to the idle listening loop restores the normal wake-word requirement.

Piper playback is split into 100 ms writes. This lets an interrupt be observed
between writes while retaining and reusing the same `sounddevice` stream. The
200-300 ms stop target is therefore plausible from the playback side but remains
a target-device measurement, not a scheduling guarantee.

## Streaming cancellation and budget settlement

The existing streaming contract required no runtime change. `APIClient` tracks
active event-backed cancellation tokens under a lock, so another thread can
safely cancel an in-flight request. Cancellation is terminal: the coordinator
does not retry the same target or fall back to another one, which prevents queued
or replayed speech after a barge-in. If a transmitted cancelled attempt has no
provider usage report, the budget ledger conservatively settles the full
reservation and clears the outstanding amount. Characterization tests cover
all three behaviors.

## Deployment status

Barge-in defaults off. Before enabling it on a vehicle, calibrate expected echo
energy with the production enclosure, microphone, speaker level, and Piper voice,
then measure interruption latency and self-trigger rate. The Vosk-event path uses
a nominal energy because the current recognizer event does not carry PCM RMS;
deployments needing stronger echo discrimination should feed synchronized PCM
energy into the detector or add hardware/software AEC ahead of it.
