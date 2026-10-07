# Scoped light-state reads

A Home Assistant native `GetLiveContext` tool may have an
empty input schema and cannot request an exact entity. The optional local bridge
`python -m automation.state_bridge` exposes a separate MCP `GetEntityState` tool.
It performs only `GET /api/states/<entity_id>` for explicit configured light IDs.
It does not call the native broad-context tool or expose service/action methods.
HA REST access is authorized by the dedicated read-only HA account; Assist exposure
is not the authorization mechanism for this bridge. Never describe this as native
Assist entity-scoped MCP support.

The bridge binds to `127.0.0.1:8124`, requires a separate random bearer credential,
and uses the existing private HA token upstream. It rejects requests outside its
allowlist before connecting, follows no redirects and has a 10-second timeout and
64-KiB response limit. It returns only the matching entity ID and one of `on`,
`off`, `unknown`, `unavailable`; all attributes and server prose are discarded.
API failures, revoked credentials and malformed responses do not cause retries.

Use a private enabled copy of `examples/automation.home-assistant-state.toml`.
Replace `light.example` with the selected ID. Set private environment values:

- `HELIOS_HA_READ_ENTITIES`: JSON array of exact allowed light IDs for the bridge.
- `HELIOS_HA_READ_ALIASES`: JSON object mapping local spoken names to those same
  exact IDs for Helios, for example `{"example light":"light.example"}`.
- `HELIOS_HA_TOKEN`: dedicated upstream Home Assistant read-only bearer token.
- `HELIOS_HA_BRIDGE_TOKEN`: independent random bearer token, at least 32 ASCII
  characters, shared only by the local bridge and Helios.
- `HELIOS_AUTOMATION_CONFIG`: path to the explicit private enabled configuration.
- `HELIOS_HA_BASE_URL`: optional upstream origin; defaults to local port 8123.
  Non-loopback origins require HTTPS; paths, queries and embedded credentials fail.

Store credentials in mode-0600 EnvironmentFiles, outside source control. Never put
tokens in command arguments, journals, PRs or transcripts. Run the bridge in its
own systemd user service with the project's optional automation requirements,
the repository as WorkingDirectory and `.venv/bin/python -m automation.state_bridge`
as ExecStart. Use Restart=on-failure and enable the unit for startup. The Helios
drop-in loads its private EnvironmentFile; preserve existing LLM/OAuth settings.
Bridge absence or upstream denial produces a failed read, never an invented state.

Say “Emilia, domotica stato example light” with the configured local alias. English
profiles accept “Emilia, home control state example light”. Only exact configured
phrases dispatch. Unknown aliases do not connect. Local policy, receipt ledger,
authoritative recognition and barge-in cancellation also apply to state reads.
Unavailable/unknown states are spoken explicitly. No entity ID, attribute or
untrusted server text is handed to an LLM; spoken labels come from local constants.

Rollback: select the previous date/time-only private profile, remove alias and
bridge environment references, reload systemd and restart Helios. Stop/disable the
bridge and revoke its local token. Preserve receipt ledgers. Verify date/time still
works and the state endpoint is no longer listening. An enabled unit is not evidence
of a completed reboot test. Physical voice confirmation and write gates remain
unverified; this profile enables no device actions.
