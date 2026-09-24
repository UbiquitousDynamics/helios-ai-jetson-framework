# Operations


Helios can persist content-free voice, routing, provider, network, and device
metrics to SQLite and display them in a responsive static dashboard. It uses a
bounded non-blocking queue and asynchronous batched writes; queue or storage
failure is isolated from recognition, LLM, RAG, TTS, and playback. No prompt,
transcript, answer, document content, credential, address, interface name, or
provider request/attempt ID enters the KPI database, API, or export.

Enable collection and automatic local dashboard startup:

```bash
export HELIOS_KPI_ENABLED=true
export HELIOS_KPI_DASHBOARD_ENABLED=true
python3 scripts/run_jetson.py
```

Or serve the configured database separately, without running or collecting from
the assistant:

```bash
python scripts/kpi.py serve
```

Open <http://127.0.0.1:8765/> locally. From another computer, keep the listener
on loopback and forward it through SSH:

```bash
ssh -L 8765:127.0.0.1:8765 emilia@<jetson-address>
```

Direct LAN binding requires explicit `allow_lan` configuration and a dashboard
token of at least 24 characters. Browsers can use HTTP Basic authentication with
username `helios` and that token as the password; API clients can send the same
token as a Bearer credential. Both schemes protect static assets and API routes.
The built-in server does not provide TLS, so SSH or a reviewed
TLS/authenticating reverse proxy is preferred. Storage management is explicit:

```bash
python scripts/kpi.py status
python scripts/kpi.py export json --output helios-kpi.json --limit 1000
python scripts/kpi.py export csv --output helios-kpi.csv --limit 1000
python scripts/kpi.py clear --yes
```

The dashboard prefers `actual_first_audio_ms`, the production clock captured
immediately before the playback-backend call. If that real playback boundary is
unavailable, it falls back to the historical `first_audio_ms`/
`speech_dispatch_ms` time to first TTS dispatch. `listening_ms` is recognition-
call wall time; `stt_ms` remains unavailable unless the STT engine exposes a
separate compute duration.

On Jetson, resource sampling combines `tegrastats` with read-only sysfs
fallbacks. GPU frequency can come from devfreq, and input power can come from
an INA3221 rail named `VDD_IN`, `POM_5V_IN`, `VIN_SYS_5V0`, or `SYS5V` when the
normal Helios user has permission to read it. A measured `0%` is valid idle GPU
utilization; `—` means the source is absent, unsupported, or unreadable. Older
JetPack `tegrastats` versions without `--count` and the Python 3.10.0 query
parser shipped by early JetPack images are supported. Do not run the assistant
as root merely to populate an optional dashboard field; grant narrowly scoped
read access or leave the field unavailable.

Run the content-free synthetic KPI benchmark with:

```bash
python scripts/benchmark_kpi.py --pretty
```

It reports record-call percentiles, asynchronous SQLite throughput, raw and
dashboard-summary query latency, bounded-queue memory/drop behavior, and resource
sampler overhead as JSON without enforcing machine-dependent thresholds.
Architecture, all KPI definitions and units, API filters, retention, security,
Jetson metric availability, and benchmark methodology are documented in
[`docs/KPI_OBSERVABILITY.md`](docs/KPI_OBSERVABILITY.md).

## Running and using the assistant

From the repository root, with Ollama and the Python environment ready, desktop
deployments can run:

```bash
python main.py
```

Jetson deployments should use the bootstrap entry point so the correct
interpreter and native OpenMP runtime are selected before Python starts:

```bash
python3 scripts/run_jetson.py
```

When `HELIOS_KPI_ENABLED=true`, the same lifecycle owns and cleanly closes the
background KPI writer and resource sampler. When
`HELIOS_KPI_DASHBOARD_ENABLED=true`, it also starts the configured read-only
dashboard. Failure to initialize either optional component is logged without
preventing the voice assistant from running.

Expected behavior:

1. logging is configured;
2. lightweight service adapters are constructed;
3. the local Ollama talk model warms in the background without a user prompt;
4. an eligible Codex route may prepare in the background without sending text;
5. Vosk/PyAudio prepare in the background without opening the input stream;
6. Piper loads and speaks the localized welcome message concurrently;
7. the assistant waits in `COMMAND` state;
8. RAG stays lazy until RAG-mode entry.

Typical content-free INFO records for a hybrid request look like:

```text
Adaptive remote tier selection: complexity_score=3, minimum_score=3, selected=codex-talk-terra
Planning talk request with eligible routes in fallback order: codex-talk-terra,local-talk
Completed talk request using route codex-talk-terra (provider=openai-codex, requested_model=gpt-5.6-terra, resolved_model=gpt-5.6-terra, attempts=1, ...)
```

The planning line reports candidates, not the provider that ultimately
answered. Use the completion line and its `provider`, `requested_model`, and
`resolved_model` fields. A plan containing only `local-talk` means the remote
route was excluded by configuration, privacy, connectivity, health, catalog,
budget, or provider eligibility.

### Example: conversational answer

Say:

```text
Emilia, spiegami come funziona la tua intelligenza artificiale
```

The assistant removes the activation occurrence of the wake word and sends
`spiegami come funziona la tua intelligenza artificiale` to the selected
`talk` route.

### Example: reasoned answer

Say:

```text
Emilia, pensa: confronta due strategie energetiche
```

The assistant removes `Emilia` and `pensa`, selects the configured `think`
route, and speaks the streamed answer.

### Example: predefined introduction

Say:

```text
Emilia, chi sei?
```

The assistant selects one of the configured Italian introduction responses and
speaks it without contacting Ollama.

### Example: regulations search

First say:

```text
regolamento
```

After the wake cue, ask:

```text
Quanta acqua deve avere ogni occupante?
```

The top passages from the local knowledge base are spoken with the Italian RAG
prefix. The stop cue plays when the assistant returns to `COMMAND`.

The application has no spoken shutdown command. Stop it from the terminal with
`Ctrl+C`.

### In-process Python API

`APIClient` is the stable compatibility facade:

```python
from api.api_client import APIClient

with APIClient() as client:
    short_answer = client.talk("Spiega in breve il progetto")
    detailed_answer = client.think(
        "Confronta inferenza locale e remota",
        tts=False,
    )
```

`talk()` speaks by default. `think()` returns text without speech unless
`tts=True`. Both accept optional context, provenance, privacy, connectivity,
redaction attestations, request options, and cancellation; consult the method
signatures in `api/api_client.py` before integrating non-voice callers. There
is no REST, WebSocket, MQTT, or gRPC server in this repository.

### Manual TTS smoke check

The smoke script has no import-time audio side effects and no artificial sleep:

```bash
python scripts/smoke_tts.py
python scripts/smoke_tts.py "Frase di prova"
```


## Logging and diagnostics

`main.py` configures logging from `Settings`:

- default level: `INFO`;
- default file: `app.log` under the repository root;
- default mode: append;
- UTF-8 encoding;
- no module configures and truncates its own log at import time;
- logging is not globally disabled.

To enable debug output on the terminal for one launch:

```bash
HELIOS_LOG_LEVEL=DEBUG HELIOS_LOG_FILE=- python3 scripts/run_jetson.py
```

With the default file destination, follow only new records:

```bash
tail -n 0 -f app.log
```

Recoverable API, recognition, TTS, sound, and assistant errors are logged. The
assistant resets to `COMMAND` and continues when recovery is safe.

For barge-in root-cause analysis, keep the content-free event fields and filter
the live DEBUG stream without logging transcripts:

```bash
HELIOS_LOG_LEVEL=DEBUG HELIOS_LOG_FILE=- python3 scripts/run_jetson.py 2>&1 \
  | grep -E 'barge_in_|stt_finalized|assistant_turn_|thread_(start|resume|recover)|response_cancel'
```

`barge_in_stt_decision` records only the segment id, final/partial state, word
count, Vosk confidence/duration, RMS values, pending state, and decision. The
associated suppression reason shows whether Helios rejected TTS echo,
energy-only re-emission, a pre-playback segment, an unconfirmed short final, or
an inconsistent/low-confidence final. `barge_in_candidate_armed` pauses only
Piper playback; `barge_in_detected` is the later final-confirmed model
cancellation. Correlate both with the content-free `conversation_session` and
`turn` fields to verify that the next finalized utterance became the active
request instead of a new logical chat.

The TTS adapter supports both the Piper 1.2
`synthesize(text, wav_file)` API and the Piper 1.3+
`synthesize_wav(text, wav_file)` API. Errors raised before a WAV header exists
remain visible instead of being replaced by `wave.Error: # channels not
specified`.

On Jetson, a scikit-learn wheel may contain a private, renamed `libgomp`.
Preloading only `/usr/lib/aarch64-linux-gnu/libgomp.so.1` does not necessarily
select that copy. `scripts/run_jetson.py` discovers the wheel library
dynamically, so its hash-bearing filename must not be copied into `.bashrc` or a
service definition. Native-library provisioning remains tied to the installed
JetPack/L4T release and is intentionally not performed by the application.

RAG corruption and stale-index errors are intentionally explicit. They include
a rebuild instruction instead of silently returning a potentially unrelated
passage.

Provider metrics use a closed schema that has no prompt, transcript, retrieved
passage, response, header, or key field. The KPI SQLite sanitizer additionally
omits provider request/attempt identifiers, endpoints, interface names, network
addresses, and arbitrary exception text. Its API and JSON/CSV exports use the
same allowlist. Only an interface-availability boolean and controlled coarse
states are eligible. Do not publish general application logs if other
operational queries are sensitive; logs and the legacy optional LLM JSONL sink
are separate from the KPI export contract.


## Troubleshooting

| Symptom | Likely cause and action |
|---|---|
| No welcome message | Verify the selected Piper `.onnx` and adjacent `.onnx.json`, `piper-tts`, ONNX Runtime, and the default output device. |
| Ollama cannot be reached | Start `ollama serve`, verify `HELIOS_OLLAMA_HOST`, and check the configured tag with `ollama list`. |
| Conversational stream stops after speaking part of an answer | Check `app.log`. The request is intentionally not replayed after speech begins. |
| Remote route always falls back locally | Check the privacy gates, connectivity state, catalog expiry, ledger permissions, budget, provider allowlist, and named credential variable. |
| Codex subscription route is rejected | Run `python scripts/codex_subscription.py status`; Helios accepts only account type `chatgpt`, not `apiKey`. |
| A configured Codex model fails | Run `python scripts/codex_subscription.py models` and use only exact IDs returned for that account. |
| Network diagnostic returns nonzero | Inspect its `passive_gate`, `active_quality`, and `decision`; verify route/carrier/IP, TLS reachability, freshness, and quality thresholds. |
| KPI database stays empty | Set `HELIOS_KPI_ENABLED=true`, restart Helios, execute a request, and inspect `python scripts/kpi.py status`. Invalid KPI settings fail disabled. |
| KPI dashboard does not start | Set `HELIOS_KPI_DASHBOARD_ENABLED=true`, check whether `127.0.0.1:8765` is available, and inspect the sanitized warning. Use `python scripts/kpi.py serve` to serve the configured store separately. |
| KPI LAN bind is rejected or returns `401` | Prefer SSH forwarding. Direct LAN use requires explicit allow-LAN and a token of at least 24 characters. In a browser use Basic username `helios` with the token as password; API clients may use Bearer authentication. |
| Jetson resource cards are blank | Run as the normal service user and check readable `/proc`, thermal sysfs, and `tegrastats` availability. Unsupported fields intentionally remain unavailable and never require root. |
| KPI export is refused | Set `HELIOS_KPI_EXPORT_ENABLED=true` and keep `--limit` within `HELIOS_KPI_MAX_EXPORT_ROWS`. |
| Live remote test is skipped | Set `HELIOS_LLM_LIVE=1`, point `HELIOS_LLM_LIVE_CONFIG` to a reviewed configuration whose policy is exactly `remote_only`, and provide its required authentication. |
| `export` reports “not a valid identifier” | Use `export NAME='value'` with no whitespace around `=`. |
| Remote routing must be stopped immediately | Set `HELIOS_LLM_EMERGENCY_LOCAL_ONLY=true` and restart Helios. |
| Vosk model fails to load | Verify `HELIOS_LANGUAGE` and the corresponding bundled Vosk directory. |
| No microphone transcription | Confirm PortAudio/PyAudio and the default 16 kHz-capable input device. |
| RAG index is missing | Run `python scripts/build_index.py`, or allow the first RAG request to build it. |
| RAG reports a legacy/stale/corrupt index | Remove only generated `embeddings.npz` and rebuild it from the current corpus/model. |
| RAG returns poor matches | Verify the corpus language/content and evaluate queries against a reviewed relevance set before changing models. |
| Wake/stop cues are silent | Install ALSA utilities and run `aplay sounds/wake_up.wav`. |
| Cue playback times out | Check the ALSA device; `SoundPlayer` terminates the wait after its configured timeout. |
| Asset doctor reports a hash mismatch | Restore the expected artifact or deliberately update and review `assets-manifest.json`. |
| Asset doctor reports license warnings | Review `THIRD_PARTY_NOTICES.md`; warnings mark unresolved release metadata. |
| Jetson pip install replaces an inference backend | Reinstall the JetPack-compatible backend and follow `requirements-jetson.txt`, including Piper `--no-deps`. |
| Process continues listening | Use `Ctrl+C`; there is currently no spoken stop command. |

## FAQ

### Does Helios AI require internet access?

Helios retains an Ollama fallback that works without internet once dependencies
and models are provisioned. The default Codex route, initial pip installation,
ChatGPT sign-in, Ollama model creation, and retrieval of missing assets require
connectivity.

### Does Helios remember previous conversations?

Helios retains bounded conversation history during the active in-process session,
including wake-free follow-ups and safe provider fallback. It clears that history
when the session ends or resets. Persistent user memory across process restarts
is not implemented.

### Does the ChatGPT-subscription route need an API key?

No. It uses the Codex app-server and a device-code ChatGPT login. The
OpenAI-compatible HTTP route is a separate integration and uses the
environment variable named by `api_key_env`.

### Are RAG documents sent to Ollama?

No. The active RAG flow embeds and ranks text locally, then speaks the retrieved
passages directly.

### Can I add PDFs?

Not directly. Convert a PDF to reviewed UTF-8 text, place the `.txt` file in
`uploads/`, and rebuild the index.

### Can I use a different Ollama model?

Yes. Change the relevant `LanguageProfile.talk_model` or inject a `Settings`
profile that names a tag shown by `ollama list`.

### Can I use a different microphone or speaker?

The libraries currently use their default devices. The adapters are injectable,
but a user-facing device-selection option has not been implemented.

### Is CUDA used for RAG?

The default builder and assistant use CPU. `scripts/build_index.py` accepts
`--device`, but any GPU choice must match the installed Torch build and should
be validated on the target.

### Why is `embeddings.npz` not in Git?

It is reproducible generated data derived from the corpus and model. Keeping it
local prevents a stale vector file from being mistaken for source truth. Its
embedded manifest provides runtime integrity after generation.

### Why does the doctor show warnings on a clean checkout?

The generated index may not exist yet, and some third-party asset provenance is
not fully recorded. These conditions are warnings. Missing required assets or
checksum mismatches are errors.

### Is this a complete vehicle-control system?

No. The repository implements voice interaction and information retrieval only.
Vehicle actuation and telemetry are outside the current codebase.

