# AGENTS.md

Context for agents working in this repository. Read this before proposing changes.

## What this project is

A Python voice assistant that runs on Jetson hardware: Vosk speech recognition, an LLM
turn (local Ollama or an optional remote provider), Piper text-to-speech, and a local
extractive RAG mode.

It is **not** a home-automation controller. There is no Home Assistant integration, no
automation executor, no GPIO/MQTT layer, and no device-action parser. "Turn on the
living-room light" is treated as a natural-language prompt for the LLM and will not
switch on a light. Do not add an actuation layer without an explicit request.

Tool and function calls are explicitly rejected by the streaming layer.

## Branches differ materially — check which one you are on

- `main` — pre-barge-in baseline. No `recognizer/barge_in_detector.py`, no
  `recognizer/echo_suppression_policy.py`, no `audio/backchannel.py`, no
  `api/conversation.py`. `recognizer/speech_recognizer.py` is 262 lines and computes no
  frame energy.
- `feature/natural-voice-conversation` — adds barge-in: concurrent capture during
  playback, echo suppression, interruption, follow-up capture. Recognizer 524 lines,
  TTS 666 lines.

Run `git log` and `git status` rather than trusting any description of current state,
including this one.

## Development is on Windows; the deployment target is Jetson (Linux)

Do not assume the development machine resembles the target. Anything measured on the
Windows dev box is provisional for Jetson. In particular, `audio/sound_player.py` shells
out to Linux `aplay`, which does not exist on Windows — the tests cover it with fakes,
but a change that depends on running it will only fail on the device.

## The test suite is the most valuable thing here — protect it

Roughly 600 tests run with **no microphone, no audio device, no Vosk model, no Piper
voice, and no Ollama**. This is deliberate and it is what makes the codebase safe to
refactor at all.

- Run `python -m pytest -q` after every change. Do not assume; run it.
- Never delete tests, merge distinct cases, or loosen assertions to make a change pass.
- If a refactor is not covered by an existing test, add the test first, confirm it passes
  against the current code, then refactor.
- Keep new components injectable and fakeable, matching the dependency-injection style
  already used throughout.

`tests/test_live_llm.py` is skipped unless `HELIOS_LLM_LIVE=1`. That is expected.

## Lint and format baseline

- `ruff check .` passes. Keep it passing.
- `ruff format --check .` **already fails on 6 files** before any of your changes,
  apparently due to a ruff version difference (it proposes lines longer than the
  configured `line-length = 100`). Establish this baseline yourself before touching
  anything, so you do not mistake it for damage you caused.

## Privacy: remote routing is opt-in and must stay that way

Files under `examples/` are documentation, **not** active configuration. Remote routing
requires `HELIOS_LLM_CONFIG` to name a routing file explicitly. A clean checkout must
stay on the `local_only` defaults.

Every field of `LLMPrivacySettings` fails closed. Do not "helpfully" default any of them
to true. This is an in-vehicle voice product; transcript egress is the most consequential
setting in it.

## Performance: the hot paths have already been measured

Do not micro-optimize the audio loop. Measured on x86:

| Path | Cost | Share of frame budget |
|---|---|---|
| `pcm16_rms`, pure Python, 4,000 samples | 0.159 ms | 0.06% of 250 ms |
| `json.loads` of a Vosk partial | 0.0008 ms | negligible |
| `_parse_recognition`, final + word metadata | 0.009 ms | negligible |
| `np.dot` ranking, 5,000 chunks x 384 dims | 0.288 ms | per query |

The heavy computation is already native: Vosk is Kaldi, Piper is ONNX Runtime, ranking is
BLAS, Ollama is llama.cpp. The Python layer is orchestration glue. **In this codebase
"efficiency" almost always means code economy, not CPU time.**

Any performance claim must come with a before and after measurement. An unmeasured
performance claim is treated as false.

## Traps that have already caught people

- **Duck-typed compatibility branches look like dead code.** `SpeechRecognizer.listen()`
  appears unused but is reached via `getattr` from `_recognize_once_unobserved()` for
  injected recognizers lacking `listen_once`. Before deleting any symbol, search for
  `getattr` access and string-based dispatch, not just direct calls.
- **`process_command()` is not duplicated logic.** It and `run_once()` both call the same
  shared primitives, so the wake-word rules are defined once. `run_once` cannot simply
  delegate to it: it must distinguish a wake-word activation from a barge-in follow-up
  and record different metrics for each.
- **`listen_once()` has an explicit contract**: it returns a `RecognitionResult` carrying
  `is_final`.
- **Speech failures must not be retried.** Once a fragment has been spoken, provider
  replay and fallback are disabled to avoid duplicate audio. Do not "improve" the error
  path by adding a retry there.
- **Cancellation arrives as `ProviderError(CANCELLED)`**, so it is already covered by
  `except ProviderError`. Do not widen exception handlers to `BaseException` without
  checking what that removes — an earlier attempt silently dropped the wrapping of
  unexpected errors into `ProviderError(UNKNOWN)`.

## Working style

- One coherent change at a time. Run the tests. Then continue. Do not batch unrelated
  refactors into a single commit.
- **Do not smuggle bug fixes inside refactors.** If you find a bug while refactoring,
  report it separately and let a human decide. A behaviour change hidden in a cleanup
  diff is very hard to review.
- If something cannot be verified, mark it unverified rather than reasoning around the
  gap.
- Report findings you decided *not* to act on. Those are often more informative than the
  ones you did.
