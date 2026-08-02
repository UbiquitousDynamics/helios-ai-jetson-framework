from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_provider_package_keeps_concrete_adapters_lazy() -> None:
    project_root = Path(__file__).resolve().parents[1]
    code = """
import sys
import api.providers as providers

adapter_modules = {
    "api.providers.codex_app_server",
    "api.providers.ollama",
    "api.providers.openai_chat_sse",
}
assert adapter_modules.isdisjoint(sys.modules)
assert providers.OpenAIChatSSEAdapter.__module__ == "api.providers.openai_chat_sse"
assert "api.providers.openai_chat_sse" in sys.modules
assert "api.providers.ollama" not in sys.modules
assert "api.providers.codex_app_server" not in sys.modules
"""

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
