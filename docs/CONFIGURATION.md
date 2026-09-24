# Configuration


Configuration is defined in [`config.py`](config.py). New code should use the
immutable `config.SETTINGS` object and its `LanguageProfile`. Historical
module-level constants remain as compatibility aliases.

### Environment variables

Copy [`.env.example`](.env.example) into a deployment-owned environment or
secret manager as a starting point. The application reads process environment
variables; it does not load `.env` files itself.

Core overrides:

```bash
export HELIOS_LANGUAGE=it
export HELIOS_OLLAMA_HOST=http://localhost:11434
export HELIOS_BARGE_IN_ENABLED=false
export HELIOS_BACKCHANNEL_DELAY_SECONDS=0.7
export HELIOS_LOG_LEVEL=INFO
export HELIOS_LOG_FILE=app.log
export HELIOS_AUDIO_INPUT_DEVICE="USB PnP Audio Device"
export HELIOS_AUDIO_OUTPUT_DEVICE="Tegra Analog"
export HELIOS_AUDIO_OUTPUT_LATENCY=high
```

PowerShell:

```powershell
$env:HELIOS_LANGUAGE = "it"
$env:HELIOS_OLLAMA_HOST = "http://localhost:11434"
$env:HELIOS_BARGE_IN_ENABLED = "false"
$env:HELIOS_BACKCHANNEL_DELAY_SECONDS = "0.7"
$env:HELIOS_LOG_LEVEL = "INFO"
$env:HELIOS_LOG_FILE = "app.log"
$env:HELIOS_AUDIO_INPUT_DEVICE = "USB PnP Audio Device"
$env:HELIOS_AUDIO_OUTPUT_DEVICE = "Tegra Analog"
$env:HELIOS_AUDIO_OUTPUT_LATENCY = "high"
```

Supported language values are `it` and `en`. Unsupported values raise
`ConfigurationError` rather than selecting an incomplete profile.

Legacy values such as `http://localhost:11434/api/generate` are accepted for the
Ollama host and normalized to the SDK base host.

`HELIOS_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.
`HELIOS_LOG_FILE` is resolved from the repository root. Set it to `-` or an
empty value to send logs to stderr instead of a file:

```bash
HELIOS_LOG_LEVEL=DEBUG HELIOS_LOG_FILE=- python3 scripts/run_jetson.py
```

Hybrid routing uses a versioned TOML file. These overrides force offline mode:

```bash
export HELIOS_LLM_CONFIG=examples/llm-routing.offline.toml
export HELIOS_LLM_REMOTE_ENABLED=false
```

With no `HELIOS_LLM_CONFIG`, a clean checkout is local-only. The repository
also includes offline, free-tier-first, paid-first, local-first escalation, and
Codex-subscription examples; selecting one is an explicit deployment choice.
The committed catalog is intentionally stale and must be replaced with reviewed
current provider data.
See
[`docs/HYBRID_LLM_OPERATIONS.md`](docs/HYBRID_LLM_OPERATIONS.md) for the full
configuration, credential, privacy, budget, live-test, benchmark, rollout, and
human-review checklist.

For OpenClaw-style ChatGPT subscription routing through the native Codex
app-server, see
[`docs/CODEX_SUBSCRIPTION.md`](docs/CODEX_SUBSCRIPTION.md). It requires no API
key and keeps Ollama as the configured fallback.

All implemented LLM environment overrides are:

| Variable | Purpose |
|---|---|
| `HELIOS_LLM_CONFIG` | Path to a version-1 routing TOML |
| `HELIOS_LLM_REMOTE_ENABLED` | Independent remote-transmission gate |
| `HELIOS_LLM_EMERGENCY_LOCAL_ONLY` | Force local-only operation after restart |
| `HELIOS_LLM_POLICY` | Override `local_only`, `remote_only`, `local_first`, `remote_first`, or `auto` |
| `HELIOS_LLM_ALLOW_REMOTE_TRANSCRIPTS` | Permit raw transcript origin remotely |
| `HELIOS_LLM_ALLOW_REMOTE_CONTEXT` | Permit canonical conversation history remotely and enable Codex thread resume/recovery |
| `HELIOS_LLM_CONTEXT_IDLE_TIMEOUT_SECONDS` | Expire the logical voice session and rotate idle Codex context; default `900` |
| `HELIOS_LLM_CONTEXT_MAX_TURNS` | Bound transmitted history and rotate a Codex thread before the next turn; default `20` |
| `HELIOS_LLM_ALLOW_REMOTE_RAG` | Permit local-document content remotely |
| `HELIOS_LLM_CATALOG` | Override the strict model catalog path |
| `HELIOS_LLM_DAILY_BUDGET_USD` | Override the daily USD limit |
| `HELIOS_LLM_MONTHLY_BUDGET_USD` | Override the monthly USD limit |
| `HELIOS_LLM_ZERO_COST_ONLY` | Reject nonzero cost reservations when true |
| `HELIOS_LLM_METRICS_ENABLED` | Enable content-free metrics |
| `HELIOS_LLM_LOG_CONTENT` | Reserved; content logging remains disabled |

Audio environment overrides are independent of routing:

| Variable | Purpose |
|---|---|
| `HELIOS_AUDIO_INPUT_DEVICE` | PyAudio index, input-device name, or `pulse:<exact source>`; blank uses the platform default |
| `HELIOS_AUDIO_INPUT_STRICT` | Fail if the requested input cannot be resolved; default `false` falls back with a warning |
| `HELIOS_AUDIO_INPUT_CHANNEL_MODE` | `mono` (default), `average`, `sum`, or `stronger` for stereo capture |
| `HELIOS_AUDIO_CAPTURE_LEVEL_MIN_RMS` | Positive RMS floor for the dead-input warning; default `0.001`, calibrated on Emilia's USB microphone. Does not change speech or barge-in detection |
| `HELIOS_AUDIO_CAPTURE_STALL_SECONDS` | Log a stalled capture if no frame arrives within this interval; default `5`; does not reopen the stream |
| `HELIOS_AUDIO_OUTPUT_DEVICE` | sounddevice output index or name; blank uses the platform default |
| `HELIOS_AUDIO_OUTPUT_LATENCY` | `high` (default, robust) or `low` (only after device-specific validation) |

The broader KPI recorder and dashboard use a separate `HELIOS_KPI_*`
configuration. Collection and automatic dashboard startup both default to
`false`; the default database is `logs/helios-kpi.sqlite3`, and the dashboard
defaults to `127.0.0.1:8765`. The principal switches are:

| Variable | Purpose |
|---|---|
| `HELIOS_KPI_ENABLED` | Enable sanitized SQLite KPI collection |
| `HELIOS_KPI_STORAGE_PATH` | Override the project-rooted SQLite path |
| `HELIOS_KPI_DASHBOARD_ENABLED` | Start the read-only dashboard with the assistant |
| `HELIOS_KPI_DASHBOARD_HOST` / `HELIOS_KPI_DASHBOARD_PORT` | Configure the local listener |
| `HELIOS_KPI_DASHBOARD_ALLOW_LAN` | Explicitly permit a non-loopback bind |
| `HELIOS_KPI_DASHBOARD_AUTH_TOKEN_ENV` | Name the environment variable holding the required LAN Basic/Bearer token |
| `HELIOS_KPI_EXPORT_ENABLED` | Enable bounded sanitized JSON/CSV export |

Queue, batch, flush, retention, rollup, size, resource interval, query, and
export limits are also configurable. The complete list, validation rules, and
secure defaults are in
[`docs/KPI_OBSERVABILITY.md`](docs/KPI_OBSERVABILITY.md#configuration). An
invalid KPI override disables the optional subsystem without changing assistant
or routing behavior.

`HELIOS_PYTHON` is consumed by `scripts/run_jetson.py` and must point to a
virtual-environment interpreter. API credentials use the environment-variable
name declared by the selected provider, such as `OPENAI_API_KEY` or
`GROQ_API_KEY`; secret values never belong in TOML or Git.

Environment overrides can disable remote operation, but cannot construct a
remote route without a validated TOML. Invalid files and invalid overrides
restore local-only behavior.

## Remote inference setup

Remote inference is optional. Choose exactly the mechanism appropriate for the
deployment.

### Codex through a ChatGPT subscription

This path follows the OpenClaw-style mechanism: the official Codex app-server
runs locally over stdio and uses a ChatGPT device login. It does **not** use
`OPENAI_API_KEY`.

```bash
python -m pip install -r requirements-remote.txt
python scripts/codex_subscription.py login
python scripts/codex_subscription.py status
python scripts/codex_subscription.py models
```

Open the displayed verification URL on any computer, enter the one-time code,
and complete sign-in. `status` must identify a `chatgpt` account. Then:

```bash
export HELIOS_LLM_CONFIG="$PWD/examples/llm-routing.codex-subscription.toml"
export HELIOS_LLM_REMOTE_ENABLED=true
python scripts/network_diagnostics.py
python3 scripts/run_jetson.py
```

The Codex child receives cleared API-key variables, an isolated temporary
`CODEX_HOME` containing only a private copy of `auth.json`, a temporary empty
workspace, read-only sandboxing, and deny-all approvals. Tools, shell, web,
plugins, connectors, and user Codex configuration are not exposed to the voice
request. The prompt still leaves the device and is subject to the signed-in
account's terms and limits.

Verify that every configured Luna/Terra/Sol identifier appears in the output of
`models`; otherwise remove or replace the unavailable target and its candidate
reference. Do not guess model identifiers.

### OpenAI-compatible API with an API key

Copy and review `examples/llm-routing.paid-first.toml`. Replace placeholder
model and catalog entries with current, independently verified provider data.
Inject the actual key outside source control:

```bash
export OPENAI_API_KEY='sk-example-not-a-real-key'
export HELIOS_LLM_CONFIG=/etc/helios/llm-routing.toml
export HELIOS_LLM_REMOTE_ENABLED=true
python3 scripts/run_jetson.py
```

The shell assignment must not contain whitespace around `=`. The adapter
accepts only HTTPS endpoints without embedded credentials, query strings, or
fragments and performs no hidden internal retry.

The example catalog is deliberately expired and uses blocking placeholder
prices. It is documentation, not a ready production catalog. Complete the
account, pricing, privacy, budget, and failure-injection checklist in
[Hybrid LLM deployment and operations](docs/HYBRID_LLM_OPERATIONS.md) before
enabling a paid or free-tier API route.

### Immediate rollback

```bash
export HELIOS_LLM_EMERGENCY_LOCAL_ONLY=true
```

Restart Helios after changing the switch. Remove or set it to `false` only
after the remote issue has been reviewed.

## Active settings

| `Settings` field | Default | Runtime effect |
|---|---|---|
| `project_root` | Repository root | Anchors models, corpus, index, sounds, and logs |
| `language` | `"it"` | Selects Vosk, Piper, prompts, trigger, and chat model |
| `name` | `"emilia"` | Compatibility assistant identity |
| `listen_timeout` | `6.5` seconds | Maximum duration of one recognition call |
| `activation_timeout_seconds` | `30` seconds | Wake-free follow-up window, independent of one recognition call; overridden by `HELIOS_ACTIVATION_TIMEOUT_SECONDS` |
| `barge_in_enabled` | `true` | Enables interruptible listen-while-speaking turns; overridden by `HELIOS_BARGE_IN_ENABLED` |
| `barge_in_event_energy` | `0.08` | Legacy-event detection energy; overridden by `HELIOS_BARGE_IN_EVENT_ENERGY` |
| `barge_in_expected_echo_energy` | `0.04` | Calibrated Piper leakage RMS; overridden by `HELIOS_BARGE_IN_EXPECTED_ECHO_ENERGY` |
| `barge_in_minimum_interrupt_energy` | `0.06` | Conservative interruption floor; overridden by `HELIOS_BARGE_IN_MINIMUM_INTERRUPT_ENERGY` |
| `backchannel_delay_seconds` | `0.7` seconds | Silent-gap threshold for a cached cue; overridden by `HELIOS_BACKCHANNEL_DELAY_SECONDS` |
| `log_level` | `INFO` | Root logging level; overridden by `HELIOS_LOG_LEVEL` |
| `log_file_name` | `app.log` | Rotated log (5 MiB plus three backups); overridden by `HELIOS_LOG_FILE` |
| `ollama_host` | `http://localhost:11434` | Host passed to the Ollama SDK |
| `think_model` | `qwen3:0.6b` | Model used by `APIClient.think()` |
| `top_k` | `4` | Number of RAG passages returned and spoken |
| `kpi` | Disabled, local-only dashboard settings | Optional bounded recorder, SQLite store, resource sampler, and read-only dashboard |

Language profiles select:

| Profile value | Italian | English |
|---|---|---|
| Vosk model | `vosk-model-small-it-0.22` | `vosk-model-small-en-us-0.15` |
| Piper voice | `it_IT-paola-medium.onnx` | `en_GB-alba-medium.onnx` |
| Ollama model | `emilia-gemma3:1b` | `emilia-en-gemma3:1b` |
| RAG trigger | `regolamento` | `regulation` |
| RAG prefix | `Ecco cosa ho trovato:` | `Here's what I found:` |

All derived paths use `project_root`; launching from another working directory
does not redirect model, corpus, sound, index, or log files.

