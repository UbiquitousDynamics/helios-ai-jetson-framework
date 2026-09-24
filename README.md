<p align="center">
  <img src="pictures/heliosAI.png" alt="Helios AI logo" width="360">
</p>

<h1 align="center">Helios AI</h1>

<p align="center">
  <strong>Offline-first voice assistant and adaptive LLM framework for NVIDIA Jetson</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%20%E2%80%93%203.12-blue" alt="Python 3.10-3.12">
  <img src="https://img.shields.io/badge/platform-NVIDIA%20Jetson%20%7C%20Linux-76B900" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/tests-2098%20passing-brightgreen" alt="Tests">
  <img src="https://img.shields.io/badge/status-developer%20framework-orange" alt="Status">
</p>

---

Helios AI turns an edge computer into a hands-free, voice-driven assistant. Speak to it, ask a
language model a question, or search a bundled knowledge base — without a keyboard, a display,
or a permanently available Internet connection.

Speech recognition, text-to-speech, semantic search, and a language model all run **on the
device by default**. Remote inference is opt-in, gated, and fails closed.

> **Scope.** This is a developer-oriented framework, not a packaged consumer product. It
> implements the complete voice pipeline and the knowledge files used by the Emilia
> deployment. It does not implement vehicle control, telemetry, navigation, battery
> management, or GPIO integration.

## Why it is different

Remote inference is not an on/off switch. Before a transcript can leave the device, the runtime
evaluates privacy policy, network reachability and measured quality, provider health, model
capability, and optional cost limits — then selects a remote model sized to an explainable
complexity score. If any gate fails, the request stays local. Generated text is spoken sentence
by sentence as it streams, so the first audio arrives long before the answer is complete.

## Capabilities

| Area | What is implemented |
| --- | --- |
| **Speech recognition** | Offline Italian and English via bundled Vosk models; structured partial/final events; whole-word wake and trigger detection |
| **Conversation** | Typed floor-state controller, adaptive turn endpointing, full-duplex capture, barge-in with conservative echo suppression, local control intents |
| **Language models** | Local streaming through Ollama; optional OpenAI-compatible SSE; optional Codex app-server via ChatGPT sign-in |
| **Routing** | Deterministic policies plus an explainable adaptive remote cascade, with a Linux route/carrier/IP gate and background HTTPS quality measurement |
| **Safety gates** | Privacy, provider health, cost, timeout, and audio no-replay controls — all fail-closed |
| **Speech synthesis** | Offline Piper voices as ONNX; one shared instance; in-memory WAV playback with no temporary file |
| **Retrieval** | Source-aware semantic search over an atomic vector index bound to its corpus and embedding model |
| **Observability** | Content-free KPI store with an optional read-only dashboard |

## Interaction model

Two user flows, both hands-free:

**Conversational command** — say `emilia` (or `amelia`, `hello`) as a whole word. The wake word
is stripped before inference; the configured route picks local or remote; the answer is
synthesized locally as it streams.

**Knowledge-base query** — say `regolamento` (`regulation` in English) to enter retrieval mode,
then ask your question in the next utterance. Helios embeds it, searches the local index, and
speaks the selected passages.

## Requirements

| | Minimum |
| --- | --- |
| Hardware | NVIDIA Jetson (Nano or newer) or an x86-64 Linux host |
| OS | Linux with ALSA; PulseAudio supported |
| Python | 3.10 – 3.12 |
| Memory | 4 GB RAM (8 GB recommended when running local inference and retrieval together) |
| Audio | A working capture device and a playback device |
| Local LLM | [Ollama](https://ollama.com/) reachable on the host |

## Quickstart

```bash
git clone https://github.com/UbiquitousDynamics/helios-ai-jetson-framework.git
cd helios-ai-jetson-framework

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# verify models, assets, and native runtime before the first run
python -m scripts.doctor

python main.py
```

`scripts.doctor` reports missing models, checksum mismatches, and native import problems before
they surface as runtime failures. Run it first on any new host.

Full steps, including Ollama model setup: **[docs/INSTALLATION.md](docs/INSTALLATION.md)**.

## Configuration essentials

Helios is configured through environment variables. It starts **local-only** unless a routing
file is named explicitly — cloning the repository and running it never sends a transcript off
the device by accident.

| Variable | Purpose |
| --- | --- |
| `HELIOS_LANGUAGE` | Active language profile (`it`, `en`) |
| `HELIOS_LLM_CONFIG` | Path to a routing file. **Unset means local-only.** |
| `HELIOS_AUDIO_INPUT_DEVICE` | Capture device: a name, an index, or `pulse:<source>` |
| `HELIOS_AUDIO_INPUT_STRICT` | Fail closed when the requested device is unavailable |
| `HELIOS_KPI_ENABLED` | Enable the content-free KPI store |
| `HELIOS_LOG_LEVEL`, `HELIOS_LOG_FILE` | Logging verbosity and destination |

Every run logs its own identity at startup — tree, commit, routing file, KPI store, log
destination — so a deployment can always say what it is:

```
event=helios_run_identity tree=… commit=… dirty=… routing_config=… kpi_enabled=… log_destination=…
```

Complete reference: **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**.

### Selecting the microphone

On hosts where ALSA and PulseAudio disagree about the default device, name the PulseAudio
source explicitly rather than relying on `default`:

```bash
HELIOS_AUDIO_INPUT_DEVICE=pulse:alsa_input.usb-Example_Audio-00.analog-stereo \
HELIOS_AUDIO_INPUT_STRICT=true \
python main.py
```

Each opened stream logs the resolved identity — device, channels, rate, downmix, PulseAudio
source, and active port — so a silent or misrouted microphone is visible in the log instead of
being mistaken for a recognition problem.

## Documentation

| Document | Contents |
| --- | --- |
| [Architecture](docs/ARCHITECTURE.md) | Design, component interaction, hybrid inference, runtime workflows, repository layout |
| [Installation](docs/INSTALLATION.md) | Prerequisites, install steps, Ollama model setup |
| [Configuration](docs/CONFIGURATION.md) | All settings, remote inference setup, active configuration |
| [Operations](docs/OPERATIONS.md) | Running the assistant, KPI dashboard, logging, troubleshooting, FAQ |
| [Knowledge base](docs/KNOWLEDGE_BASE.md) | Corpus ingestion, embeddings, asset validation and provenance |
| [Development](docs/DEVELOPMENT.md) | Testing, contribution workflow, performance considerations |

**Design notes:** [hybrid LLM operations](docs/HYBRID_LLM_OPERATIONS.md) ·
[adaptive remote routing](docs/ADAPTIVE_REMOTE_ROUTING.md) ·
[network connectivity routing](docs/NETWORK_CONNECTIVITY_ROUTING.md) ·
[barge-in](docs/BARGE_IN_DESIGN.md) · [remote context](docs/REMOTE_CONTEXT_DESIGN.md) ·
[Codex subscription](docs/CODEX_SUBSCRIPTION.md) · [KPI observability](docs/KPI_OBSERVABILITY.md)

## Project status

The realtime conversation layer is implemented and covered by an automated suite
(**2,098 tests passing** on the reference Jetson target). Acoustic hardware-in-the-loop
validation is **in progress** and is tracked in
[docs/voice-test-suite-progress.md](docs/voice-test-suite-progress.md).

Deployment-specific audio, latency, thermal, and power characteristics are **not certified** by
this repository. They require calibration on the target hardware.

## Known limitations

These are current, deliberate, and documented rather than hidden.

**Packaging and scope**
- Runs from a source checkout; not distributed as a wheel, container, or appliance image.
- No spoken shutdown command.
- Notification cues rely on Linux ALSA `aplay`.

**Conversation and session**
- One in-process logical session; no multi-user conversation API.
- Conversation history is not durable across process restarts. Codex threads are ephemeral;
  interruption, fallback, and rotation recover from bounded logical history while Helios runs.

**Audio**
- Barge-in thresholds and the 200–300 ms interruption target require calibration on the
  deployed microphone, speaker, enclosure, and levels. Hardware AEC is not assumed.
- Documented first-audio figures use a fake timed stream, not measured Jetson/Piper hardware.
- Microphone and speaker selection is configured by environment variable, not by CLI.

**Retrieval**
- Only top-level UTF-8 `.txt` files are ingested.
- Retrieval is extractive; it does not generate a source-cited answer through Ollama.
- The first index build can be expensive on constrained hardware.
- Changing the embedding model or splitter needs a language-specific gold-question set first.

**Remote inference**
- Limited to strict OpenAI-compatible Chat Completions SSE and the native Codex app-server.
  Other semantics require a separately tested adapter.
- The network-quality gate depends on Linux interface and kernel route/sysfs behavior;
  non-Linux behavior is not certified.
- The network score is an explainable heuristic, not a learned optimum; its thresholds need
  target-network calibration.
- Provider accounts, catalogs, legal and privacy approval, and connectivity signals are
  deployment responsibilities.

**Verification and provenance**
- CI does not exercise real microphones, audio outputs, Ollama, or neural-model inference.
- Some voice, Vosk, corpus, sound, and image provenance metadata remains incomplete.
- Existing large binary history has not been migrated to Git LFS.

## Project background

Helios AI was developed with [Onda Solare](https://ondasolare.com/), the Italian solar-vehicle
team, and installed on **Emilia 5.9** in connection with the team's participation in the 2025
Bridgestone World Solar Challenge in Australia.

<p align="center">
  <img src="pictures/emilia5.9.bmp" alt="Onda Solare's Emilia 5.9 solar vehicle" width="900">
</p>

Project video: [Surfin' the wave — Emilia 5.9](https://www.youtube.com/watch?v=8vY06AmO5Fg)

## License

Released under the [MIT License](LICENSE), copyright 2025 Ubiquitous Dynamics.

The repository also contains third-party model and content assets. The project license does not
relicense them. Review [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), the bundled model
cards, and the applicable upstream terms before redistribution or commercial deployment.
