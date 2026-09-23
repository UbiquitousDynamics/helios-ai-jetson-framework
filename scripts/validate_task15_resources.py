"""Content-free bounded resource summary for the Task 15 target test gate."""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time


def main() -> int:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("HELIOS_", "LLM_"))}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=short",
             "tests/test_task_manager.py", "tests/test_task_delegation.py",
             "tests/test_task_stress.py"],
            env=env, capture_output=True, text=True, timeout=240, check=False,
        )
        code = result.returncode
        summary = next((line for line in reversed(result.stdout.splitlines())
                        if " passed" in line or " failed" in line), "no summary")
    except subprocess.TimeoutExpired:
        code, summary = 124, "timeout"
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(json.dumps({"exit_code": code, "summary": summary,
                      "wall_seconds": round(time.monotonic() - started, 3),
                      "cpu_user_seconds": round(usage.ru_utime, 3),
                      "cpu_system_seconds": round(usage.ru_stime, 3),
                      "peak_child_rss_kib": usage.ru_maxrss}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
