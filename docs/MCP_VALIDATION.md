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
| HA discovery | Actual version/auth/catalog; diagnostic result | Unverified, awaiting instance details |
| Live reads | Exact installed schema and exposed test entities | Blocked |
| Confirmed test writes | Named low-risk entities; local policy; verified confirmation | Blocked |
| Voice acoustic release | Device calibration, echo/false-confirmation/barged speech evidence | Blocked |
| Startup deployment | Reboot persistence, capture selection and rollback verified | Unverified |

`docs/mcp/rollout-status.json` records unknown values as null and absent evidence as
blocked/unverified. Zero hardware samples is not a pass. No production automation was
deployed on Debian or Jetson; the existing service is unchanged.

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

Rollout and rollback are described in MCP_HOME_ASSISTANT.md. A production entry-point
assembly, acoustic confirmation barge-in and installed HA target mapping still require
verification; the injected components and fake verifier alone are not a deployable voice
write profile. Preserve the ledger when disabling configuration or revoking tokens.
