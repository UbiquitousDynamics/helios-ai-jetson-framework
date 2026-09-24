# A1 device change record — approved commands applied

The operator first approved only the two backups, the `30-capture-source.conf` drop-in, `systemctl daemon-reload`, `alsactl store 2`, and read-only verification. All five actions completed with exit status 0. The operator separately approved reboot and A1 persistence verification; those are recorded below. Service start/enable, Codex routing changes, and provider requests were not authorized or performed.

Read-only inspection on Emilia, 2026-09-24: `emilia.service` is disabled and inactive. Its effective environment has no audio input selector or strict setting. The USB PulseAudio source is present and selected as default, its profile is `output:analog-stereo+input:analog-stereo`, and its source volume is 100%. `amixer -c 2 sget Mic` reports capture 496/496 (+31 dB). PulseAudio has `module-card-restore`, `module-device-restore`, and persisted card/device databases. ALSA runs `alsa-restore.service`, but `/var/lib/alsa/asound.state` stores USB `Mic Capture Volume` **464/496**, dated 2026-09-03. Thus the current gain is not in the boot restore state.

## Approved change (applied)

First retain the existing service and ALSA state, then add a service drop-in. The drop-in is needed even though the USB source is PulseAudio's default, because this host's PortAudio default path was measured to capture silence.

```sh
sudo cp -a /etc/systemd/system/emilia.service /etc/systemd/system/emilia.service.a1-before
sudo cp -a /var/lib/alsa/asound.state /var/lib/alsa/asound.state.a1-before
sudo install -d -m 755 /etc/systemd/system/emilia.service.d
sudo tee /etc/systemd/system/emilia.service.d/30-capture-source.conf >/dev/null <<'EOF'
[Service]
Environment="HELIOS_AUDIO_INPUT_DEVICE=pulse:alsa_input.usb-Solid_State_System_Co._Ltd._USB_PnP_Audio_Device_000000000000-00.analog-stereo"
Environment="HELIOS_AUDIO_INPUT_STRICT=true"
EOF
sudo systemctl daemon-reload
sudo alsactl store 2
```

`alsactl store 2` updates the USB card's saved capture gain for `alsa-restore.service`; inspect the resulting `state.Device` entry to confirm `Mic Capture Volume` is 496. PulseAudio already has the restore modules and databases, so no extra profile/volume boot script is proposed before reboot evidence. Do not enable or start `emilia.service` as part of this change. Any interactive launcher must export the same two `HELIOS_AUDIO_*` variables; otherwise it remains outside the service configuration.

## Verification

```sh
systemctl is-enabled emilia.service
systemctl is-active emilia.service
systemctl show emilia.service -p Environment
pactl list short sources
pactl list cards | grep -E 'Name:|Active Profile:'
pactl list sources | grep -E 'Name:|Volume:|Active Port:'
amixer -c 2 sget Mic
```

Read-only post-change verification: `emilia.service` remained `disabled` and `inactive`; its effective environment contains the exact USB PulseAudio source and `HELIOS_AUDIO_INPUT_STRICT=true`. The USB source remains present at 100% volume; USB profile remains `output:analog-stereo+input:analog-stereo`; live Mic capture is 496/496 and saved ALSA `state.Device` Mic Capture Volume is 496. No audio stream was opened.

After the separately approved reboot, the boot ID changed from `19d561b0-2beb-42ca-a828-b17b3755caa9` to `8c01da32-a045-45e9-94b5-300373784a83`. At approximately 78 seconds uptime, without manual audio setup, the service was still disabled/inactive; its effective input device and strict setting were intact; the USB duplex profile was active; USB source volume was 100%; Mic capture and its saved ALSA value were 496/496. PulseAudio restore modules were loaded and the USB source existed. The three backup/drop-in hashes below were unchanged. This proves boot persistence of those settings.

With separate operator approval, one generated-only A1 acoustic probe ran on that boot. Piper generated a 1.916 s mono WAV (22,050 Hz), played once through the explicit USB sink at its unchanged 23% volume. The current deployed `SpeechRecognizer` opened exactly one input stream with `input_device_strict=True` and the explicit USB PulseAudio source. Its `capture_pulse_source_selected` and `capture_device_resolved` events identified the USB source, PortAudio index 17, and `analog-input-mic`. An empty decoder prevented transcript generation. Fifty 100 ms capture frames gave initial-eight-frame median RMS 0.014027 and maximum RMS 0.081973 (ratio 5.84); playback exited 0 and the capture probe exited 0. This is one acoustic sample, sufficient to establish nonzero capture after reboot, not a calibrated detection or recognition performance result. No loopback module, pre-existing sink input, or source output was present. No PCM samples or transcript were logged or stored. The temporary generated WAV was removed; no source output or sink input remained. Service remained disabled/inactive. The first probe against the older development checkout failed at constructor argument validation before opening audio or playing the fixture; the successful probe used the current validation deployment.

Validation: `PYTHONDONTWRITEBYTECODE=1 timeout 600 /home/emilia/helios-ai-jetson-framework/venv/bin/python -m pytest -q -m 'not remote_live' -p no:cacheprovider` in the validation deployment: **2086 passed, 1 deselected, exit 0**. No Python source changed. Local `git diff --check` passed. No remote provider was called.

### SHA-256 evidence

| File | SHA-256 |
| --- | --- |
| `/etc/systemd/system/emilia.service` | `61341d7d71a0161e5fc0557104cf9d8cba8513066fabbe6cae07306c729f224d` |
| `/etc/systemd/system/emilia.service.a1-before` | `61341d7d71a0161e5fc0557104cf9d8cba8513066fabbe6cae07306c729f224d` |
| `/var/lib/alsa/asound.state.a1-before` | `c0623618ee3c276a0123f10f16bd600c039b57c2d79c6449593e0103d84cec8b` |
| `/var/lib/alsa/asound.state` | `136266d16d442ae52664bda50f2d5e3b846c2a1fd7ca759ceef6ebc8c5055a9d` |
| `/etc/systemd/system/emilia.service.d/30-capture-source.conf` | `5953060b1dad12ae4b12d2531ec97b5ed754f22b068c4224615d29d2405d2f81` |

## Rollback and risks

```sh
sudo rm /etc/systemd/system/emilia.service.d/30-capture-source.conf
sudo systemctl daemon-reload
sudo cp -a /var/lib/alsa/asound.state.a1-before /var/lib/alsa/asound.state
sudo alsactl restore 2
```

The service file itself is not edited; its backup is retained for audit. Strict mode will stop capture if the USB source is absent or its PulseAudio name changes. Storing the current ALSA state can preserve other current USB mixer settings along with the gain; the backup permits reversal. Starting the system service may lack the user PulseAudio session environment even with the right source, so test its actual launch path after approval. A reboot may disrupt remote access; schedule it with local recovery available. The existing Codex routing drop-in remains untouched, and verification must not issue paid Codex requests.
