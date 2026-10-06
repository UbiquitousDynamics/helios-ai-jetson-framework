# Content-free automation audit v1

Events use the existing `SafeMetricsRecorder` and storage/KPI pipeline (#16), without
a second writer, background loop or dashboard. The prefix `automation_v1_` versions the
closed schema. Phases: proposal, policy, confirmation, dispatch, outcome, reconciliation,
replay_suppressed and capability. Outcomes are closed enums distinguishing cancellation,
unknown delivery, transport failure, auth denial and unsupported capability.

To preserve the public metrics contract, correlation uses existing fields:

| Audit identifier | Metric field | Encoding |
| --- | --- | --- |
| Session | resource_scope | SHA-256 |
| Turn | attempt_id | SHA-256 |
| Action | request_id | SHA-256 |
| Provider/server | provider | SHA-256 |

There is no slot for transcript, audio, tool name, device label/state, arguments,
results, URLs, credentials or exception text. Fixed event/outcome codes and a bounded
nonnegative phase latency are recorded. Dispatch/outcome latency measures the call
phase; proposal/confirmation events retain their own timestamps. This is observability,
not an unmeasured performance claim.

Hashes remain linkable and are not anonymization. Use locally generated opaque IDs,
protect metric storage and use the existing configured bounded recorder retention,
queue capacity and storage rotation. No audit events are emitted per audio frame.
Recorder errors cannot change dispatch behavior.

Audit is separate from the durable dispatch ledger. Missing/dropped audit events do
not authorize replay. A missing durable receipt remains unknown, even if a tool returned
success; the emitted outcome reflects that uncertainty. Replay suppression is explicit.
Reconciliation events are reserved for a future read-only reconciler; none performs writes.
