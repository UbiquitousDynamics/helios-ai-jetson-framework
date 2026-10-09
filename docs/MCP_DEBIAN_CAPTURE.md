# Debian capture persistence for MCP validation

On 2026-10-07 the Debian reference device was updated to merged main `de4706d`.
The existing read-only automation profile, private credentials, LLM routing and
receipt ledgers were retained. No light actions or machine reboot were performed.

The integrated analog microphone was present as the explicit PulseAudio/PipeWire
source `alsa_input.pci-0000_00_1b.0.analog-stereo`. The PortAudio `pulse` device
was unique, with index 12 at inspection time, ALSA host API index 0, 32 advertised
input channels and a default rate of 44100 Hz. These virtual-device capabilities
are metadata, not the actual mono/stereo capture parameters or acoustic evidence.
The volatile index was not used as the persistent selector.

A private user-service drop-in `40-mcp32-capture.conf` now selects
`HELIOS_AUDIO_INPUT_DEVICE=pulse:alsa_input.pci-0000_00_1b.0.analog-stereo` and
`HELIOS_AUDIO_INPUT_STRICT=true`. The recognizer checks that exact source and the
unique PortAudio pulse backend. Missing or ambiguous devices fail instead of
silently using the default. Existing gains, channel mode and recognition/barge-in
thresholds were not changed. The example `helios-capture-pulse.conf.example` is
inactive and must be adapted to each target, never copied with its placeholder.

After restart, the running process contained the explicit selector and strict
flag. Startup emitted `capture_pulse_source_selected`, `voice_ready` and
`capture_device_resolved`: pulse index 12, capture_channels=1, capture_rate=16000,
downmix=mono, the intended analog source and active_port=analog-input-internal-mic.
No fallback/unavailable event or startup error was observed. The merged-main
Debian suite passed (2247 passed, 3 skipped), then the explicitly opted-in actual
MCP scoped-read/denial test passed (1 passed).

Before this change, both Helios and its scoped-state MCP bridge were already active
and enabled after a different observed boot. Their ActiveEnterTimestampMonotonic
values were 16018491 and 16010965 microseconds (about 16 seconds after boot).
This is evidence of autostart of the earlier read runtime and configuration, not
of a reboot test for the new explicit capture drop-in or the new main commit.

Verify after startup: the service process has the explicit selector and strict
flag; `capture_pulse_source_selected` names the intended source;
`capture_device_resolved` names pulse and that same source; no fallback event is
present; `voice_ready` is emitted. Successful actual state/date-time reads verify
the MCP path independently. These checks do not establish a nonzero generated
speech level, calibrated confirmation, acoustic latency or physical write success.

For rollback, remove only this newly installed capture drop-in, reload the user
systemd manager and restart Helios. This returns to the previously observed default
input and does not affect MCP credentials or the state/date-time profile. Keep the
explicit source if it is correct; changing back to a default is a recovery step,
not a capture-persistence fix. The deployment procedure included a rollback trap
for an unsuccessful service restart; that trap was not triggered.

Remaining #32 evidence: a new boot with this exact configuration, matching resolved
source and controlled generated-speech capture; reviewed stimulus/response devices,
isolation and feedback-abort setup; acoustic calibration and predeclared limits;
confirmation/fault scenarios and designated low-risk test writes. No user speech
was recorded for these deployment checks. Unavailable measurements stay unverified.

The subsequent generated-speech pilots failed acoustic recognition. See
`MCP_ACOUSTIC_PILOTS.md` for measured levels, temporary gain trials and recovery.
They do not satisfy the remaining calibration or confirmation requirements.
