# ADR 0001: Helios owns the MCP authorization boundary

Status: accepted for the opt-in implementation tracked by issue #23.

Helios will own an injectable MCP client. Providers may propose actions, but cannot
authorize or execute them. Local policy owns server, tool and entity scopes, risk
classification and confirmation requirements. Remote tool annotations are untrusted
metadata. The existing text streaming path continues rejecting tool calls.

Proposals carry canonical immutable JSON, catalog identity, session/turn/action IDs
and UTC epoch expiry. Confirmation binds the exact proposal fingerprint. Policy and
expiry must be checked immediately before dispatch. Wall-clock rollback must invalidate
pending confirmations in the future session controller; durable receipts use epoch time.

Configuration is loaded only through HELIOS_AUTOMATION_CONFIG. A clean checkout
remains disabled, with no discovery, connection, execution or additional egress.
Credentials are environment references, never literals in configuration. Remote home
context requires separate explicit permission, independently of transcript routing.

The executor will own a durable dispatch ledger. An uncertain delivery produces an
unknown outcome, without automatic replay. Cancellation and provider fallback cannot
cause duplicate physical actions. Audit logs are separate from this execution ledger.

Initial device scope is reads and explicitly authorized lights/scenes. Sensitive
devices and administrative or opaque scripts remain denied. Deployment requires the
later transport, policy, executor and voice issues; these contracts alone perform no
device operations. Windows tests do not certify Jetson hardware or acoustic safety.
