# A2 capture-level calibration — 2026-09-24

## Decision

Set the capture-health warning floor to **0.001 normalized PCM16 RMS** through `Settings.audio_capture_level_min_rms` / `HELIOS_AUDIO_CAPTURE_LEVEL_MIN_RMS`. Retain **`mono`** as the default and selected channel mode. This is calibration of the dead-input diagnostic against measured quiet input; it does not alter speech, endpointing, or barge-in detection. The previous warning floor was `barge_in_minimum_interrupt_energy * 0.1 = 0.006`, which overlapped the earlier 0.0032–0.0059 quiet-room measurements. The dead onboard input measured exactly 0.000000. The lowest quiet median in this A2 run was 0.002329; 0.001 lies below it while remaining above zero. No detection threshold was lowered.

## Method and target state

Emilia boot ID `8c01da32-a045-45e9-94b5-300373784a83`; `emilia.service` disabled/inactive. The A1 USB source and USB sink were identified by their full PulseAudio names. No loopback module, source output, or sink input was present before calibration. Piper generated one 1.939 s Italian fixture locally. Five paired trials alternated `mono` then `stronger`, using the same fixture, sink, microphone, gain and source volume. Each trial opened one `SpeechRecognizer` capture stream in strict PulseAudio mode for about 4.8 s. An empty recognizer returned no transcript. Playback began after ten captured 100 ms frames; the first eight frames supplied the quiet window and frames 10–35 the stimulus window. An RMS of 0.4 or greater would abort capture. Playback had a 5 s timeout, and the whole calibration had a 100 s timeout. All ten playbacks exited 0, no abort occurred, and each run returned at least 48 frames. Only scalar metrics were printed; no captured PCM or transcript was saved. The generated WAV was deleted after the run and streams were closed.

RMS values below are unitless, normalized to full-scale PCM16. `quiet p95` is the nearest-rank 95th percentile of eight pre-playback frames; `stimulus p95` is the same statistic for frames 10–35. The ratio divides those two values. It is descriptive, not an acceptance threshold.

| Trial | Mode | Frames | Quiet median | Quiet p95 | Stimulus p95 | Peak RMS | p95 ratio |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | mono | 49 | 0.005345 | 0.006424 | 0.023024 | 0.036320 | 3.584 |
| 1 | stronger | 49 | 0.006032 | 0.012903 | 0.025033 | 0.039536 | 1.940 |
| 2 | mono | 50 | 0.003255 | 0.003913 | 0.025626 | 0.035612 | 6.550 |
| 2 | stronger | 51 | 0.003961 | 0.005775 | 0.030408 | 0.039428 | 5.265 |
| 3 | mono | 55 | 0.002521 | 0.009130 | 0.020395 | 0.041518 | 2.234 |
| 3 | stronger | 49 | 0.002329 | 0.003603 | 0.069251 | 0.110528 | 19.219 |
| 4 | mono | 48 | 0.002577 | 0.002906 | 0.082675 | 0.096336 | 28.453 |
| 4 | stronger | 50 | 0.003032 | 0.003826 | 0.085002 | 0.122802 | 22.219 |
| 5 | mono | 49 | 0.002925 | 0.006645 | 0.075694 | 0.114222 | 11.391 |
| 5 | stronger | 51 | 0.003228 | 0.003487 | 0.088415 | 0.104639 | 25.356 |

Across five trials per mode, median quiet RMS was 0.002925 for `mono` and 0.003228 for `stronger`. The median stimulus p95 was 0.025626 for `mono` and 0.069251 for `stronger`, but `stronger` had a higher quiet p95 in three of five paired trials and a higher stimulus-to-quiet p95 ratio in only two. Physical/acoustic variation was substantial across trials, so these measurements do not establish a reliable signal-to-noise benefit for `stronger`. Retaining `mono` preserves the current channel path. This does not rule out a later, separately measured stereo calibration.

## Code, tests, and remaining device configuration

`config.py` now validates a positive finite capture-health floor, default 0.001; `assistant.py` passes it to the existing recognizer parameter instead of deriving it from barge-in energy. `README.md` documents the setting. Focused tests cover default, override, invalid and boundary values, integration into `VoiceAssistant`, and warning behavior at zero and either side of 0.001. Syntax parsing of all five changed Python files and Ruff passed. In a dedicated A2 validation tree on Emilia, focused command `PYTHONDONTWRITEBYTECODE=1 /home/emilia/helios-ai-jetson-framework/venv/bin/python -m pytest -q tests/test_config_llm.py tests/test_recognizer.py tests/test_assistant.py -p no:cacheprovider -m 'not remote_live'` passed **195**. Full command `PYTHONDONTWRITEBYTECODE=1 timeout 600 /home/emilia/helios-ai-jetson-framework/venv/bin/python -m pytest -q -m 'not remote_live' -p no:cacheprovider` passed **2097**, 1 remote test deselected. Both exited 0. No provider was called.

During calibration the service drop-in was unchanged; its SHA-256 was `5953060b1dad12ae4b12d2531ec97b5ed754f22b068c4224615d29d2405d2f81`. The service stayed disabled/inactive; no reboot or service start occurred. The service currently points to the older development checkout, so the new environment binding is not yet active for that launcher. The dedicated A2 validation tree is not the service deployment.

### Separately approved service configuration staging

The selected values can be pinned in the existing drop-in with these two lines, leaving its A1 source and strict settings intact:

```ini
Environment="HELIOS_AUDIO_CAPTURE_LEVEL_MIN_RMS=0.001"
Environment="HELIOS_AUDIO_INPUT_CHANNEL_MODE=mono"
```

The operator separately approved backing up the drop-in, appending **only** these lines, reloading systemd, and read-only verification. `/etc/systemd/system/emilia.service.d/30-capture-source.conf.a2-before` did not exist before the backup; `cmp` confirmed an exact copy. The append and `systemctl daemon-reload` both exited 0. The complete diff from the backup is the two added lines above. SHA-256: backup `5953060b1dad12ae4b12d2531ec97b5ed754f22b068c4224615d29d2405d2f81`; updated drop-in `bebe2720a9c0062fb51171a4fac3a38075d00a9019610b0641516eb4d7ce86ad`. `systemctl show emilia.service -p Environment --value` reported the original USB source and strict=true plus `HELIOS_AUDIO_CAPTURE_LEVEL_MIN_RMS=0.001` and `HELIOS_AUDIO_INPUT_CHANNEL_MODE=mono`. The service remained `disabled/inactive`. Rollback: `sudo cp -a /etc/systemd/system/emilia.service.d/30-capture-source.conf.a2-before /etc/systemd/system/emilia.service.d/30-capture-source.conf` followed by `sudo systemctl daemon-reload`. No reboot, service start/enable, live-code deployment, or provider request occurred. **The older service checkout does not consume the new threshold binding until compatible code is separately deployed and verified.**
