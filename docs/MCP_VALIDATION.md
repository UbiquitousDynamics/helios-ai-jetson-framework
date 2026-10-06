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

| Stage | Required evidence | Current status |
| --- | --- | --- |
| Disabled checkout | No connection/action; existing voice suite | Model-free tested |
| HA discovery | Actual version/auth/catalog; diagnostic result | Passed on Debian, Core 2025.12.3; 21 tools, zero authorized |
| Live reads | Exact installed schema and exposed test entities | Blocked |
| Confirmed test writes | Named low-risk entities; local policy; verified confirmation | Blocked |
| Voice acoustic release | Device calibration, echo/false-confirmation/barged speech evidence | Blocked |
| Startup deployment | Reboot persistence, capture selection and rollback verified | Unverified |

`docs/mcp/rollout-status.json` records unknown values as null and absent evidence as
blocked/unverified. Zero hardware samples is not a pass. No production automation was
deployed on Debian or Jetson; the existing service is unchanged.

On 2026-10-06 the PR's opt-in `test_explicit_home_assistant_version_and_catalog`
passed on Debian x86_64/Python 3.11 in the isolated diagnostic environment. It used
the dedicated private token from #39 and the explicit empty-scope configuration.
Only GET `/api/config`, MCP initialization and tools/list were performed. The live
test is discovery evidence, not a state read, acoustic sample or physical write.

## Executable evidence review

Run `python scripts/mcp_rollout_check.py --manifest docs/mcp/rollout-status.json
--stage home_assistant_discovery`. Exit 0 means that stage's required evidence is
present; exit 2 means blocked. Output contains only the stage, status and a fixed
reason code. The CLI opens no network/audio connection and never enables automation.
CI checks the bundled discovery record on every OS/Python matrix entry, without
using credentials or opting into the live test.

Stages are ordered: `home_assistant_discovery`, `live_reads`, `voice_confirmation`,
`voice_writes`, `reboot_persistence`. Later stages require all earlier stages.
The discovery artifact must be bounded, local and match its recorded SHA-256.
Read evidence requires verified exact target mapping, exposure, permission and at
least one successful scoped read. The installed intent schemas have no entity-ID
selector; claiming that the catalog is present cannot satisfy these read gates.

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
The 2026-09-24 USB/Jetson calibration in repository documents does not calibrate the
current Debian integrated microphone. Windows CI cannot certify Jetson hardware.

Rollout and rollback are described in MCP_HOME_ASSISTANT.md. Concurrent confirmation
barge-in software is implemented by #42; acoustic confirmation and interruption on
the Debian microphone still require calibration and verification. Production assembly,
exact HA target mapping and reboot evidence remain deployment gates. The validation
PR is reviewable while those gates correctly report blocked; merging validation does
not authorize live writes. Preserve the ledger when disabling configuration or
revoking tokens.
