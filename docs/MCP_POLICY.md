# Local automation policy

`LocalPolicy` accepts immutable, operator-owned `ToolPolicy` rules. Rules name exact
server/tool identities and classify only reads, lights or scenes. No rules means deny.
Server tool annotations and descriptions never select a rule or grant permission.

Each proposal must match the current discovered catalog and configured server/tool
scope. Targets are exact entity IDs. Writes require one named light or scene; locks,
alarms, scripts, automations, administration and generic service execution are denied.
Area scope is checked for reads. Area writes must first be resolved locally to explicit
entities; combined area/entity selectors are rejected because server union semantics
could affect additional devices. No fuzzy target or list of guessed names is accepted.

All arguments must be fixed locally, declared entity/area selectors or locally bounded
numbers. Schema validation adds constraints but cannot broaden local authorization.
Confirmation is required for writes and must match the exact proposal fingerprint,
session and expiry. The executor rechecks policy and catalog immediately before dispatch.

`allow_remote_catalog`, `allow_remote_home_state` and `allow_remote_results` are
independent booleans, false by default. The earlier generic `allow_remote_context`
field and LLM transcript permission grant none of these permissions. Context is copied
only for the individually selected categories. This policy API does no network I/O.
