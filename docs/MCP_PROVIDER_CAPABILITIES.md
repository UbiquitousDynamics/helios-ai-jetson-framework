# Structured proposal capability evidence

Read-only inspection on the deployed Debian host, 2026-10-05:

| Component | Observed API | Mechanism selected |
| --- | --- | --- |
| openai-codex 0.144.4 | `Thread.turn(..., output_schema: JsonObject)` | Isolated ephemeral structured-output turn |
| Ollama Python 0.6.3 | `Client.chat(..., format: dict, stream=False)` | JSON Schema `format` on a separate local request |
| Ordinary Helios voice adapters | Text streaming; tool completions rejected | Unchanged |

Inspection used package metadata and `inspect.signature`, with no model request.
Official references: [Codex app server outputSchema](https://developers.openai.com/codex/app-server/),
[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs).
API capability is verified; successful inference with a particular model is unverified.
Ollama cloud structured output is unsupported by the cited documentation. Provider errors,
invalid output or unsupported mechanisms produce clarification, never an execution claim.

The planner sends a complete JSON Schema and parses only the entire final structured
response. Codex deltas are bounded and assembled only for a completed turn. No prose,
Markdown fences or arbitrary JSON substring is interpreted as a command. Allowed tools
come from injected catalog entries and local rules, with a per-turn action bound.
The planner assigns action IDs and expiry locally, then validates each proposal against
local policy. It has no executor/client reference.

Codex reuses Helios's auth-only environment, disabled apps/plugins/shell/tools and
read-only sandbox. Home Assistant credentials never enter its runtime. Remote planning
requires both explicit transcript permission and separate catalog permission. No home
state/results are sent by this adapter. Live model compatibility tests remain opt-in.
