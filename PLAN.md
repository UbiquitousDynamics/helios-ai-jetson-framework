# PLAN.md — file-by-file efficiency review and refactoring

Generated from `feature/natural-voice-conversation`. Inventory: **24,586 source lines**
across 51 files, plus **16,197 test lines** across 40 files.

If you are on `main`, these files do not exist and their steps are skipped:
`recognizer/barge_in_detector.py`, `recognizer/echo_suppression_policy.py`,
`audio/backchannel.py`, `audio/speech_pipeline.py`, `api/conversation.py`.
Several other files are substantially smaller there.

**Read `AGENTS.md` before starting.** It carries the constraints, the measured
performance data, and the traps that have already caught people.

## How to work this plan

One step per iteration. Finish, verify, commit, mark done, stop. Do not start the next
step in the same iteration.

For each step:

1. Read the file(s) fully before changing anything.
2. Map every public symbol to its callers, **including `getattr` and string dispatch**.
3. Apply the review procedure from the refactoring prompt.
4. Run `python -m pytest -q`. It must pass.
5. Run `ruff check .`. It must pass.
6. Commit with a message naming the step.
7. Mark the step `[x]` below with a one-line note: lines removed, and what kind of finding.

**Record findings you decided not to act on.** Those are often more informative than the
ones you did.

Goal is less code doing the same work, more clearly. Fewer lines is the expected
*outcome*, never the target. Re-read the anti-goals list if you catch yourself
compressing readable logic to shrink a number.

---

## Phase 0 — Whole-repo mechanical passes

These are cheap, high-yield, and low-risk. They produce the evidence that should
**reorder every phase below**. Do them first and do not skip them.

- [ ] **0.1 — Dead symbol inventory.** Across the whole tree, list every public symbol
      and classify it: (a) truly dead, no references anywhere including tests;
      (b) referenced only by tests; (c) reached only via `getattr`/duck-typed
      compatibility branch; (d) live. Produce `findings/dead-symbols.md`. **Change no
      code in this step.** Only (a) is safe to delete later. Category (c) is the trap —
      `SpeechRecognizer.listen()` looks dead and is not.

- [ ] **0.2 — Duplication scan.** Run a similarity/clone detector across the tree
      (e.g. `ruff`'s rules plus a token-based clone finder, or `pylint --disable=all
      --enable=duplicate-code`). Produce `findings/duplication.md` ranked by size of the
      duplicated block and by how far apart the copies live. Copies in different modules
      matter more than adjacent ones — they diverge silently. **Change no code.**

- [ ] **0.3 — Reorder the plan.** Using 0.1 and 0.2, rewrite the phase order below and
      note what changed and why. If the evidence says the observability cluster is
      clean and the providers are full of duplication, follow the evidence, not this
      document's guess.

---

## Phase 1 — Provider adapters (~3,924 lines)

Three adapters implement the same contract. This is the single most likely place for
real duplication in the repository: the same retry, the same SSE framing, the same
error classification, written three times.

- [ ] **1.1** `api/providers/contracts.py` (270) — establish what the contract actually
      requires before touching any implementation.
- [ ] **1.2** `api/providers/openai_chat_sse.py` (1,487) — part 1: stream parsing and
      SSE framing.
- [ ] **1.3** `api/providers/openai_chat_sse.py` — part 2: request construction, error
      classification, cancellation.
- [ ] **1.4** `api/providers/codex_app_server.py` (1,212) — part 1: transport and session.
- [ ] **1.5** `api/providers/codex_app_server.py` — part 2: streaming and error paths.
- [ ] **1.6** `api/providers/ollama.py` (751).
- [ ] **1.7** **Cross-adapter consolidation.** With all three read, extract what is
      genuinely shared into `contracts.py` or a shared helper. Be careful: three
      similar-looking error classifications may encode three different provider
      behaviours. Confirm before merging.
- [ ] **1.8** `api/provider_factory.py` (59), `api/providers/__init__.py` (84),
      `api/providers/codex_session.py` (61) — small files, one iteration together.

## Phase 2 — Observability cluster (~5,296 lines)

Least on the critical path, most likely to contain repetitive field-mapping that
collapses into a table. Note that `audio_playback_ms` and friends appear in at least six
places across these files — that mapping is a strong table candidate.

- [ ] **2.1** `observability/storage.py` (1,353) — part 1: schema and field mapping.
- [ ] **2.2** `observability/storage.py` — part 2: read/write paths.
- [ ] **2.3** `observability/aggregate.py` (1,278) — part 1: field definitions and mapping.
- [ ] **2.4** `observability/aggregate.py` — part 2: aggregation logic.
- [ ] **2.5** `observability/resources.py` (862).
- [ ] **2.6** `observability/dashboard.py` (803).
- [ ] **2.7** `api/metrics.py` (656).
- [ ] **2.8** `observability/service.py` (274) + `observability/activity.py` (70) +
      `observability/__init__.py`.
- [ ] **2.9** **Cross-cluster consolidation.** The metric field list appears in storage,
      aggregate, dashboard, and metrics. Establish a single source of truth if one is
      justified. Verify every entry is genuinely uniform first — one irregular field
      usually means the table is the wrong shape.

## Phase 3 — API core (~5,939 lines)

Highest risk in the repository. `streaming.py` holds the cancellation and speech-commit
contracts; breaking them causes duplicate spoken audio or lost cancellation. Move slowly.

- [ ] **3.1** `api/_strict_json.py` (23) + `api/speech_chunker.py` (95) +
      `api/privacy.py` (154) — small, one iteration.
- [ ] **3.2** `api/target_compiler.py` (171) + `api/catalog.py` (330).
- [ ] **3.3** `api/health.py` (350).
- [ ] **3.4** `api/conversation.py` (417).
- [ ] **3.5** `api/budget.py` (657).
- [ ] **3.6** `api/routing.py` (664).
- [ ] **3.7** `api/streaming.py` (1,342) — part 1: `CancellationController`,
      `StreamingResponseCoordinator.run`, retry/fallback. **Do not add a retry to the
      post-speech path.**
- [ ] **3.8** `api/streaming.py` — part 2: `_stream_once`, event normalization, speech
      dispatch and flush.
- [ ] **3.9** `api/api_client.py` (1,736) — part 1: construction, routing integration.
- [ ] **3.10** `api/api_client.py` — part 2: `_stream`/`_stream_turn`, lifecycle.
- [ ] **3.11** `api/api_client.py` — part 3: public surface (`talk`, `think`), cleanup.

## Phase 4 — Assistant and configuration (~4,091 lines)

- [ ] **4.1** `config.py` (1,825) — part 1: `LanguageProfile`, language data. Dead keys
      were removed in a prior pass; verify none crept back and check for further
      unreferenced fields.
- [ ] **4.2** `config.py` — part 2: `Settings`, `from_env`, path properties.
- [ ] **4.3** `config.py` — part 3: LLM/routing/KPI settings and module-level constants.
- [ ] **4.4** `assistant.py` (2,266) — part 1: construction, lifecycle, executors.
- [ ] **4.5** `assistant.py` — part 2: `run_once`, wake-word and RAG dispatch.
      **`process_command` is not duplication** — see AGENTS.md before "fixing" it.
- [ ] **4.6** `assistant.py` — part 3: barge-in monitoring and follow-up capture.
- [ ] **4.7** `assistant.py` — part 4: model invocation, metrics, shutdown.
- [ ] **4.8** `main.py` (87).

## Phase 5 — Audio (~1,199 lines)

- [ ] **5.1** `audio/tts.py` (725) — part 1: `SoundDeviceBackend`, synthesis.
- [ ] **5.2** `audio/tts.py` — part 2: playback, interrupt/duck/resume, preloading.
- [ ] **5.3** `audio/speech_pipeline.py` (233).
- [ ] **5.4** `audio/backchannel.py` (139) + `audio/sound_player.py` (70) +
      `audio/playback.py` (32, deprecated shim — deprecate further, do not delete
      without evidence nothing external imports it).

## Phase 6 — Recognizer (~1,014 lines)

Do **not** micro-optimize the frame loop. `pcm16_rms` is 0.06% of the frame budget;
see AGENTS.md.

- [ ] **6.1** `recognizer/speech_recognizer.py` (543).
- [ ] **6.2** `recognizer/barge_in_detector.py` (380) +
      `recognizer/echo_suppression_policy.py` (91).

## Phase 7 — Document retrieval and connectivity (~1,511 lines)

- [ ] **7.1** `document/rag_system.py` (688) — part 1: corpus loading, chunking,
      fingerprinting.
- [ ] **7.2** `document/rag_system.py` — part 2: embedding, indexing, retrieval.
      Ranking is BLAS and already fast; do not rewrite it.
- [ ] **7.3** `api/connectivity.py` (823).

## Phase 8 — Scripts (~1,175 lines)

Operator tooling. Lower risk, and often where copy-paste accumulates.

- [ ] **8.1** `scripts/benchmark_kpi.py` (403) + `scripts/kpi.py` (323).
- [ ] **8.2** `scripts/doctor.py` (389).
- [ ] **8.3** `scripts/run_jetson.py` (133) + `scripts/codex_subscription.py` (127).
- [ ] **8.4** `scripts/build_index.py` (60) + `scripts/network_diagnostics.py` (49) +
      `scripts/smoke_tts.py` (40).

## Phase 9 — Test suite (16,197 lines) — RESTRICTED SCOPE

**Read this section twice before starting it.**

The test suite is the reason this codebase can be refactored safely. It is an asset, not
overhead. Its size is not a problem to be solved.

**Permitted in this phase, and nothing else:**

- Consolidating duplicated *fixtures and helpers* into `conftest.py`, where several test
  files build the same fake by hand.
- Removing helpers that are genuinely unreferenced after Phase 0.1.
- Fixing a test that asserts wrong behaviour — but only by reporting it first and
  getting a human decision, never silently.

**Forbidden:**

- Deleting a test, merging distinct cases, parameterizing away meaningful cases, or
  loosening any assertion.
- Reducing coverage of any behaviour, however redundant it looks.
- Touching a test to make a source refactor pass. If a test fails, the refactor is wrong
  until proven otherwise.

Steps:

- [ ] **9.1** Inventory duplicated fake/fixture construction across all test files.
      Produce `findings/test-fixtures.md`. **Change no code.**
- [ ] **9.2** Consolidate the fakes with the most copies into `conftest.py`. Test count
      must not change. Record the count before and after.
- [ ] **9.3** Second consolidation pass if 9.1 justifies it. Otherwise mark skipped.

## Phase 10 — Report

- [ ] **10.1** Write `findings/REPORT.md`: net line delta per phase; findings by category
      (dead code, duplication, redundant abstraction, repetitive structure, algorithmic);
      **findings deliberately not acted on, with reasons**; any behaviour change found and
      escalated; test count before and after (these must match).

      Present the line delta as a result, not an achievement. If the honest outcome is
      that the codebase is already reasonably tight and only a few hundred lines come
      out, say that. Inventing work to hit a number is the failure mode this plan is most
      exposed to.

---

## Stop conditions

Stop and hand back to a human if any of these occur:

- The test suite fails and the cause is not obvious within one iteration.
- A refactor would change observable behaviour.
- You find a bug. Report it; do not fix it inside a refactor.
- A file's responsibility cannot be stated in one sentence — that is a design finding
  and needs a human decision, not a cleanup.
- You are about to delete something in category (b) or (c) from Phase 0.1.
