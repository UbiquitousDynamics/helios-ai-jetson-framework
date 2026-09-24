# Codex remote conversation continuity

## Process-lifecycle prerequisite

The Step 0 audit found no process-lifetime blocker. Helios registers one lazy
`CodexAppServerAdapter` instance, the adapter creates and caches one
`_OfficialCodexRuntime`, and that runtime owns one official `Codex` client until
provider shutdown. The client starts one app-server connection/process during
construction. Multiple voice turns therefore reach the same running app-server;
previously, only the Codex thread was recreated for each request.

This matters because Helios deliberately uses `ephemeral: true`. An ephemeral
thread is held in that app-server process instead of being persisted as a
rollout on disk. Its id can be resumed during the current Helios run, but not
after process shutdown or restart. Disk-persisted threads, retention policy, and
restart recovery remain out of scope. The protocol behavior is documented in
the official [Codex app-server guide](https://learn.chatgpt.com/docs/app-server),
and the pinned Python SDK exposes `thread_start`, `thread_resume`, per-turn model
overrides, and a turn handle whose interrupt call carries both thread and turn
ids.

## Opt-in lifecycle

Helios now owns one provider-neutral, in-memory logical conversation. Finalized
user turns enter its canonical history exactly once; assistant text enters only
after a successful terminal completion. Interrupted or failed partial assistant
text is not represented as a completed answer. Ollama receives this bounded
history on every request.

Remote conversation continuity is enabled by the application and bundled Codex
profile defaults. With
`HELIOS_LLM_ALLOW_REMOTE_CONTEXT=false`, prior turns are not authorized for
remote egress. Codex therefore uses a fresh physical thread for an eligible
context-free request, while a request containing history is rejected by the
privacy guard and can fall back locally. The committed Codex subscription
example enables the same behavior without requiring an environment override.
Deployments that require fresh remote threads can explicitly opt out with
`HELIOS_LLM_ALLOW_REMOTE_CONTEXT=false`.

When the flag is true:

1. The first Codex turn starts an ephemeral physical thread and captures its id
   as soon as the SDK exposes it.
2. A contiguous healthy Codex turn resumes that id and sends only the current
   unsynchronized user turn, avoiding duplicated history.
3. A local-provider turn advances the logical session without mutating Codex.
   On the next Codex request, the adapter detects the checkpoint gap, starts a
   replacement thread, and rehydrates it from canonical history.
4. An idle or capped physical thread is rotated the same way; the logical
   session is not erased merely because a physical provider checkpoint rotates.
5. A failed, cancelled, partially consumed, or interrupted physical turn is
   marked unusable. The next eligible Codex turn performs an explicit recovery
   start with canonical history instead of silently starting an empty chat.
6. Context state is keyed by logical session id, so unrelated sessions never
   resume one another's physical thread.
7. The physical thread id is mirrored into the provider-neutral session as soon
   as it is available. A logical commit handshake invalidates a provider
   checkpoint if cancellation or TTS failure wins after provider EOF.
8. Provider state is removed on explicit logical reset and LRU-bounded for
   unrelated callers. Retiring an app-server runtime invalidates every physical
   checkpoint owned by that process.

The two lifecycle bounds are:

| Environment variable | Default | Effect |
|---|---:|---|
| `HELIOS_LLM_CONTEXT_IDLE_TIMEOUT_SECONDS` | `900` | Expire an idle logical voice session and rotate idle physical context |
| `HELIOS_LLM_CONTEXT_MAX_TURNS` | `20` | Bound canonical request history and rotate the physical thread at the cap |

Both values must be positive; invalid environment overrides trigger the
existing fail-closed hybrid-routing behavior.

## Prompt, model, cancellation, and fallback behavior

`VoiceAssistant` does not repurpose the legacy one-off `context` system string.
`APIClient` builds typed user/assistant history with
`ContentOrigin.CONVERSATION_HISTORY`. A healthy resumed thread receives only
the current raw transcript; a new or recovered thread receives the bounded
canonical history. RAG remains separate, and the existing transcript/context
privacy gates still authorize remote egress independently.

Canonical history retains transitive provenance and a sticky remote-egress
classification. Content first handled as `local_only`, derived from a local
document, or supplied as unredacted data under `remote_redacted` cannot be
reclassified merely because a later request uses `remote_allowed`. Model output
is never marked redacted without a separate attestation. The privacy guard
therefore keeps the whole continuity-preserving request local when any retained
message is not eligible for remote transmission.

Adaptive model selection remains per turn. On a resumed thread, Helios forwards
the selected model to both resume and turn start. App-server warning and
developer-notification events are not visible answer deltas, so a model-switch
warning is ignored while the actual agent-message stream is preserved. The
official SDK's turn handle stores `thread_id` and the active turn id;
`APIClient.cancel_current()` reaches its `interrupt()` method, which issues the
required two-id `turn/interrupt` request.

Codex interrupt, runtime close, Ollama stream close, and Ollama client retirement
run behind bounded control workers. A hung transport cannot hold the session
lock forever; owned clients/runtimes are retired and recreated for the next
turn. Ollama also enforces connect, first-token, read, and total deadlines while
waiting on its SDK iterator.

There is deliberately no `thread/inject_items` side channel. Ollama fallback
commits to Helios's canonical history. If a later Codex turn is privacy-
authorized, it receives that history once through an explicit recovery start.
This makes disclosure visible to the normal privacy guard and avoids pretending
that Codex generated a local answer.

Automated coverage uses fake runtimes and routing targets. It verifies disabled
fresh-thread behavior, healthy start/resume deltas, interruption recovery with
rehydration, idle and cap rotation, model switching, session isolation,
provider-gap recovery, persistent runtime reuse, and exact local/remote history
across Codex-to-Ollama and Ollama-to-Codex route changes.
