# Optional MCP client

Install `requirements-automation.txt` only when deploying automation. The pinned
official SDK is MCP 2.3.0 (Python >=3.10). This adapter uses `ClientSession.initialize`
and negotiated handshake revisions through 2025-11-25. The newer stateless discovery
mode is deliberately unsupported; unsupported revisions fail closed.

Streamable HTTP is the only transport. Endpoints require HTTPS except loopback.
Bearer credentials come from the configured environment reference, separately from
ChatGPT OAuth. Redirects are rejected and a transport guard binds all requests to the
configured origin. Ambient proxy configuration is disabled.

Limits: 20 catalog pages, 1,000 tools, 64 KiB per schema/argument object, 1 MiB per
HTTP response stream/result, and configured finite per-operation timeouts. The total
response-stream bound also applies to long-lived server notification streams; these
may disconnect at that limit. JSON Schema resolution uses a registry with no network
retrieval. Remote annotations/descriptions never grant authorization.

Discovery performs no tools/call. Calls are never automatically retried. SDK SSE
resumption can retrieve an existing response; it does not resend a mutating POST.
Shutdown closes transport/session resources; cancellation propagates to the caller.
Transport errors after a possible send must be handled as unknown by the executor.
SDK background-task failures are recovered during shutdown in the owning task and
returned as typed client failures. They must not be confused with caller cancellation;
genuine cancellation propagates. Authentication loss during a call never causes replay.
SDK protocol logs are suppressed only within Helios client tasks, including inherited
transport tasks, because they can contain session IDs and tool content. Fixed content-free
audit events provide diagnostics instead; unrelated MCP clients retain their logging.

References: [official SDK source](https://github.com/modelcontextprotocol/python-sdk/tree/v2.3.0),
[MCP security guidance](https://modelcontextprotocol.io/docs/2025-11-25/tutorials/security/security_best_practices).
