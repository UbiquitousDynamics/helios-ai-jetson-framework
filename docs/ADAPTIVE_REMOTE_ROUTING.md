# Adaptive remote model routing and latency

Helios uses a deterministic model cascade for the Codex/ChatGPT-subscription
route. This is a known routing pattern, also called complexity-aware routing
or a model cascade: use the smallest model that satisfies the request, then
escalate only when the estimated difficulty justifies it. Helios does not call
another LLM to classify the question, so selection adds no network latency,
tokens, subscription usage, or transcript exposure.

Before complexity selection, the fast connectivity gate must admit the remote
path. Missing route/carrier/IP, a stale or failed ChatGPT HTTPS validation, or
poor measured quality removes all remote tiers and selects local Ollama
immediately. See `docs/NETWORK_CONNECTIVITY_ROUTING.md`.

The implementation follows the general model-selection objective of meeting an
evaluated accuracy target with the smallest and fastest suitable model:

- <https://developers.openai.com/api/docs/guides/model-selection>
- <https://developers.openai.com/api/docs/guides/latest-model>

## Selection algorithm

`RoutePlanner.complexity_score()` assigns an explainable integer score:

- +2 when estimated input plus output reserve exceeds 80% of the available
  local context;
- +1 for `think`;
- +1 for more than 160 conservatively estimated input tokens;
- +1 for an Italian or English reasoning cue such as `spiega`, `confronta`,
  `calcola`, `explain`, or `compare`;
- +1 for at least three connectors or question separators;
- +1 for more than 64 estimated tokens of instruction, history, or tool
  context;
- +2 when a programmatic caller explicitly sets `complex=true`.

Each adaptive remote target declares `min_complexity_score`. Of the currently
eligible and healthy tiers, the router selects only the highest floor that is
less than or equal to the score. If no floor is below the score, it selects the
smallest available floor.

The committed Codex profile uses:

| Score | Remote tier | Talk reasoning | Normal purpose |
|---|---|---|---|
| 0-2 | `gpt-5.6-luna` | `none` | Short, direct conversation |
| 3-4 | `gpt-5.6-terra` | `low` | Explanations and moderate reasoning |
| 5+ | `gpt-5.6-sol` | `low` | Long or multi-step difficult requests |

The fixed Emilia system instruction contributes roughly two points to a normal
hybrid `talk` request. A `think` request normally starts around three. These
floors are therefore calibrated for the current prompt and must be validated
again if the scoring rules or fixed instruction change.

Only one adaptive remote tier is placed in the execution plan:

```text
selected Luna/Terra/Sol -> local Ollama fallback
```

The router does not put all three remote tiers before Ollama. That would turn
one outage into as many as three consecutive remote timeouts. If the preferred
tier is circuit-open, the next healthy tier is selected. If the selected
remote fails during the request before speech begins, Helios goes directly to
the local target.

`remote_first` remains the right policy for this profile. The `auto` policy
controls remote-versus-local order; `min_complexity_score` controls model size
within the remote side.

## Remote latency behavior

The Codex adapter already consumes `item/agentMessage/delta` events as they
arrive. Helios now turns those deltas into speech before `turn/completed`:

- the first complete sentence is removed from the buffer and sent to Piper
  immediately;
- commas no longer create tiny, choppy speech fragments;
- `speech_chunk_max_chars` creates a soft whitespace boundary when a response
  contains no punctuation;
- after the first fragment is committed to speech, retry/fallback remains
  prohibited so the listener never hears a duplicated answer.

This matches OpenAI's streaming guidance: visible deltas should be rendered
while the rest of the response is still being generated:

- <https://developers.openai.com/api/docs/guides/streaming-responses>
- <https://developers.openai.com/api/docs/guides/latency-optimization>

The committed profile also applies these latency controls:

| Control | `talk` | `think` |
|---|---:|---:|
| `first_speech_min_chars` | 0 | 0 |
| `speech_chunk_max_chars` | 80 | 96 |
| `first_visible_token_seconds` | 15 s | 30 s |
| maximum output | 128 tokens / 50 words | 256 tokens |

Hidden reasoning deltas do not satisfy or reset the visible-first-token
deadline. A slow remote attempt can therefore fall back before any remote
speech reaches the listener. Piper still synthesizes each selected fragment
before playing it, so shorter fragments also reduce time to the first audible
audio.

At assistant startup, Helios prepares the Codex process and validates the
ChatGPT account in a background thread while the welcome message is spoken.
Preparation does not start a model turn and sends no prompt. The process and
successful account check are reused by later requests; each request still uses
a fresh ephemeral Codex thread so conversation history cannot leak between
voice commands.

## Observe the selected and actual model

With normal INFO logging, a request emits content-free lines similar to:

```text
Adaptive remote tier selection: complexity_score=3, minimum_score=3, selected=codex-talk-terra
Planning talk request with eligible routes in fallback order: codex-talk-terra,local-talk
Completed talk request using route codex-talk-terra (provider=openai-codex, requested_model=gpt-5.6-terra, resolved_model=gpt-5.6-terra, attempts=1, first_text_ms=..., first_speech_ms=...)
```

`resolved_model` is important because Codex can report a server-side model
reroute. The completion line, not the planning line, identifies the route that
actually answered. `first_speech_ms` is the speech-commit/dispatch time before
Piper synthesis, not a sound-card playback timestamp. Metrics remain
content-free in `logs/llm-metrics.jsonl`.

## Jetson verification and tuning

First verify that the signed-in ChatGPT account exposes every configured model:

```bash
python scripts/codex_subscription.py status
python scripts/codex_subscription.py models
```

If Luna, Terra, or Sol is absent, do not guess an identifier. Replace that
target with an ID returned by `models`, or remove that target from both the
relevant `modes.*.candidates` list and its `[targets.*]` table, then recalibrate
the neighboring floor.

Run the network-free regression set:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest \
  tests/test_connectivity.py \
  tests/test_routing.py \
  tests/test_streaming.py \
  tests/test_codex_app_server.py \
  tests/test_hybrid_api_client.py \
  tests/test_config_llm.py -q
```

Then benchmark a reviewed prompt set on the real Jetson and network. Record at
least p50/p95 first-text, first-speech, and total latency per selected model,
plus fallback rate and answer-quality acceptance. Tune the two floors before
changing the algorithm. Increasing a floor sends more questions to the smaller
model; decreasing it escalates sooner.

## Deliberately configurable or not enabled by default

- `service_tier="fast"` is already passed through by the Codex adapter, but it
  may consume plan credits faster and depends on account/model support. Enable
  it only after checking `models` and measuring the real account.
- Hedged execution could start local inference after a remote p90 delay, but it
  spends extra Jetson work and can consume remote allowance even when local
  wins. It needs an explicit operational decision and is not enabled.
- The direct Responses or Realtime API may have a lower latency path than a
  coding-agent app-server, but it requires API-key billing and is not the
  OpenClaw-style ChatGPT-subscription mechanism requested for this deployment.
- Prompt caching is not a useful optimization for the current short Helios
  prompt; OpenAI caching starts at much larger prompt sizes.
- Model availability, ChatGPT usage limits, mobile-network quality, privacy
  approval, and acceptable answer quality cannot be fixed by repository code.
  They must be certified on the deployment account and vehicle.
