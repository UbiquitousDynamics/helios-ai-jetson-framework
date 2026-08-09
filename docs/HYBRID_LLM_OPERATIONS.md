# Hybrid LLM deployment and operations

This document separates what the repository now enforces in code from the
deployment decisions that require credentials, current provider facts, legal
review, a target Jetson, or a representative network.

The repository defaults to `examples/llm-routing.codex-subscription.toml` with
remote-first routing and local Ollama fallback. An invalid routing file,
invalid environment override, unavailable ChatGPT session, privacy denial,
connectivity failure, or unavailable remote provider falls back or fails
closed according to the validated policy.

## What is implemented

The hybrid subsystem includes:

- typed provider-neutral requests, messages, stream events, usage, rate limits,
  finish reasons, capabilities, and sanitized errors;
- the extracted lazy Ollama adapter;
- one strict OpenAI-compatible Chat Completions SSE adapter with no internal
  retries;
- deterministic `local_only`, `remote_only`, `local_first`, `remote_first`,
  and `auto` routing;
- an optional zero-request adaptive remote model cascade through
  `min_complexity_score`;
- per-mode candidate chains, language/model selection, allowlists, denylists,
  context limits, feature checks, connectivity state, and provider health;
- a fail-closed Linux connectivity monitor with a synchronous route/carrier/IP
  gate, netlink change events, background HTTPS validation, quality scoring,
  smoothing, and hysteresis;
- a provenance-based privacy guard with authorization rechecked at remote
  dispatch and again inside the OpenAI-compatible SSE adapter;
- retry and fallback only before speech may have reached the listener;
- sentence-level speech streaming, optional pre-speech buffering, and a soft
  maximum fragment size;
- provider/model cooldowns for transient, rate-limit, authentication, and
  quota failures;
- a strict, expiring JSON model catalog using exact decimal prices;
- append-only budget reservations and reconciliation with per-request, daily,
  monthly, and zero-cost limits;
- content-free metrics for provider, model, mode, language, latency, usage,
  cost, error category, and fallback count;
- strict versioned TOML configuration and four example policies;
- network-free unit tests plus an explicitly enabled live certification test.

`APIClient.talk()` and `APIClient.think()` remain the public compatibility
boundary. Existing callers do not need to know about provider response objects.
The voice loop selects `think` when the post-wake-word command begins with
`pensa`/`ragiona` in Italian or `think`/`reason` in English. Target-specific
`max_output_words` instructions can keep local speech short without applying
the same word limit to a remote target.
The active extractive RAG path is still local and does not call an LLM.

## Security and privacy invariants

Remote transmission requires every gate below to pass:

1. A valid routing TOML was loaded.
2. `router.remote_enabled` is true.
3. `HELIOS_LLM_REMOTE_ENABLED` is not explicitly set to `false`.
4. `HELIOS_LLM_EMERGENCY_LOCAL_ONLY` is false.
5. The selected policy and candidate chain permit a remote target.
6. The provider and target are enabled and allowed.
7. Connectivity policy permits the attempt.
8. The request privacy level is not `local_only`.
9. Every message has an allowed provenance.
10. The coordinator grants and rechecks `remote_authorized` at dispatch.
11. The OpenAI-compatible SSE adapter checks it again immediately before POST.
12. Budget/catalog checks pass when budget enforcement is enabled.
13. The named credential environment variable exists.

The provenance rules are:

| Content origin | Remote gate |
|---|---|
| `static_instruction` | Allowed after general remote authorization |
| `raw_transcript` | Requires `allow_remote_transcripts = true` |
| `conversation_history` or `tool_result` | Requires `allow_remote_context = true` |
| `local_document` or derivative | Requires `allow_remote_rag_context = true` |
| `unknown` | Always blocked from remote transmission |

The legacy `context` argument has unknown provenance unless the caller supplies
an explicit `context_origin`. This deliberately keeps existing or future RAG
content local. `remote_redacted` is enforced, but Helios does not contain a
general-purpose redactor: callers must provide already-redacted messages and
mark them as such.

API keys are looked up lazily by environment-variable name. Credential-like
option names and credentials embedded in endpoints are rejected, and the
supported API-key field accepts only an environment-variable name. The parser
cannot prove that an arbitrary string such as a model name, path, or stop
sequence is not a secret, so the operational rule remains: never place secret
values anywhere in routing TOML. Secrets are not emitted in metrics or included
in sanitized provider errors. Remote endpoints must use HTTPS and cannot
contain credentials, query strings, or fragments.

## Routing behavior

Eligibility is evaluated before any provider is constructed:

- target enabled;
- target appears in the active mode chain;
- provider/target allowlist and denylist;
- language and required capabilities;
- estimated input plus reserved output within the context window;
- health circuit available;
- privacy authorization for remote targets;
- connectivity not explicitly offline.

The policies then order eligible targets:

| Policy | Ordering |
|---|---|
| `local_only` | Local targets only |
| `remote_only` | Remote targets only |
| `local_first` | Local chain, then remote chain |
| `remote_first` | Remote chain, then local chain |
| `auto` | Explainable complexity score chooses remote-first or local-first |

`auto` adds:

- 2 points when estimated input plus output reserve exceeds 80% of the largest
  eligible local context window;
- 1 for `think`;
- 1 above 160 conservatively estimated input tokens;
- 1 for a reasoning cue such as “analyze”, “compare”, “spiega”, or “calcola”;
- 1 for three or more connectors/questions;
- 1 for more than 64 estimated tokens of instruction/history/tool context;
- 2 when an API caller explicitly supplies `request_options={"complex": true}`.

The mode’s `complexity_threshold` selects remote-first when the score reaches
the threshold. Candidate order remains deterministic. Health and budget can
remove a candidate but do not use an opaque learned ranking.

Remote targets that declare `min_complexity_score` form an adaptive model
cascade independently of the selected policy. The planner retains only the
healthy tier with the highest floor not exceeding the score (or the smallest
available floor when none is below it). Untiered targets retain their normal
behavior. Configure one local candidate after the adaptive remotes so a plan
contains one selected remote followed directly by local fallback. See
`docs/ADAPTIVE_REMOTE_ROUTING.md`.

Health records latency EWMA for observation and circuit decisions, but current
routing does not reorder candidates from that EWMA. Battery state, Jetson power
mode, thermal headroom, and metered-link state are not runtime inputs yet; an
integrator must supply those policies before they can influence routing.

When `[network].enabled=true`, Helios starts a background HTTPS path monitor.
Every request first performs the packet-free passive gate; an absent default
route, carrier, or usable IP returns offline immediately. Remote also requires
a fresh successful quality result. Unknown and stale states therefore remain
local under `unknown_connectivity="prefer_local"`. See
`docs/NETWORK_CONNECTIVITY_ROUTING.md` for the estimator, configuration, and
Jetson diagnostic command.

## Streaming, retries, and failover

Every adapter performs one transport attempt. The coordinator owns retries and
fallback so it can enforce one global speech-commit rule.

Text deltas are collected exactly. Reasoning deltas are never spoken or
returned as visible output. Complete sentences are removed from the speech
buffer as soon as they arrive; commas do not create fragments.
`speech_chunk_max_chars` adds a soft whitespace cutoff for unpunctuated text.
Immediately before calling Piper, the coordinator marks speech as committed.
From that moment:

- the same provider is not retried;
- another provider is not tried;
- Ollama fallback is not called;
- the partial answer is not replayed.

Before speech commits, partial text is discarded when an attempt fails.
Retrying the same provider requires a normalized
`retryable_same_provider = true` error and an available retry slot. Retry-After
delays above the configured coordinator cap are not slept; the next candidate
is preferred. A safety refusal and cancellation are terminal. A TTS exception
is returned unchanged and is never relabeled as a provider error.

| Failure | Same provider | Next target/local fallback | After speech |
|---|---|---|---|
| Offline/DNS/TLS/connect failure | If classified transient and retry slot remains | Yes | Stop |
| First-token/read timeout | Yes, before speech | Yes | Stop |
| Stream interruption | Yes, before speech | Yes | Stop |
| 401/403 | No; health blocks until reset/restart | Yes | Stop |
| 408/425/429/5xx | Bounded retry when safe | Yes; cooldown recorded | Stop |
| Quota exhausted | No; quota health block | Yes | Stop |
| Context overflow/unsupported feature | No | Yes if another eligible target exists | Stop |
| Malformed or empty completion | Bounded retry | Yes | Stop |
| Safety refusal | No | No | Stop |
| TTS failure | No | No | Return original TTS error |

`first_speech_min_chars` can improve safe failover by delaying the first spoken
sentence. Keep it at `0` for legacy latency. Values around 20–40 characters are
a reasonable benchmark range, not a production recommendation.
`first_visible_token_seconds` overrides the provider first-token deadline for
one mode. Hidden Codex reasoning does not satisfy or reset it. A value that is
too aggressive increases local fallback; tune it from Jetson p95 measurements.
Set `speech_chunk_max_chars = 0` to disable soft chunking.

`APIClient.cancel_current()` cancels active streams, and
`VoiceAssistant.stop()` invokes it. Cancellation is checked before dispatch and
between received chunks. A thread already blocked in an HTTP or Ollama read can
only return when that transport produces data, closes, or reaches its configured
read/total timeout; cancellation is cooperative, not an unbounded hard kill.

## Configuration

Install the optional HTTP dependency only on images that may use remote
providers:

```bash
python -m pip install -r requirements-remote.txt
```

Choose and copy one example:

- `examples/llm-routing.offline.toml`
- `examples/llm-routing.free-tier-first.toml`
- `examples/llm-routing.paid-first.toml`
- `examples/llm-routing.local-first-escalation.toml`
- `examples/llm-routing.codex-subscription.toml`

Then set:

```bash
export HELIOS_LLM_CONFIG=/etc/helios/llm-routing.toml
export HELIOS_LLM_REMOTE_ENABLED=true
```

PowerShell:

```powershell
$env:HELIOS_LLM_CONFIG = "C:\ProgramData\Helios\llm-routing.toml"
$env:HELIOS_LLM_REMOTE_ENABLED = "true"
```

The validated TOML value is the default. `HELIOS_LLM_REMOTE_ENABLED=false`
overrides it and disables remote transmission; setting it to `true` can enable
only a valid profile whose router also permits remote operation.

The routing file stores only the environment-variable name:

```toml
[providers.groq]
adapter = "openai_chat_sse"
endpoint = "https://api.groq.com/openai/v1"
locality = "remote"
api_key_env = "GROQ_API_KEY"
internal_retries = 0
```

Inject the actual key with the vehicle’s secret manager, systemd credential,
container secret, CI secret, or a deployment-owned protected environment file.
Do not add it to `.env.example`, TOML, service arguments, shell history, or Git.

The legacy Ollama host is trusted as device-local only when it is loopback. A
non-loopback Ollama URL is treated as remote and fails closed unless it is
classified explicitly with an endpoint that exactly matches the runtime
Ollama host:

```toml
[providers.ollama]
adapter = "ollama"
endpoint = "http://ollama.lan:11434"
locality = "trusted_lan"
enabled = true
internal_retries = 0
```

Declaring `trusted_lan` is a deployment security decision: the operator must
provide network isolation, authentication controls where available, and an
egress/privacy review. Use `locality = "device"` only with a loopback endpoint.

Supported environment overrides are:

| Variable | Purpose |
|---|---|
| `HELIOS_LLM_CONFIG` | Versioned TOML path |
| `HELIOS_LLM_REMOTE_ENABLED` | Can disable or enable a valid remote config |
| `HELIOS_LLM_EMERGENCY_LOCAL_ONLY` | Immediate local-only kill switch |
| `HELIOS_LLM_POLICY` | Policy override |
| `HELIOS_LLM_ALLOW_REMOTE_TRANSCRIPTS` | Transcript egress gate |
| `HELIOS_LLM_ALLOW_REMOTE_CONTEXT` | History/tool-context egress gate |
| `HELIOS_LLM_ALLOW_REMOTE_RAG` | Local-document egress gate |
| `HELIOS_LLM_CATALOG` | Current catalog path |
| `HELIOS_LLM_DAILY_BUDGET_USD` | Daily hard limit |
| `HELIOS_LLM_MONTHLY_BUDGET_USD` | Monthly hard limit |
| `HELIOS_LLM_ZERO_COST_ONLY` | Reject every nonzero reservation |
| `HELIOS_LLM_METRICS_ENABLED` | Content-free metrics switch |
| `HELIOS_LLM_LOG_CONTENT` | Reserved; content is still not logged |
| `HELIOS_LLM_LOG_HEADERS` | Reserved; headers are still not logged |

Environment variables can disable remote operation without a file. They cannot
construct a remote route by themselves.

`observability.metrics_retention_days` is implemented as daily pruning of the
content-free JSONL metrics file. The `log_content` and `log_headers` settings are
reserved and must remain false. Missing provider usage always settles the full
budget reservation; no alternate `missing_usage` behavior is implemented.

## Catalog and budget sign-off

`examples/model-catalog.example.json` is intentionally stale and contains
fail-closed placeholder prices. It must never be promoted unchanged.

For every configured remote target, an operator must:

1. Open the provider’s official model, pricing, rate-limit, and data-control
   documentation.
2. Confirm the exact API model identifier and endpoint for the deployment
   account and region.
3. Record prices as decimal strings per one million tokens.
4. Record the real context and maximum-output limits.
5. Decide whether the account currently has a free tier. Treat free capacity
   as interruptible; do not equate open weights or trial credits with permanent
   free service.
6. Set `verified_on`, a short `expires_on`, and a change-controlled revision.
7. Have a second reviewer compare the JSON with the cited official pages.
8. Run catalog, budget, fake-transport, and opt-in live tests.

The catalog entry’s provider and model must exactly match the route. A stale or
missing entry blocks the remote attempt. Reservations use the conservative
estimated input and maximum output before network dispatch. Returned usage
reconciles the charge; missing usage settles the full reservation.

Budget limits are currently global across all remote routes. Per-provider and
per-model sub-budgets are not implemented. The catalog’s `free_tier` flag is
informational and never overrides the exact decimal prices or `zero_cost_only`
reservation check.

For a real free-tier account, do not enter zero prices merely because the first
quota band is free unless operations can also detect quota exhaustion and
prevent paid overage. A zero-cost policy is only as reliable as the account
controls and catalog values behind it.

The append-only ledger uses a continuity sidecar to detect ordinary truncation,
replacement, and clock rollback. Coordinated deletion of the ledger, sidecar,
and lock file cannot be detected without an external trusted anchor. Production
deployments that require that guarantee must anchor ledger state in a protected
service, TPM-backed store, remote audit system, or equivalent control.

Useful primary documentation starting points, which still require verification
on the deployment date, are:

- Groq API compatibility, rate limits, data controls, and pricing:
  `https://console.groq.com/docs`, `https://groq.com/pricing/`
- OpenAI API reference, data controls, rate limits, and pricing:
  `https://platform.openai.com/docs`, `https://openai.com/api/pricing/`

Other providers should be added through the same strict SSE adapter only after
fixture and live certification proves their Chat Completions behavior matches.
Providers with different authentication, streaming, safety, or message
semantics need a native adapter. Do not assume “OpenAI compatible” means
behaviorally identical.

## Live certification

The native Codex app-server/ChatGPT-subscription route is documented separately
in `docs/CODEX_SUBSCRIPTION.md`. It deliberately forbids `api_key_env`, verifies
an account of type `chatgpt`, and disables API-cost accounting because a
subscription is not API-key billing.

The normal suite is network-free and ignores API keys. A deployment-owned
remote-only configuration can be certified with one explicitly authorized
request:

```bash
python -m pip install -r requirements-dev.txt
export HELIOS_LLM_LIVE=1
export HELIOS_LLM_LIVE_CONFIG=/etc/helios/llm-routing-live.toml
python -m pytest tests/test_live_llm.py -m remote_live -q
```

`pytest` is intentionally not part of `requirements-jetson.txt`, which is the
runtime-only installation contract. Install `requirements-dev.txt` whenever
tests are to run on the target.

The live file must use `remote_only`, keep a local Ollama target available for
the emergency switch, use current catalog data, a writable ledger, and a strict
spending cap. `remote_only` filters the local candidate during the test. The
test skips when opt-in, configuration, or credentials are missing. It does not
print the prompt, response, or key.

Before production, inject failures with fake transports and on a staging
network:

- DNS failure;
- invalid certificate;
- connect and first-token timeout;
- 429 with Retry-After;
- 401/403;
- quota exhaustion;
- truncated and malformed SSE;
- stream loss before and after first audio;
- unavailable Ollama;
- unwritable or corrupt budget ledger.

## Target Jetson benchmark

Code and CI cannot decide whether offloading improves the vehicle experience.
Benchmark on the exact Jetson/JetPack image, microphone/audio stack, Ollama
models, modem, SIM/APN, antenna placement, and expected power mode.

Use a reviewed set containing at least:

- 10 short Italian and 10 short English `talk` prompts;
- 10 Italian and 10 English multi-step `think` prompts;
- simple, complex, long-context, sensitive, and deliberately local-only cases;
- representative provider refusals and rate-limit simulations.

Record per target and network condition:

- time to first token;
- time to first spoken sentence;
- total response latency;
- local model load time and tokens/second;
- fallback latency;
- response length;
- input/output/reasoning token counts;
- reserved and reconciled cost;
- request/fallback/error rate;
- transmitted and received bytes;
- RAM, CPU, GPU, temperature, and `tegrastats` power data.

Test strong Wi-Fi, weak Wi-Fi, normal cellular, high-latency cellular, packet
loss, metered limits, and full offline mode. Do not select remote-first for
`talk` until its p95 first-audio latency and failure behavior beat the local
path under the accepted operating envelope.

## Human and deployment decisions

The following cannot be completed safely from the repository and must be
signed off before remote enablement:

- provider account ownership, billing alarms, hard spend controls, and key
  rotation/revocation;
- current model availability, free-tier rules, prices, quotas, and rate limits;
- provider retention, training, abuse-monitoring, region, subprocessors, and
  enterprise data-control settings;
- GDPR/privacy notice, consent or lawful basis for transmitting in-vehicle
  speech, controller/processor roles, data residency, and deletion requests;
- model/output licensing and acceptable-use review for the vehicle’s use cases;
- a definition of sensitive speech and whether any transcript may leave the
  vehicle;
- selection of a trusted connectivity signal and battery/resource policy;
- filesystem ownership, ledger backup, external continuity anchoring where
  required, clock synchronization, metrics archival, and incident response;
- target-device latency, power, thermal, bandwidth, and audio acceptance
  thresholds;
- Italian/English answer-quality review by domain owners;
- production rollout window and on-call ownership.

Remote RAG generation is a separate future change. It requires explicit
document classification, prompt-injection defenses, source handling, and legal
approval. The current extractive RAG path should remain local.

## Rollout and rollback

Recommended rollout:

1. Validate the bundled Codex-subscription profile and its Ollama fallback.
2. Validate the offline example and all existing Ollama behavior.
3. Configure a remote provider in staging with `remote_only` and a tiny budget.
4. Run the live certification and failure injection.
5. Benchmark `think` with local fallback.
6. Canary `auto` for `think`; keep `talk` local.
7. Enable remote `talk` only if measurements and privacy review support it.

Immediate rollback:

```bash
export HELIOS_LLM_EMERGENCY_LOCAL_ONLY=true
```

Restart the process so all remote provider instances and transports close. No
configuration file or code rollback is required. Even a configured remote-only
mode receives an emergency local Ollama route, and emergency mode ignores
normal route allow/deny filters. This requires an available loopback or
explicitly trusted-LAN Ollama host. Unsetting `HELIOS_LLM_CONFIG` selects the
repository's remote-first default, so use `HELIOS_LLM_REMOTE_ENABLED=false` or
select `examples/llm-routing.offline.toml` for persistent Ollama-only behavior.
