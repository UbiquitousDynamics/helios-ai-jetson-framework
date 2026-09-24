# Development and testing


### Automated coverage

The default suite is model-free and network-free. It does not require Ollama,
microphone access, Piper, Vosk, Torch, or the bundled neural models.

Covered behaviors include:

- whole-word wake detection;
- partial versus finalized recognition routing;
- `COMMAND`/`RAG` state transitions;
- configured RAG `top_k`;
- idempotent service shutdown;
- profile-specific shared TTS injection;
- Ollama host normalization and lazy construction;
- SDK `done`/`done_reason` stream parsing;
- retry success, exhaustion, and no-replay behavior;
- preservation of TTS failures;
- normalized Ollama and OpenAI-compatible SSE adapters;
- isolated Codex app-server authentication, streaming, timeout, and teardown;
- deterministic routing, privacy authorization, cooldowns, catalog freshness,
  durable budget limits, and content-free metrics;
- passive/active connectivity admission, freshness, route-change notification,
  quality smoothing, and hysteresis;
- adaptive Luna/Terra/Sol tier selection and direct local fallback;
- fallback before speech and the global no-replay rule after speech;
- PCM-frame playback without WAV-header corruption;
- lazy and bounded `aplay` execution;
- microphone stream cleanup and PyAudio termination;
- legacy RAG index rejection;
- the historical 1,116-row/1,115-chunk mismatch;
- corpus and model content fingerprints;
- vector normalization, finite-value checks, and stable ranking;
- asset paths, companion files, checksums, and manifest safety;
- KPI configuration validation and fail-disabled security defaults;
- closed-schema sanitization and proof that conversation content and identifiers
  are not persisted;
- bounded queue overflow, asynchronous batching, final flush, and sink failure;
- SQLite migration, retention, size enforcement, concurrent reads/writes,
  aggregates, percentile correctness, and empty-store behavior;
- strict dashboard API validation, Basic/Bearer access control, exports, and static
  assets;
- machine-readable KPI benchmark schema and invariants without timing thresholds;
- mocked `tegrastats`, unavailable resource sources, and clean sampler shutdown
  on cross-platform test hosts.

Install development dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Run the complete local quality suite:

```bash
python -m ruff check .
python -m ruff format --check .
python -m compileall -q main.py assistant.py config.py api audio document observability recognizer scripts tests
python -m pytest
python scripts/doctor.py --assets-only --check-hashes
```

The only test allowed to contact a remote service is marked `remote_live` and
skips unless both opt-in variables are present. It additionally requires a
reviewed `remote_only` configuration:

```bash
export HELIOS_LLM_LIVE=1
export HELIOS_LLM_LIVE_CONFIG=/etc/helios/llm-routing-live.toml
python -m pytest tests/test_live_llm.py -m remote_live -q
```

Normal `pytest` and CI runs remain network-free. For Codex, a convenient
certification procedure using a temporary copy of the committed profile is
documented in [Codex via ChatGPT subscription](docs/CODEX_SUBSCRIPTION.md).

### CI

`.github/workflows/quality.yml` runs on pushes, pull requests, and manual
dispatches:

- Ubuntu and Windows;
- Python 3.10 and 3.12;
- Ruff linting;
- Ruff format verification;
- bytecode compilation;
- all model-free tests, including KPI storage, resource, and local HTTP tests;
- one asset hash-validation job.

The workflow installs `requirements-dev.txt`, not the hardware runtime stack.
Passing CI validates code and repository assets but does not prove that a
specific Jetson audio/inference image is correctly provisioned.

### Suggested development workflow

1. Create a feature branch from the latest `main`.
2. Install `requirements-dev.txt`.
3. Add or update model-free regression tests before changing behavior.
4. Make the smallest cohesive source change.
5. Run Ruff, compilation, Pytest, and the asset doctor.
6. If `uploads/*.txt` or the embedding model changed, rebuild
   `embeddings.npz`.
7. Run `scripts/smoke_tts.py` and a target-device `main.py` smoke test.
8. Inspect `git diff` and keep logs, caches, generated indexes, and environments
   untracked.
9. Commit source, tests, and documentation together when they describe one
   behavior.

## Performance considerations

### Confirmed improvements

- The corpus and compressed embedding matrix are loaded once and cached.
- Every query is encoded once.
- Already normalized encoder output is reused without another division/copy.
- RAG top-k selection partitions the similarity vector in linear time and
  deterministically sorts only the selected candidates.
- RAG model and existing-index loading overlap the interval between the RAG
  trigger and the following spoken question; missing indexes are never built
  by this background preparation.
- No startup RAG query is executed and discarded.
- No constructor sends an Ollama warm-up request by default.
- Eligible Codex startup/account validation overlaps the welcome message and
  never starts an inference turn.
- Vosk and PyAudio prepare concurrently with the greeting without opening the
  microphone stream; the Ollama client, Piper weights, and RAG remain lazy at
  their relevant boundary.
- One Piper object is shared between direct responses and streamed chat.
- Synthesis stays in memory and does not repeatedly write a fixed WAV file.
- TTS fragments reuse one blocking raw PortAudio output stream while the PCM
  format remains unchanged, avoiding per-sentence device reconstruction and
  NumPy conversion.
- Recognition returns on the first finalized phrase instead of always waiting
  the full timeout.
- Remote deltas are spoken at sentence boundaries before completion; long
  unpunctuated output uses a configurable soft whitespace boundary.
- The legacy content-free LLM JSONL sink remains available for compatibility.
  The optional broader KPI store uses a bounded non-blocking queue, asynchronous
  SQLite batches, rollups, and shutdown flushing, keeping persistence and
  retention work out of the conversational return path.
- Voice requests read cached network quality and perform only a fast passive
  kernel check instead of waiting for an active probe.
- Notification cues reuse one bounded worker instead of creating a process per
  state change.
- Index writes are atomic, and valid data is not repeatedly decompressed.

### Current algorithmic choices

- Corpus encoding uses configurable batches, defaulting to 16.
- Embeddings and queries are explicitly L2-normalized.
- Search uses an in-memory NumPy dot product.
- Stable full ranking is used instead of partial or approximate ranking.
- Model identity hashes relevant model/tokenizer content once when RAG is
  created.
- Remote selection uses local integer scoring and adds no classifier request,
  network latency, token cost, or additional transcript exposure.

With approximately 1,115 chunks, the full ranking cost is small and the simpler
algorithm improves determinism. An ANN database should be considered only after
the corpus grows enough for profiling to show a material bottleneck.

### Optimizations that still require target profiling

- CPU versus GPU placement for SentenceTransformers;
- batch-size changes on a specific Jetson memory budget;
- overlap between LLM generation and audio playback;
- audio device latency and buffer tuning;
- alternative embedding models or chunking strategies.
- Codex model-tier floors, first-visible-token limits, and speech-fragment size;
- connectivity thresholds against real Wi-Fi/cellular p50 and p95 data.

The repository does not claim a Jetson speedup for these changes without
target-device measurements.

