"""Optional structured-output adapters, separate from ordinary voice streaming."""

from __future__ import annotations

import json
import threading
from typing import Any
from urllib.parse import urlsplit

import anyio

from automation.planner import ProposalCapability


class OllamaProposalProvider:
    def __init__(
        self,
        model: str,
        *,
        host: str = "http://localhost:11434",
        timeout: float = 10,
        client: Any = None,
    ):
        self.model = model
        self.host = host
        self.timeout = timeout
        self.client = client
        self.capability = ProposalCapability(
            "ollama",
            True,
            urlsplit(host).hostname not in {"localhost", "127.0.0.1", "::1"},
            "Ollama 0.6.3 AsyncClient.chat format schema; model support requires opt-in validation",
        )

    async def generate(self, prompt: str, schema: dict[str, Any]) -> str:
        client = self.client
        owned = client is None
        if owned:
            from ollama import AsyncClient

            client = AsyncClient(host=self.host, timeout=self.timeout)
        try:
            response = await client.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                format=schema,
                stream=False,
                options={"temperature": 0, "num_predict": 1024},
            )
            message = response["message"] if isinstance(response, dict) else response.message
            return message["content"] if isinstance(message, dict) else message.content
        finally:
            if owned:
                await client._client.aclose()


class CodexProposalProvider:
    def __init__(self, model: str, *, runtime_factory=None):
        self.model = model
        self.runtime_factory = runtime_factory
        self.capability = ProposalCapability(
            "codex_subscription",
            True,
            True,
            "Pinned openai-codex 0.144.4 Thread.turn output_schema verified on Debian; model support unverified",
        )

    async def generate(self, prompt: str, schema: dict[str, Any]) -> str:
        from api.providers.codex_app_server import _OfficialCodexRuntime, _notification_parts
        from api.providers.codex_session import field_value

        stopped = threading.Event()
        finished = threading.Event()
        active = {}

        def worker():
            runtime = (self.runtime_factory or _OfficialCodexRuntime)()
            active["runtime"] = runtime
            try:
                if stopped.is_set() or runtime.account_kind() != "chatgpt":
                    raise ValueError("Unavailable ChatGPT authentication")
                turn = runtime.start_proposal_turn(
                    model=self.model, prompt=prompt, output_schema=schema
                )
                active["turn"] = turn
                if stopped.is_set():
                    turn.interrupt()
                    raise ValueError("Cancelled")
                fragments = []
                size = 0
                for notification in turn.stream():
                    if stopped.is_set():
                        raise ValueError("Cancelled")
                    method, payload = _notification_parts(notification)
                    if method == "item/agentMessage/delta":
                        delta = field_value(payload, "delta")
                        if not isinstance(delta, str):
                            raise ValueError("Malformed structured delta")
                        size += len(delta.encode())
                        if size > 65536:
                            turn.interrupt()
                            raise ValueError("Oversized structured output")
                        fragments.append(delta)
                    elif method == "turn/completed":
                        completed = field_value(payload, "turn", payload)
                        if field_value(completed, "status") != "completed":
                            raise ValueError("Structured turn failed")
                        # Validate the whole output, never scrape JSON from prose.
                        value = "".join(fragments)
                        json.loads(value)
                        finished.set()
                        return value
                raise ValueError("Incomplete structured turn")
            finally:
                runtime.close()

        try:
            return await anyio.to_thread.run_sync(worker, abandon_on_cancel=True)
        finally:
            stopped.set()
            turn = active.get("turn")
            if turn is not None and not finished.is_set():
                # Control RPC may block; it must not block the cancellation path.
                def interrupt():
                    try:
                        turn.interrupt()
                    except Exception:
                        pass

                threading.Thread(target=interrupt, daemon=True).start()
