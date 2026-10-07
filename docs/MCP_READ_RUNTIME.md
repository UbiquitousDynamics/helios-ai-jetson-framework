# First MCP voice read

The optional entry-point runtime supports Home Assistant's verified `GetDateTime`
tool. Enable a private copy of `examples/automation.home-assistant-read.toml`, select
it explicitly with `HELIOS_AUTOMATION_CONFIG` and provide the dedicated bearer token
in `HELIOS_HA_TOKEN`. Install `requirements-automation.txt` in the runtime virtualenv.
Examples remain disabled. Ordinary startup creates no MCP client or ledger.

Say **“Emilia, domotica che ora è”** or **“Emilia, domotica che data è”**. English
profiles accept “Emilia, home control what time is it”. The wake-word/authoritative
recognition and observed TTS paths remain those of the assistant. The request is
interpreted locally with explicit phrases, without an LLM or remote catalog/state
egress. Other domotica requests receive a local explanation of the supported read.

This profile requires exactly one server named `homeassistant`, exactly the tool
`GetDateTime`, and empty entity/area scopes. Extra tools or entities cause startup
to fail closed. Each supported final request owns a fresh SDK session and checks
the current catalog through the local policy and durable executor. The private
receipt ledger is closed after the assistant workers and never stores tool results.
Duplicate turns, ambiguous responses, malformed dates/times, transport failure and
cancelled requests do not cause replay or invented clock readings. Speaker failures
are propagated without retry. Only validated numeric date/time fields are spoken;
server prose and instructions are never handed to TTS or an LLM.

An explicit second `homeassistant_state` server can enable exact light-state reads
through the separate local bridge. See `MCP_SCOPED_STATE.md` for its independent
credential, entity allowlist and spoken alias mapping. The date/time-only profile
does not require or start that bridge.

This is a server-clock read, not a sensor query or an actuator profile. Device-state
reads still need explicit entity scope/mapping; writes still require the separate
calibrated confirmation and rollout evidence. The controller uses the existing
barge-in cancellation and speech leases. Acoustic performance remains unverified.

For systemd, use a private mode-0600 EnvironmentFile containing the token and the
explicit configuration path. Do not place the token in ExecStart, source control or
console output. Keep ordinary LLM/OAuth configuration unchanged. Choose and renew the credential lifetime privately for each deployment.

Rollback: remove the read-runtime EnvironmentFile override, reload systemd and
restart Helios. Revoke the dedicated HA token if no longer needed. Preserve the
private receipt ledger so an uncertain action cannot be replayed after rollback.
