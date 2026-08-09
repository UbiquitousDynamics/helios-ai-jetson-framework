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

Remote continuity is disabled by default. With
`HELIOS_LLM_ALLOW_REMOTE_CONTEXT=false`, every Codex request follows the prior
path exactly: start a fresh ephemeral thread and run one turn on it.

When the flag is true:

1. The first successfully completed Codex turn starts an ephemeral thread and
   saves its returned id in memory.
2. The next Codex request resumes that id before starting its turn. Context
   calls are serialized because one thread cannot safely accept competing
   active turns.
3. Only successfully completed Codex turns count toward the cap. A local Ollama
   turn never enters the adapter, never increments the counter, and is never
   injected or backfilled into the remote thread.
4. Before a new Codex attempt, Helios drops an idle thread at the configured
   timeout or drops a thread whose completed-turn cap has been reached. The next
   attempt starts a fresh ephemeral thread.
5. A failed, cancelled, partially consumed, or interrupted Codex attempt drops
   the saved id. This conservative reset prevents a later answer from resuming
   history whose last turn has uncertain completion state.

The two lifecycle bounds are:

| Environment variable | Default | Effect |
|---|---:|---|
| `HELIOS_LLM_CONTEXT_IDLE_TIMEOUT_SECONDS` | `900` | Start a fresh thread after this much inactivity |
| `HELIOS_LLM_CONTEXT_MAX_TURNS` | `20` | Start a fresh thread before the turn after this many completed Codex turns |

Both values must be positive; invalid environment overrides trigger the
existing fail-closed hybrid-routing behavior.

## Prompt, model, cancellation, and fallback behavior

`VoiceAssistant` continues to pass `context=None`. The remote conversation is
already represented by the resumed Codex thread; also sending a textual history
would duplicate it. No RAG, transcript privacy, model-tier selection, catalog,
or budget behavior is changed.

Adaptive model selection remains per turn. On a resumed thread, Helios forwards
the selected model to both resume and turn start. App-server warning and
developer-notification events are not visible answer deltas, so a model-switch
warning is ignored while the actual agent-message stream is preserved. The
official SDK's turn handle stores `thread_id` and the active turn id;
`APIClient.cancel_current()` reaches its `interrupt()` method, which issues the
required two-id `turn/interrupt` request.

Ollama fallback remains independent. There is deliberately no
`thread/inject_items` path: a locally answered turn is neither disclosed to
Codex nor represented as if Codex answered it. If a Codex attempt itself fails
before Ollama fallback, the uncertain remote thread is discarded; the local
answer still is not backfilled, and the next eligible Codex request starts a
new thread.

Automated coverage uses fake runtimes and routing targets. It verifies disabled
fresh-thread behavior, start-then-resume continuity, idle and cap boundaries,
failure/interruption reset, model switching without warning leakage, exact SDK
arguments, configuration wiring, persistent runtime reuse, and a local Ollama
route producing zero Codex start/resume/inject calls.
