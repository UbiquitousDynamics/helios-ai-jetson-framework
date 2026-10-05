# Voice action boundary

The optional `VoiceActionController` has explicit clarification, confirmation,
dispatch and outcome states. `VoiceAssistant` accepts an injected controller only
when automation configuration is enabled. It routes authoritative primary-listener
utterances through the existing wake/follow-up rules; normal requests, presentation,
think mode and RAG retain their existing path. Automation is explicitly selected with
`domotica ...` (Italian) or `home control ...` (English).

The first release accepts one action at a time. A trusted local renderer must state the
exact device/change before consent; provider narrative is never used as authorization.
The subsequent microphone utterance must be final, from a newer capture, begin after
prompt playback, match `confermo`/`confirm` exactly and match its recognition text.
An injected calibrated verifier must additionally reject echo/uncertain speech.
No numeric confidence threshold or verifier is bundled: existing acoustic validation
issues #12/#18 must supply device-specific evidence. Without it, voice writes are denied.
Partial, stale, ambiguous, negative, expired and cross-session consent is rejected.

Local stop/mute/session controls invalidate pending actions. Confirmation speech runs
through the existing observed-speech and concurrent barge-in engine. Its cancellation
token reaches the executor before dispatch; interruption cannot undo a possibly completed
action. The existing barge-in boundary returns text without calibrated capture metadata,
so such a follow-up cancels rather than confirms. Consent must arrive from a fresh primary
microphone capture after playback. This path is model-free tested; actual acoustic behavior
remains a rollout gate. No deployment or voice-write activation is performed here.

Outcome speech is fixed locally from the executor's receipt. Success means the service
reported completion; unknown stays unknown and is never replayed. Speech failure after
execution clears pending consent and leaves the durable outcome unchanged.

The present Debian integrated microphone is unresolved. Synthetic fake-verifier tests
are software evidence only and cannot authorize production voice writes.
