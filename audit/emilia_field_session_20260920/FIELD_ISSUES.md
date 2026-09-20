# Emilia field session — issues found

Recorded from two live sessions on the Emilia target, 2026-09-19 and 2026-09-20. Every
observation below is reproducible from the evidence quoted. No application source was
modified during this session; the only changes were device-side configuration and
credentials, itemized in the last section.

## Environment

| Item | Value |
| --- | --- |
| Device | `emilia@192.168.1.100`, hostname `ondasolare` |
| Kernel / arch | Linux 4.9.253-tegra, aarch64 |
| Python | 3.10.0, device venv at `/home/emilia/helios-ai-jetson-framework/venv` |
| Deployment path | `/home/emilia/helios-live-conversation-validation-20260919-claude` |
| Source | working tree at commit `b23a531` plus uncommitted Task 01-06 changes |
| Local model | Ollama `emilia-gemma3:1b`, Q4_K_M, 999.89M parameters |
| Remote provider | `openai-codex` via `codex_app_server`, codex-cli 0.144.4 |

Suite status on the target, unchanged before and after the configuration changes:

```
1 failed, 1140 passed, 1 skipped in 27.60s
```

The skip is `tests/test_live_llm.py`, which requires `HELIOS_LLM_LIVE=1`. It is intentional.

---

## Issue 1 — `SpeechPipeline` error state survives into the next turn

**Severity:** blocks the Task 06 gate. **Status:** open, belongs to the task in progress.

`tests/test_api_client.py::test_failure_during_eof_audio_drain_is_not_replayed_and_next_turn_recovers`
fails deterministically — reproduced three times in isolation, 1.35 s each. The test injects a
synthesis failure on the first turn and asserts the next turn recovers. Instead the first
turn's error is re-raised by the second:

```
client.talk("second synthetic request")
  api/streaming.py:611          flush_speech
  audio/speech_pipeline.py:196  flush
  audio/speech_pipeline.py:260  _raise_pending_error
  RuntimeError: synthetic synthesis failure    <- raised by the PREVIOUS turn
```

The pending-error slot is not cleared between turns, so a synthesis failure makes the
following turn fail as well instead of recovering.

**Attribution.** This failure was present at first deployment of the current working tree,
before any configuration change in this session. The same tree at commit `b23a531` without
the Task 05/06 modifications passed 1044 tests clean on 2026-09-19. The regression is in the
in-progress `audio/speech_pipeline.py` rework (228 changed lines against HEAD), not in the
deployment or the device.

**Not reproduced locally:** the Windows workstation has no pytest installed in its Python
3.12. Device evidence only.

---

## Issue 2 — Time-based speech chunking fragments TTS word by word

**Severity:** user-visible; unnatural speech. **Status:** mitigated device-side, root cause
is a configuration/provider mismatch.

Piper synthesized one or two words per fragment, roughly one second apart, through 57
fragments in a single session:

```
10:33:43  text=Fammi sapere
10:33:44  text=se
10:33:45  text=hai altre
10:33:46  text=domande
10:33:47  text=o
10:33:48  text=se
10:33:49  text=vuoi che
```

**Cause.** `examples/llm-routing.codex-subscription.toml` sets, for both speech modes:

```toml
[modes.talk]
speech_chunk_max_chars = 64
speech_chunk_max_delay_seconds = 0.75
```

`SpeechChunker` flushes whatever is buffered once the delay expires
(`api/speech_chunker.py:98-100`). With a fast remote provider a full clause accumulates in
0.75 s. With Ollama on CPU — measured `first_text_ms` of 1873, 3166 and 10655 across three
consecutive turns — only one or two words arrive per window, so that is what gets
synthesized.

The 0.75 s window therefore buys no earlier audio on the local path; it only fragments it.

**Why it appeared only on 2026-09-20.** Runs on 2026-09-19 were started without
`HELIOS_LLM_CONFIG`, leaving both values at the `0.0` default in `config.py:367-369`, which
disables time-based flushing and splits on sentence boundaries. The symptom arrived together
with the routing file, not with the Task 05/06 code.

**Note for whoever tunes this.** The two fields are per-mode, not per-route, so one value
covers both the remote candidate and its local fallback. A setting appropriate for Codex is
actively harmful whenever the fallback serves the turn — which, per Issue 3, is currently
every turn. A per-route override would let both paths be correct at once; today they cannot
be.

**Mitigation applied.** `speech_chunk_max_delay_seconds = 0.0` in the device-local routing
copy, restoring sentence-boundary chunking. `speech_chunk_max_chars` (64 talk / 80 think)
is retained, so long sentences still split — on a character bound, not mid-word. No test
asserts on these fields, and the applied value equals the repository default.

---

## Issue 3 — Codex quota exhausted; fallback to local is permanent for the session

**Severity:** environmental, not a defect. **Status:** no action available in the repository.

Every remote turn failed and fell back:

```
event=turn_completion_failed status=failed category=quota_exhausted retryable=False
Route codex-talk-luna  excluded by provider health (status=quota_blocked)
Route codex-talk-terra excluded by provider health (status=quota_blocked)
Route codex-talk-sol   excluded by provider health (status=quota_blocked)
```

**Credentials were ruled out.** Running Codex directly under Helios's own profile:

```
CODEX_HOME=~/.helios-codex codex login status
  -> Logged in using ChatGPT

CODEX_HOME=~/.helios-codex codex exec 'say only: ok'
  -> ERROR: You've hit your usage limit. ... try again at 2:32 PM.
```

The profile authenticates, opens a session and selects `gpt-5.6-sol` before the server
refuses on usage limit. An authentication problem would surface as an auth error, not as a
quota message carrying a reset time.

**Behavior was correct.** Health tracking marked the routes `quota_blocked` and the
fail-closed fallback served every turn locally. This is the designed outcome; it is recorded
because it explains the latency and fragmentation seen above, not because it needs fixing.

---

## Issue 4 — Helios's durable Codex profile drifts out of date

**Severity:** latent; would have caused an auth failure once the old refresh token expired.

`ensure_persistent_chatgpt_auth()` bootstraps `~/.helios-codex/auth.json` only when the file
is absent (`api/providers/codex_session.py:75-100`). Once present it is never refreshed from
`~/.codex`, so the two diverge as soon as anything else refreshes the rotating token:

| observed at | `~/.codex` | `~/.helios-codex` |
| --- | --- | --- |
| 2026-09-19 19:00 | 2026-09-18T23:04 | 2026-09-01T22:09 |
| 2026-09-20 10:29 | 2026-09-20T08:29 | 2026-09-19T16:59 |

The 2026-09-01 timestamp matches the last recorded working Helios run with remote routing
(device shell history, 22:11 the same evening) — that run is when the profile froze.

Realigning it by hand fixed the drift twice, and it reappeared within a day. Whether the
durable profile should re-sync when the source is newer is a design question for whoever
owns the Codex provider.

**Secondary observation.** The docstring at `api/providers/codex_session.py:64` states the
profile "deliberately contains only `auth.json`". On the device it also holds
`logs_2.sqlite` (8 MB), `memories_1.sqlite`, `state_5.sqlite`, `models_cache.json`,
`skills/` and `shell_snapshots/`. Code and reality disagree; the stated privacy property is
not the one in force.

---

## Issue 5 — Prompt growth exceeds the first-token budget on CPU inference

**Severity:** conversation-ending on the local path. **Status:** open; no owning task exists.

Observed 2026-09-19 with `max_history_turns` at its default of 20. Canonical history is
replayed in full on every turn, so the prompt grows by two messages per turn. On CPU the
prefill time grows with it until it crosses `first_token_seconds`, default `20.0` in
`api/providers/contracts.py:93`:

| Turn | `request_messages` | `stt_finalized` to `assistant_turn_completed` |
| --- | --- | --- |
| 4 | 9 | 7.8 s |
| 8 | ~15 | 12.8 s |
| 9 | 17 | 17.6 s |
| 10 | 19 | 24.0 s |
| 11 | 21 | failed |
| 12 | 22 | failed |
| 13 | 23 | failed |

```
api.api_client.APIClientError: Unable to stream a model response (first_token_timeout)
  api/api_client.py:1599 in _stream_turn
provider=ollama event=stream_worker_stop_unacknowledged stop_reason=timeout
```

Turns 11-13 failed identically. The realtime layer itself stayed correct throughout:
state transitions remained clean and the loop recovered after every failed turn.

**Device conditions during the same window:** `/api/ps` reported `size_vram: 0` — the model
was served from CPU with the accelerator unused; `free -m` showed 219 MB free of 3964;
thermal zone 0 read 57.5 C. The model's `expires_at` was about five minutes out, so pauses
between turns cost a cold reload.

**Related requirement.** FR-38 (Dynamic Context Management) is the only one of the 45
requirements that addresses this: bound prompt growth by summarizing or selectively
retrieving history rather than replaying it. It currently has no task in the numbered
sequence. Reducing `max_history_turns` is a mitigation, not a fix — it forgets rather than
compresses.

**Observed side effect of the routing file.** `llm-routing.codex-subscription.toml` trims
far harder (`event=target_history_trimmed retained_turns=1 omitted_turns=2`). That removes
the timeout but leaves almost no conversational memory.

**Secondary observation.** After a first-token timeout, cancellation closes the response body
mid-stream (`receive_response_body.failed exception=GeneratorExit()`). Cancellation is
cooperative and left no orphaned worker, but the request remains in flight server-side. Worth
a look during the Task 14/15 failure-path audit; not confirmed as a defect.

---

## What worked

Recorded so the issues above are not read as a general verdict. From a 13-turn unscripted
Italian conversation on 2026-09-19:

- **Wake-word gating.** Three pre-activation utterances rejected with
  `voice_command_ignored reason=no_wake_word`; activation then logged `wake_word=True
  fresh_activation=True`.
- **Wake-free follow-ups.** Turns 2-13 all `voice_follow_up_accepted
  wake_word_required=false`; session active about eight minutes without re-activation.
- **Barge-in capture.** One turn taken mid-speech: `stt_finalized source=barge_in words=5
  segment=2`.
- **Echo rejection.** Seven `barge_in_candidate_suppressed reason=short_unconfirmed_final`
  — playback attenuated, generation never cancelled. The conservative path behaved as
  designed.
- **Routing and privacy gates.** Every turn stayed on the intended provider; nothing escaped
  locally while remote was unavailable.
- **State machine.** Clean `listening -> user_turn_finalized -> generating -> listening`
  across all 13 turns, including after the three failures.
- **Task 01-06 tests.** `test_realtime_conversation.py`, `test_transcripts.py`,
  `test_turn_endpoint_detector.py`, `test_conversation_control.py`,
  `test_full_duplex_capture.py` — 449 passed in 4.64 s.

Startup timings, three runs: Ollama warm-up 7.1-29.1 s (cold vs. warm), five Piper voices
8.3-9.8 s, welcome utterance 5.4-5.6 s, Codex remote preparation 6.2-9.5 s.

---

## Changes made to the device during this session

No repository source file was modified. `examples/llm-routing.codex-subscription.toml` was
left untouched; the adjustment lives in a copy.

| Change | Location | Reversible by |
| --- | --- | --- |
| `speech_chunk_max_delay_seconds` 0.75/0.90 -> 0.0 | `llm-routing.device.toml` in the deploy path | deleting the file and pointing `HELIOS_LLM_CONFIG` back at the example |
| `~/.helios-codex/auth.json` refreshed from `~/.codex` (twice) | device home | `auth.json.bak-20260919-190553`, `auth.json.bak-20260920-102952` |
| SSH deploy key installed | `~/.ssh/authorized_keys` | removing the `claude-deploy` line |
| Codex OAuth device-code sign-in | `~/.codex` | user's own account |

Launched with `HELIOS_LLM_CONFIG`, `HELIOS_KPI_ENABLED=true`, `HELIOS_LOG_LEVEL=DEBUG`,
`HELIOS_LOG_FILE`. Models and assets were copied from the device's existing installation
rather than uploaded; that installation was not modified. No system packages, services or
network configuration were changed. No microphone audio or user speech was recorded, and no
transcript content is retained here or in the evidence kept on the device.

## Scope of these measurements

Single-session observations from unscripted conversation on one device. Not a benchmark, and
not evidence of Jetson acoustic, latency, thermal or power validation. No product-parity
claim is made.
