# Hybrid LLM deployment and operations

This document separates what the repository now enforces in code from the
deployment decisions that require credentials, current provider facts, legal
review, a target Jetson, or a representative network.

The safe default remains unchanged: if no `HELIOS_LLM_CONFIG` is set, Helios
uses the existing local Ollama models. An invalid routing file, invalid
environment override, stale catalog, unavailable budget ledger, missing
credential, privacy denial, or unavailable remote provider cannot silently
turn remote transmission on.

## What is implemented

The hybrid subsystem includes:

- typed provider-neutral requests, messages, stream events, usage, rate limits,
  finish reasons, capabilities, and sanitized errors;
- the extracted lazy Ollama adapter;
- one strict OpenAI-compatible Chat Completions SSE adapter with no internal
  retries;
- deterministic `local_only`, `remote_only`, `local_first`, `remote_first`,
  and `auto` routing;
- per-mode candidate chains, language/model selection, allowlists, denylists,
  context limits, feature checks, connectivity state, and provider health;
- a provenance-based privacy guard with a second authorization check inside
  the remote adapter;
- retry and fallback only before speech may have reached the listener;
- optional pre-speech buffering through `first_speech_min_chars`;
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
The active extractive RAG path is still local and does not call an LLM.

## Security and privacy invariants

Remote transmission requires every gate below to pass:

1. A valid routing TOML was loaded.
2. `router.remote_enabled` is true.
3. `HELIOS_LLM_REMOTE_ENABLED` has not disabled it.
4. `HELIOS_LLM_EMERGENCY_LOCAL_ONLY` is false.
5. The selected policy and candidate chain permit a remote target.
6. The provider and target are enabled and allowed.
7. Connectivity policy permits the attempt.
8. The request privacy level is not `local_only`.
9. Every message has an allowed provenance.
10. The coordinator grants `remote_authorized`.
11. The remote adapter rechecks `remote_authorized` immediately before POST.
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

API keys are looked up lazily by environment-variable name. Secret values are
not accepted in TOML, stored in `Settings`, emitted in metrics, or included in
sanitized provider errors. Remote endpoints must use HTTPS and cannot contain
credentials, query strings, or fragments.

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

Helios does not probe the internet during construction. Runtime connectivity is
`unknown` until an integrator supplies `Connectivity.ONLINE` or `OFFLINE`.
`unknown_connectivity = "prefer_local"` prevents `auto` remote escalation and
turns `remote_first` into local-first. `"allow_remote"` permits a configured
remote attempt while connectivity remains unknown.

## Streaming, retries, and failover

Every adapter performs one transport attempt. The coordinator owns retries and
fallback so it can enforce one global speech-commit rule.

Text deltas are collected exactly. Reasoning deltas are never spoken or
returned as visible output. Punctuation triggers the existing sentence
buffering behavior. Immediately before calling Piper, the coordinator marks
speech as committed. From that moment:

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

Environment variables can disable remote operation without a file. They cannot
construct a remote route by themselves.

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

For a real free-tier account, do not enter zero prices merely because the first
quota band is free unless operations can also detect quota exhaustion and
prevent paid overage. A zero-cost policy is only as reliable as the account
controls and catalog values behind it.

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

The normal suite is network-free and ignores API keys. A deployment-owned
remote-only configuration can be certified with one explicitly authorized
request:

```bash
export HELIOS_LLM_LIVE=1
export HELIOS_LLM_LIVE_CONFIG=/etc/helios/llm-routing-live.toml
python -m pytest tests/test_live_llm.py -m remote_live -q
```

The live file must use `remote_only`, current catalog data, a writable ledger,
and a strict spending cap. The test skips when opt-in, configuration, or
credentials are missing. It does not print the prompt, response, or key.

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
- filesystem ownership, ledger backup, clock synchronization, metrics rotation,
  and incident response;
- target-device latency, power, thermal, bandwidth, and audio acceptance
  thresholds;
- Italian/English answer-quality review by domain owners;
- production rollout window and on-call ownership.

Remote RAG generation is a separate future change. It requires explicit
document classification, prompt-injection defenses, source handling, and legal
approval. The current extractive RAG path should remain local.

## Rollout and rollback

Recommended rollout:

1. Ship this code with no `HELIOS_LLM_CONFIG`.
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
configuration file or code rollback is required. Removing
`HELIOS_LLM_CONFIG` also restores the original Ollama-only behavior.
