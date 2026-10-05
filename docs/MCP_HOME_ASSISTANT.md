# Home Assistant provisioning and diagnostics

Installed Home Assistant compatibility is **unverified** until the user's URL/version
and an authenticated catalog are checked. Fake catalogs are software tests, not a
claim that a particular HA tool exists. No devices have been acted on.

## Home Assistant Core 2025.12.3 checkpoint

The operator reports Core 2025.12.3 and Frontend 20251203.2 on the Debian host.
An unauthenticated loopback probe found the frontend responding with HTTP 200,
`/api/` with 401, and both `/api/mcp` and `/api/mcp/assist` with 404. These probes
do not verify the reported version or establish why the MCP route is unavailable.
No token reference was found in the SSH environment or the inspected top-level
Helios configuration filenames; credentials may exist elsewhere.

The [2025.12.3 transport source](https://github.com/home-assistant/core/blob/2025.12.3/homeassistant/components/mcp_server/http.py)
registers `/api/mcp`, with POST requests and no `/api/mcp/assist` route. Its
[configuration flow](https://github.com/home-assistant/core/blob/2025.12.3/homeassistant/components/mcp_server/config_flow.py)
selects the Assist API during integration setup. Use `/api/mcp` for this version;
the API-specific path described below belongs to newer Home Assistant versions.
Authenticated initialization and catalog compatibility remain unverified.

Because Home Assistant and Helios share the Debian host, the diagnostic endpoint
can be `http://127.0.0.1:8123/api/mcp`; this satisfies the existing loopback-only
HTTP policy without changing transport validation. A client on another machine
still requires HTTPS or a local tunnel. Before diagnostics, configure the MCP
Server integration and a dedicated token in a private file. Discovery grants no
device calls, and production automation remains disabled.

## General setup

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
