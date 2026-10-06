# Home Assistant provisioning and diagnostics

Discovery compatibility was verified on the Debian installation of Core 2025.12.3
on 2026-10-06 with the actual PR #39 diagnostic modules. The authenticated command
returned `discovery_only`, 21 tools and zero authorized tools. No tools/call or device
actions were performed. This verifies discovery, not entity reads or write compatibility.

## Verified Core 2025.12.3 profile

The integration was configured through Home Assistant's API with Assist selected.
The diagnostic endpoint is `http://127.0.0.1:8123/api/mcp`: Home Assistant and Helios
share the Debian host, so no LAN HTTP exception is needed. The installed version has
no `/api/mcp/assist` route; select Assist during integration setup instead. See the
[versioned transport source](https://github.com/home-assistant/core/blob/2025.12.3/homeassistant/components/mcp_server/http.py).
The MCP server negotiated protocol version `2025-06-18`.

Provisioning created a dedicated local-only, non-administrator user in Home Assistant's
`system-read-only` group. Its 30-day token is stored only on the device at
`/home/debian/.config/helios/ha-token`, mode 0600. The temporary login refresh token was
revoked. Tokens and raw catalog content are not repository artifacts. The diagnostic
configuration at `/home/debian/.config/helios/automation-diagnostics.toml` has empty
tools, entities and areas, and all remote-context permissions disabled. It is selected
only by the diagnostic CLI, not by the production service.

| Observed tools | Observed selectors | Local profile decision |
| --- | --- | --- |
| `GetDateTime` | Empty input | Present; no device read, not authorized |
| `GetLiveContext` | Empty input | Broad home context; not authorized |
| `todo_get_items` | `todo_list`, optional `status` | List name is not an exact entity ID; not authorized |
| `HassTurnOn`, `HassTurnOff`, `ChangeLightState`, `HassLightSet` | Name/area/floor/domain selectors | No `entity_id`; exact light writes unsupported by this profile |
| Remaining timer/media/list/vacuum actions | Intent-specific selectors | Outside the initial light/scene scope; not authorized |

This table maps real schemas rather than guessing an entity selector. No configured
entity needs exposure because the scope is empty. The existing exposure snapshot had
three explicit Assist exclusions; default exposure was not overridden. That snapshot
does not certify an exact target for future actions. Establish a designated sandbox
entity and verify its exposure and unique mapping before any read/write rollout.

The installed catalog includes `GetLiveContext`, not `homeassistant__GetLiveContext`.
Use the installed catalog as authority when configuring a different version. The
production Helios service remained active and enabled, without an MCP runtime restart.
Its process environment had no `HELIOS_AUTOMATION_CONFIG`. Real negative checks also
passed: an invalid bearer credential returned a fixed failure code with exit 2, and
an expected version of 2025.12.4 returned `version_mismatch` with exit 2. The sanitized
[discovery evidence](mcp/home-assistant-2025.12.3-discovery.json) records these checks.

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

Tool names can differ between HA versions. A context tool is not automatically
authorized by Helios: the installed catalog must confirm it, and broad context may
include more exposed entities than a desired local scope. Do not invent tool names or
infer exposed entities from catalog hints. `exposure_matches` checks explicit snapshots;
the diagnostic command does not claim to verify entity exposure without such evidence.

Rollback: unset HELIOS_AUTOMATION_CONFIG (or set enabled=false), restart Helios, and verify
ordinary local voice operation. Revoke the dedicated HA token in the user's security
settings; remove unused exposed entities and the MCP integration if appropriate. Preserve
the dispatch ledger through rollback so an uncertain action cannot be replayed later.
Voice writes remain gated by #29/#32 and the unresolved Debian microphone.

For the provisioned profile, remove or revoke the token named "Helios MCP metadata
diagnostics" in the dedicated user's security settings (or delete that user as an
administrator). Remove the private diagnostic configuration/token files when no longer
needed. Delete the MCP integration if it is no longer used. Renew the token explicitly
after its 30-day lifetime; no automatic renewal or remote credential export is configured.
