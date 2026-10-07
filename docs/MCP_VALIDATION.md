# MCP validation and rollout gates

Model-free tests run in the existing Linux/Windows CI matrix (Python 3.10/3.12).
They use injected providers/recognition and the pinned real SDK with mock HTTP; no
microphone, models, credentials, Home Assistant instance or real device are required.
The integrated test covers proposal, exact confirmation, dispatch, lost response,
unknown speech, fallback replay suppression and receipt lookup after restart.

Runtime audio/model dependencies remain optional. Automation dependencies are separate
in `requirements-automation.txt`, included by the development test requirements only.
CI does not opt into external calls. Live metadata verification is skipped unless
`HELIOS_MCP_LIVE_DISCOVERY=1`, with an explicit enabled HELIOS_AUTOMATION_CONFIG,
server credential reference and optional expected installed version. It performs only
GET config and tools/list. It does not constitute a live state read or designated write.

Deployment evidence is private and is not bundled with this repository.
The JSON records under `docs/mcp/` named `rollout-status.json`,
`scoped-read-rollout-status.json`, `discovery.example.json` and
`scoped-read.example.json` are synthetic fixtures only. Their sample verdicts
exercise offline validation and certify no real system, read or device action.

## Executable evidence review

Run `python scripts/mcp_rollout_check.py --manifest docs/mcp/rollout-status.json
--stage home_assistant_discovery`. Exit 0 means that stage's required evidence is
present; exit 2 means blocked. Output contains only the stage, status and a fixed
reason code. The CLI opens no network/audio connection and never enables automation.
CI checks the synthetic discovery fixture on every OS/Python matrix entry, without
using credentials or opting into the live test.

Review the newer selected-light evidence with `python scripts/mcp_rollout_check.py
--manifest docs/mcp/scoped-read-rollout-status.json --stage live_reads`. This exercises
the fictional scoped software read stage; selecting `voice_writes` remains blocked on missing
voice-confirmation evidence. The examples contain no actual deployment evidence.

For a repeatable explicitly authorized scoped read, run only
`tests/test_automation_live_reads.py` with `HELIOS_MCP_LIVE_READS=1` and the private
enabled state-profile environment. This independently opts into `GetEntityState`;
the metadata-only discovery flag does not permit it. The test checks a selected
state read and the bridge's denial of an unconfigured entity. It opens no microphone,
does not send model requests and cannot enable device actions. Ordinary CI skips it.

Operational rollback was exercised on Debian on 2026-10-06: stop Helios, select
the private date/time-only EnvironmentFile, reload systemd, stop the bridge and
restart Helios. The running process had one authorized server, no state aliases,
and a successful actual date/time read; bridge port 8124 was not listening.
Restore the previous state-profile drop-in, reload/start the bridge and restart
Helios. Both actual scoped state and date/time reads succeeded afterward, both
services remained enabled, and the existing LLM configuration was preserved.
No machine reboot or physical light action was performed. The private pre-test
drop-in remains on the device for recovery; receipt ledgers were preserved.

On 2026-10-07 the Debian service was updated to merged main and given an explicit
persistent integrated-microphone selector with strict resolution. See
`MCP_DEBIAN_CAPTURE.md`. Autostart of the earlier configuration was observed after
a different boot; the new selector still needs a controlled reboot/capture test.
Audio device metadata does not calibrate confirmation. Physical tests require
reviewed explicit stimulus/response audio devices,
generated-only stimuli, isolation/feedback abort setup and predeclared calibrated
limits. `voice_test_suite.py --require-hil` reviews the baseline and never opens
audio; its blocked result is not a completed physical test.

Stages are ordered: `home_assistant_discovery`, `live_reads`, `voice_confirmation`,
`voice_writes`, `reboot_persistence`. Later stages require all earlier stages.
The discovery artifact must be bounded, local and match its recorded SHA-256.
Read evidence requires verified exact target mapping, exposure, permission and at
least one successful scoped read. The installed intent schemas have no entity-ID
selector; claiming that the catalog is present cannot satisfy these read gates.
The selected state read therefore uses the separate allowlisted MCP bridge, rather
than native GetLiveContext. It proves exact REST permission and bridge exposure.

Acoustic evidence requires a target profile, calibration artifact hash, calibration
verification, predeclared thresholds, positive samples, zero failures and finite,
ordered median/p95/max within limits. Each of false wake, wrong target, playback
echo, missed denial, interruptions before/after dispatch and remote failure/fallback
requires a nonempty, failure-free scenario record. Empty samples, null thresholds,
NaN, boolean counts or a claimed stage pass alone remain blocked. A designated
write also needs exact low-risk target evidence and a successful test write. Deployment
needs runtime assembly, reboot, capture and rollback verification.

This is a review of **operator-maintained evidence**, not a hardware certification
or authorization mechanism. The checker does not independently authenticate a
calibration hash, recompute summary measurements from samples or deploy a runtime.
Use the existing acoustic runners to produce and review those measurements. Fixture
records in tests are fictional and must never be copied into release evidence.
The production policy, calibrated verifier and durable executor remain mandatory.

Reuse #18 and the existing voice test suite for generated acoustic fixtures only. Record
explicit capture/playback devices, device/route identity, gain, calibration artifact and
abort feedback limits before running. Predeclare hardware-specific latency/error and
false-confirmation thresholds before interpreting data. Include counts, failures,
median, nearest-rank p95 and max. Never store user speech/PCM. Test false wakes, wrong
targets, playback echo, missed denial, interrupts before/after dispatch and remote
failure/fallback without replay. Missing device/threshold evidence blocks the stage.

Reuse #13 for persistent capture selection, #12 for barge-in calibration, and #19/#20
for provider fallback behavior; this change does not alter their thresholds or retries.

Windows CI cannot certify target hardware. Require deployment-specific calibration
and verification before enabling live reads or writes. Keep device evidence private.
To disable the extension, unset HELIOS_AUTOMATION_CONFIG and restart the service.
Revoke unused credentials and preserve the receipt ledger to prevent replay.
