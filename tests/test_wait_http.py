from __future__ import annotations

import io
import urllib.error

from scripts import wait_http


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_wait_until_ready_retries_then_succeeds(monkeypatch) -> None:
    attempts = iter((urllib.error.URLError("starting"), Response()))

    def open_endpoint(_url: str, *, timeout: float):
        assert timeout > 0
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(wait_http.urllib.request, "urlopen", open_endpoint)
    monkeypatch.setattr(wait_http.time, "sleep", lambda _delay: None)

    assert wait_http.wait_until_ready("http://127.0.0.1/health", timeout=5, interval=1)


def test_wait_until_ready_rejects_invalid_deadlines() -> None:
    try:
        wait_http.wait_until_ready("http://127.0.0.1/health", timeout=0, interval=1)
    except ValueError as exc:
        assert str(exc) == "timeout and interval must be positive"
    else:
        raise AssertionError("zero timeout should be rejected")
