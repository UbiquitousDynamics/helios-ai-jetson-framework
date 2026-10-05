# Home Assistant provisioning and diagnostics

Installed Home Assistant compatibility is **unverified** until the user's URL/version
and an authenticated catalog are checked. Fake catalogs are software tests, not a
claim that a particular HA tool exists. No devices have been acted on.

The [official integration documentation](https://www.home-assistant.io/integrations/mcp_server/)
describes Streamable HTTP at `/api/mcp` and the selected Assist API at `/api/mcp/assist`.
Use a dedicated non-administrator HA user where supported and expose only designated
test entities to Assist. Tokens inherit that user's authority; a token itself is not a
per-tool least-privilege scope. Local policy adds narrower limits. HA credentials are
separate from ChatGPT OAuth. Helios currently supports bearer tokens, not HA OAuth flows.

1. Add the MCP Server integration and choose Assist. Review exposed entities and keep
   sensitive devices/scripts unexposed. Begin with a sandbox light/sensor.
2. Copy `examples/automation.home-assistant.toml` to a private configuration directory.
   Set the actual HTTPS endpoint. Local HTTP is accepted only for loopback; a local
   tunnel/TLS endpoint is needed for an HTTP-only LAN instance.
3. Provision a dedicated HA token in a private file (Linux mode 0600). Set `enabled=true`
   only in that explicitly selected diagnostic configuration. Leave tools/entities empty
   for discovery; this grants no device calls.
4. Run `python scripts/mcp_doctor.py --config /private/automation.toml --server homeassistant
   --credential-file /private/ha-token --expected-version <installed-version>`.
   The command performs GET `/api/config` and MCP initialization/tools/list only.
   It emits version, catalog fingerprint, counts and fixed compatibility codes, never
   token/config/catalog content. Missing permission or catalog/version drift is a failure.
5. Review the actual schemas before adding exact tool/entity scopes and trusted local
   policy rules. Tool hints are not permission. Name/area intent selectors do not prove
   a unique entity target; those writes stay denied until a verified mapping exists.
   No generic service-call shortcut is provided. Read checks and designated writes are
   separate opt-ins; the diagnostic command contains no tools/call.

Current HA source advertises `homeassistant__GetLiveContext`, but it is not automatically
authorized by Helios: the installed catalog must confirm it, and broad context may
include more exposed entities than a desired local scope. Do not invent tool names or
infer exposed entities from catalog hints. `exposure_matches` checks explicit snapshots;
the diagnostic command does not claim to verify entity exposure without such evidence.

Rollback: unset HELIOS_AUTOMATION_CONFIG (or set enabled=false), restart Helios, and verify
ordinary local voice operation. Revoke the dedicated HA token in the user's security
settings; remove unused exposed entities and the MCP integration if appropriate. Preserve
the dispatch ledger through rollback so an uncertain action cannot be replayed later.
Voice writes remain gated by #29/#32 and the unresolved Debian microphone.
