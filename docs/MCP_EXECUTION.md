# Dispatch and replay suppression

`ActionExecutor` re-discovers the catalog, validates local policy and exact confirmation,
checks cancellation and expiry, and commits a SQLite reservation before calling the
tool. No await separates the durable reservation from entering the call. The reservation
is the conservative dispatch boundary: a crash before the actual network send is also
unknown. This intentionally sacrifices automatic recovery rather than risking a second write.

A duplicate action ID with the same fingerprint returns the persisted outcome; changed
arguments or identities under the same action ID are denied. A lost response, timeout,
disconnect, cancellation after reservation or failed receipt persistence stays unknown.
Cancellation propagates; callers can look up the receipt without dispatch. No fallback,
speech failure or reconnect may generate another ID for the same accepted action.
No reconciliation writes, idempotency keys or exactly-once physical guarantee are claimed.
Success means the tool reported success, not independently verified physical state.

The ledger uses WAL and synchronous FULL commits. It stores hashed action/session/turn
IDs, proposal fingerprints, finite timestamps and fixed outcome codes, never arguments,
results or transcripts. Hashes remain correlatable and are not anonymization. Deploy the
database under a private operator-owned directory and restrict directory access.

Defaults retain tombstones for one day (maximum seven days) with 10,000 records.
Capacity exhaustion denies dispatch rather than dropping live records. Expired records
are pruned only beyond retention. IDs must be locally generated and never reused;
duplicate suppression is bounded by retention. Per-turn limits are checked inside the
same transaction. Proposal lifetime is capped by configuration (at most 300 seconds).
Clock rollback denies new reservations; a session controller must invalidate pending consent.

Tests simulate lost acknowledgment after execution, restart, a durable pending crash,
conflicting IDs, cancellation, stale catalogs, missing confirmation and capacity limits.
These are model-free tests and do not certify any device or microphone.
