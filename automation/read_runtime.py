"""Opt-in local Home Assistant date/time read; no model or actuation path."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, time as clock_time
from pathlib import Path

from automation.client import MCPClient
from automation.contracts import ActionProposal, OutcomeStatus, object_json
from automation.executor import ActionExecutor, ReceiptLedger
from automation.policy import ActionKind, LocalPolicy, ToolPolicy

_MONTHS = (
    "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre"
).split()


def _unbound_speaker(_message):
    raise RuntimeError("Read runtime speaker is not bound")


def _datetime_phrase(payload, language):
    result = json.loads(object_json(payload))
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1 or content[0].get("type") != "text":
        raise ValueError("Unsupported read result")
    response = json.loads(object_json(content[0]["text"]))
    if response.get("success") is not True:
        raise ValueError("Read failed")
    values = response["result"]
    day, moment = values["date"], values["time"]
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("Invalid date")
    if not isinstance(moment, str) or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", moment):
        raise ValueError("Invalid time")
    day, moment = date.fromisoformat(day), clock_time.fromisoformat(moment)
    if language == "it":
        return (
            f"Home Assistant indica le {moment.hour} e {moment.minute:02d}. "
            f"Oggi è il {day.day} {_MONTHS[day.month - 1]} {day.year}."
        )
    return f"Home Assistant reports {moment.hour}:{moment.minute:02d} on {day.isoformat()}."


class _ReadCapture:
    def __init__(self, client):
        self.client = client
        self.result = None

    async def discover(self):
        return await self.client.discover()

    async def call(self, tool, arguments):
        self.result = await self.client.call(tool, arguments)
        return self.result


class ReadOnlyController:
    def __init__(self, settings, ledger, *, environ, client_factory, speak, language):
        self.settings = settings
        self.ledger = ledger
        self.environ = environ
        self.client_factory = client_factory
        self.speak = speak
        self.language = language
        self._cancelled = threading.Event()
        self.policy = LocalPolicy(
            settings,
            (ToolPolicy("homeassistant", "GetDateTime", ActionKind.READ, require_entity=False),),
        )

    def accepts(self, text):
        return (
            text.strip()
            .casefold()
            .startswith("domotica " if self.language == "it" else "home control ")
        )

    def cancel(self):
        self._cancelled.set()

    async def handle(self, text, recognition, *, session_id, turn_id, cancelled=lambda: False):
        if cancelled() or not recognition.is_final:
            return None
        commands = (
            {
                "domotica che ora è",
                "domotica che ore sono",
                "domotica che data è",
                "domotica data e ora",
            }
            if self.language == "it"
            else {"home control what time is it", "home control what is the date"}
        )
        command = " ".join(text.casefold().strip().rstrip(".!?").split())
        if command not in commands:
            self.speak(
                "Posso solo leggere data e ora di Home Assistant."
                if self.language == "it"
                else "I can only read the date and time from Home Assistant."
            )
            return None
        stop = self._cancelled = threading.Event()

        def is_cancelled():
            return stop.is_set() or cancelled()

        try:
            async with self.client_factory(
                self.settings, "homeassistant", environ=self.environ
            ) as client:
                catalog = await client.discover()
                tool = next(item for item in catalog if item.name == "GetDateTime")
                proposal = ActionProposal(
                    "homeassistant",
                    tool.name,
                    tool.catalog_id,
                    session_id,
                    turn_id,
                    hashlib.sha256(f"{session_id}\0{turn_id}\0GetDateTime".encode()).hexdigest(),
                    "{}",
                    time.time() + self.settings.proposal_ttl_seconds,
                )
                captured = _ReadCapture(client)
                outcome = await ActionExecutor(self.policy, captured, self.ledger).execute(
                    proposal, cancelled=is_cancelled
                )
                if is_cancelled():
                    return outcome
                if outcome.status != OutcomeStatus.SUCCESS or captured.result is None:
                    raise ValueError("Read outcome unavailable")
                message = _datetime_phrase(captured.result.payload_json, self.language)
        except Exception:
            if is_cancelled():
                return None
            message = (
                "Non posso verificare data e ora di Home Assistant. La lettura non è stata ripetuta."
                if self.language == "it"
                else "I cannot verify Home Assistant's date and time. The read was not replayed."
            )
        if not is_cancelled():
            # Speech failures are not caught or replayed.
            self.speak(message)
        return None


@contextmanager
def read_only_runtime(
    settings,
    *,
    environ=None,
    state_dir=None,
    client_factory=MCPClient,
    speak=_unbound_speaker,
    language="it",
):
    if (
        not settings.enabled
        or language not in {"it", "en"}
        or len(settings.servers) != 1
        or settings.servers[0].server_id != "homeassistant"
        or settings.servers[0].tools != ("GetDateTime",)
        or settings.servers[0].entities
        or settings.servers[0].areas
    ):
        raise ValueError("Unsupported read-only runtime scope")
    env = dict(os.environ if environ is None else environ)
    directory = (
        Path(state_dir)
        if state_dir
        else Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "helios/mcp-read"
    )
    if directory.is_symlink():
        raise ValueError("Unsafe ledger directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger_path = directory / "receipts.sqlite3"
    if ledger_path.is_symlink():
        raise ValueError("Unsafe ledger file")
    ledger = ReceiptLedger(ledger_path)
    try:
        yield ReadOnlyController(
            settings,
            ledger,
            environ=env,
            client_factory=client_factory,
            speak=speak,
            language=language,
        )
    finally:
        ledger.close()
