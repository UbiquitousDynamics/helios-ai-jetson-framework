# Jetson barge-in acceptance preregistration

Frozen before any timed microbenchmark or acoustic A/B result is collected. Environment
inventory may be collected first because it determines which paths and hardware exist.

## Test revision and operating point

- Deployment target: `feature/natural-voice-conversation` at
  `b86781c7c424ce3a9972198b3a8f0470025461f1`.
- Timed tests use one fixed `nvpmodel` profile, `jetson_clocks`, and one pinned online CPU.
  The selected values will be recorded before results are collected.
- A test path must keep the existing injectable Python fallback available. No application
  source or C++ implementation is part of this audit.

## Predeclared pass/fail thresholds

1. **Speech-onset-to-playback-stop latency.** Boundary: the timestamp of the first
   16 kHz capture sample belonging to scripted near-end speech, after fixed acoustic/digital
   alignment, through the timestamp of the last non-silent sample submitted to the physical
   playback device. Across 60 interruptions (20 each at near-end-to-echo ratios -6, 0, and
   +6 dB), pass at p95 <= 250 ms, p99 <= 300 ms, maximum <= 350 ms, with no trial counted as
   stopped unless playback remains stopped for at least 500 ms. These limits make the
   repository's 200--300 ms design aim testable while allowing one 100 ms capture block and
   one output buffer of scheduling margin.

2. **Interruption recall.** Boundary: the same 60 scripted, speech-present interruption
   trials, from onset until 1 second after onset. Pass at >= 95% overall (at least 57/60)
   and >= 90% in every level stratum (at least 18/20). A detection after 1 second is a miss.

3. **Self-trigger / false-barge-in rate.** Boundary: assistant-render/echo-only playback
   with no near-end speech, beginning after the path's documented convergence interval.
   Run at least 100 distinct assistant utterance trials and at least 30 continuous minutes.
   Pass with <= 1 false stop per 100 utterances and <= 0.5 false stops/hour. Both normalized
   rates and exact counts/durations are reported; a run shorter than two hours cannot by
   itself demonstrate the hourly threshold with useful zero-event exposure and is marked
   inconclusive rather than silently extrapolated.

4. **Echo attenuation (ERLE).** Boundary: aligned far-end-only voiced windows after the
   first 500 ms of convergence, `10*log10(P_mic_echo/P_residual)`, excluding silence below
   -45 dBFS and clipped windows. Across at least 20 clips, pass with median ERLE >= 15 dB
   and 10th-percentile ERLE >= 10 dB. Raw and residual RMS levels are also reported so a
   muted output cannot masquerade as cancellation.

5. **Double-talk preservation.** Boundary: aligned near-end-active windows in simultaneous
   near/far playback, compared with the clean near-end reference after gain and delay
   alignment. Pass with median STOI >= 0.85, 10th-percentile STOI >= 0.75, and median
   near-end level loss <= 3 dB, in addition to the recall threshold above. If the target has
   no independently addressable near-end transducer or calibrated injection/loopback path,
   physical double-talk preservation is explicitly unmeasurable; digitally mixed testing is
   labeled algorithmic-only and does not clear this criterion.

6. **Xruns/underruns.** Boundary: ALSA/PortAudio/audio-server counters and application logs
   from start through shutdown of each complete A/B run. Pass with zero capture overruns,
   playback underruns, or xruns during the 60 interruption trials plus the false-trigger
   exposure. Any counter that the active stack does not expose is marked unavailable.

7. **Per-frame compute.** All distributions use monotonic `perf_counter_ns`, deterministic
   inputs, warmup, at least five repetitions, nearest-rank p50/p99/max, and the same pinned
   operating point. For 100 ms capture-frame work (RMS and Vosk parsing), pass at p99 <= 5 ms,
   max <= 10 ms, and zero >= 100 ms. For 20 ms AEC/VAD work, pass at p99 <= 2 ms, max <= 5 ms,
   and zero >= 20 ms. For RAG dot-plus-top-20, which is turn-level rather than frame-periodic,
   use a declared 100 ms interactive-overhead allocation: p99 <= 10 ms and max <= 20 ms.
   Scalar and block NLMS are reported against the 20 ms frame budget even when not candidates
   for deployment.

## Path decision rule

Paths are tested in order: current RMS + Vosk soft gate; system PipeWire/PulseAudio WebRTC
AEC; `pywebrtc-audio==0.1.0`; SpeexDSP only if all preceding available paths fail. A path
clears the acoustic acceptance target only if every measurable criterion above passes.
Unavailable hardware, unavailable packages/wheels, or missing observability produces an
explicit `INCONCLUSIVE/UNAVAILABLE`, never a substituted workstation estimate.
