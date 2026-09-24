# Barge-in detection and echo suppression

## Detection strategy

Helios uses a model-free detector with two inputs. Raw microphone frames are
classified by normalized RMS energy, and a signal must remain above the
configured threshold for a minimum duration before it becomes a candidate.
The production Vosk path emits each non-empty partial or final
`RecognitionResult` with PCM energy, a stable segment id, segment onset/peak,
and optional per-word confidence/timing metadata. A non-echo partial can only
arm a provisional candidate. Irreversible model cancellation requires a
textually consistent final from the same segment, sufficient duration and,
when Vosk supplies it, acceptable confidence. Energy-only re-emission and
unstable hypothesis revisions cannot confirm a turn. Legacy injected
recognizers without metadata retain their compatibility path. The detector is
one-shot during each response interval and must be rearmed with `reset()`.

The defaults match the existing recognizer's 16 kHz, signed 16-bit mono PCM.
With its current 4,000-sample reads, the default 120 ms duration is satisfied
when one 250 ms frame arrives. Smaller future capture frames accumulate until
they cover the same duration. This bounds the detector-side response without
adding a neural model, inference runtime, network access, or native package.

Vosk partial events alone are useful but not sufficient for every integration:
they arrive only after the recognizer has decoded speech, and speaker leakage
can itself decode as text. Attaching PCM RMS to the decoded event lets one
continuous Vosk session provide both signals without opening a second capture
stream. A dedicated VAD such as Silero was rejected for the first implementation
because the synthetic-energy approach is deterministic and adds no model asset
or runtime cost. It can be reconsidered after hardware measurements show that
RMS cannot adequately distinguish environmental noise from speech.

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

`VoiceAssistant` implements that contract by default; set
`HELIOS_BARGE_IN_ENABLED=false` to disable it. It submits the response path to its existing
injectable conversation executor and opens one continuous `listen_events()`
microphone/Vosk session while the response is being generated and spoken. The
capture stays open during model thinking. Partial recognition remains armed
only during actual playback and a 250 ms echo tail, while a finalized utterance
received during thinking or synthesis explicitly supersedes the pending answer.
This preserves Vosk decoder state
across the whole overlap instead of repeatedly reopening 250 ms capture slices.
A stable partial reversibly pauses Piper at its current PCM offset, while the
model and canonical turn keep running. This removes loudspeaker interference
while Vosk finalizes the user's utterance; a rejected candidate resumes at the
same offset. A 1.5-second candidate-inactivity deadline prevents a missing Vosk
final from leaving playback paused. A confirmed final calls both
`PiperTTS.interrupt()` and
`APIClient.cancel_current()`. The same recognizer session then continues until
Vosk emits or flushes the finalized interruption utterance. After the cancelled
response worker unwinds, that finalized utterance becomes an authorized
immediate follow-up. It does not require the wake word, and its response is
monitored in the same way so more than one consecutive interruption is possible.
Interrupted canonical history renders a content-free assistant control marker
between the old user turn and the new one. That preserves useful context while
making the latest user utterance unambiguously active for stateless Ollama and
Codex thread recovery. The active voice session also accepts ordinary finalized
follow-ups without a wake word until its configured idle timeout. A partial is never promoted to a
final command when Vosk's final flush is empty; timeout therefore cancels the
interrupted answer but executes no incomplete follow-up.
The post-detection timeout is an inactivity deadline refreshed by each decoded
event, so a long utterance is not cut off a fixed number of seconds after its
first partial.

Piper playback is split into 100 ms writes. This lets an interrupt be observed
between writes while retaining and reusing the same `sounddevice` stream. The
200-300 ms stop target is therefore plausible from the playback side but remains
a target-device measurement, not a scheduling guarantee. A speech-operation
event is registered before Piper synthesis begins, so cancellation during
synthesis prevents the completed WAV from starting. Closing Piper sets a
terminal state, interrupts and drains synthesis/playback, clears the cue cache,
and only then closes the backend.

Notification sound and RAG preparation retain their original single-worker
serial executor. Conversation responses and backchannels use a separate
two-worker injectable lane, preventing a model worker from starving its cue.
A supplied conversation executor with a known capacity below two workers is
rejected when barge-in is enabled. Assistant-owned tasks are tracked and given a
bounded cancellation deadline at shutdown even when the executor itself belongs
to the caller.

The conservative gate is deployment-tunable without code changes:

| Environment variable | Default | Meaning |
|---|---:|---|
| `HELIOS_BARGE_IN_EVENT_ENERGY` | `0.08` | Fallback energy for legacy recognition events without PCM RMS |
| `HELIOS_BARGE_IN_EXPECTED_ECHO_ENERGY` | `0.04` | Calibrated microphone RMS of ordinary Piper leakage |
| `HELIOS_BARGE_IN_MINIMUM_INTERRUPT_ENERGY` | `0.06` | Absolute floor for admitting an interruption |

## Conversational pacing and time to first audio

`SpeechChunker` already committed streamed output at a natural boundary: `.`,
`!`, `?`, `;`, or `:`, with an 80-character soft whitespace boundary for the
bundled talk profile when punctuation is late. It neither waited for a complete
response nor emitted lone words, so this granularity was preserved.

When barge-in mode is enabled, Helios also schedules one short phrase from the
active language profile after `HELIOS_BACKCHANNEL_DELAY_SECONDS` (default
`0.7`). Italian rotates through `Certo.`, `Un momento.`, and `Vediamo.`;
English rotates through `Sure.`, `One moment.`, and `Let's see.`. Piper
pre-synthesizes these WAVs once after the welcome message, and startup waits for
that preparation before accepting the first command. The latency-sensitive path
only plays a verified in-memory WAV and never synthesizes a cue on demand. The
delay is measured from session construction rather than worker-dispatch time.
If the first real fragment is ready first, the queued cue is cancelled. If a cue
is already playing, a session-owned cancellation event stops and fully joins it
immediately before dispatching real speech, so the two cannot overlap or queue
back-to-back. A legacy TTS backend without scoped cancellation skips cues rather
than risking interruption of unrelated playback. Partial or failed preparation
rotates only through phrases verified as cached; no ready cue degrades to silence
and never blocks a valid answer.

The model-free timed-stream harness used a fake local provider whose first
complete sentence arrived at either 250 ms (representative fast stream) or
1,200 ms (representative slow stream). Times are medians of three wall-clock
runs on the development host and measure dispatch to a fake first audio frame;
they exclude Piper synthesis, device buffering, and Jetson scheduling:

| Scripted case | Baseline first real audio | Post-change perceived first audio | Post-change first real audio |
|---|---:|---:|---:|
| Fast sentence at 250 ms | 251 ms | 251 ms; no cue | 251 ms |
| Slow sentence at 1,200 ms | 1,201 ms | 709 ms cached cue | 1,201 ms |

The change deliberately does not pretend to make the model faster: real-answer
latency is unchanged. It removes roughly 492 ms of perceived silence in the
slow scripted case while leaving the fast case untouched. Production Piper
frame latency and the threshold still require target-device measurement.

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

Barge-in and conversational cues default on together. Before production use on
a vehicle, calibrate expected echo energy with the enclosure,
microphone, speaker level, and Piper voice, then measure interruption latency,
self-trigger rate, cue timing, Piper synthesis time, and actual first audio on
the Jetson. Silence endpointing remains intentionally unchanged and optional;
the existing 6.5-second recognition timeout is backward compatible, while the
full-duplex work removes repeated capture setup and addresses interruption
without making an unvalidated endpointing policy the default. Hardware or
software AEC can later feed cleaner PCM into the same detector interface.
