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
    def __init__(self, settings, ledger, *, environ, client_factory, speak, language, aliases):
        self.settings = settings
        self.ledger = ledger
        self.environ = environ
        self.client_factory = client_factory
        self.speak = speak
        self.language = language
        self.aliases = aliases
        self._cancelled = threading.Event()
        self.policy = LocalPolicy(
            settings,
            (
                ToolPolicy("homeassistant", "GetDateTime", ActionKind.READ, require_entity=False),
                ToolPolicy("homeassistant_state", "GetEntityState", ActionKind.READ),
            ),
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
        prefix = "domotica stato " if self.language == "it" else "home control state "
        alias = command[len(prefix) :] if command.startswith(prefix) else ""
        entity = self.aliases.get(alias)
        if command not in commands and entity is None:
            self.speak(
                "Posso leggere data, ora e lo stato delle luci configurate."
                if self.language == "it"
                else "I can read the date, time and state of configured lights."
            )
            return None
        stop = self._cancelled = threading.Event()

        def is_cancelled():
            return stop.is_set() or cancelled()

        try:
            server_id = "homeassistant_state" if entity else "homeassistant"
            tool_name = "GetEntityState" if entity else "GetDateTime"
            arguments = object_json(json.dumps({"entity_id": entity})) if entity else "{}"
            async with self.client_factory(
                self.settings, server_id, environ=self.environ
            ) as client:
                catalog = await client.discover()
                tool = next(item for item in catalog if item.name == tool_name)
                proposal = ActionProposal(
                    server_id,
                    tool.name,
                    tool.catalog_id,
                    session_id,
                    turn_id,
                    hashlib.sha256(f"{session_id}\0{turn_id}\0{tool_name}".encode()).hexdigest(),
                    arguments,
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
                message = (
                    _state_phrase(captured.result.payload_json, entity, alias, self.language)
                    if entity
                    else _datetime_phrase(captured.result.payload_json, self.language)
                )
        except Exception:
            if is_cancelled():
                return None
            message = (
                "Non posso verificare la lettura di Home Assistant. La lettura non è stata ripetuta."
                if self.language == "it"
                else "I cannot verify the Home Assistant read. The read was not replayed."
            )
        if not is_cancelled():
            # Speech failures are not caught or replayed.
            self.speak(message)
        return None


def _state_phrase(payload, entity, alias, language):
    result = json.loads(object_json(payload))
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1 or content[0].get("type") != "text":
        raise ValueError("Unsupported state result")
    value = json.loads(object_json(content[0]["text"]))
    labels = (
        {
            "on": "accesa",
            "off": "spenta",
            "unknown": "sconosciuto",
            "unavailable": "non disponibile",
        }
        if language == "it"
        else {"on": "on", "off": "off", "unknown": "unknown", "unavailable": "unavailable"}
    )
    if value.get("entity_id") != entity or value.get("state") not in labels:
        raise ValueError("Invalid state result")
    label = labels[value["state"]]
    if language == "it":
        return f"Home Assistant indica: {alias}, stato {label}."
    return f"Home Assistant reports: {alias}, state {label}."


def _read_aliases(settings, env):
    servers = {server.server_id: server for server in settings.servers}
    clock = servers.get("homeassistant")
    state = servers.get("homeassistant_state")
    if (
        clock is None
        or clock.tools != ("GetDateTime",)
        or clock.entities
        or clock.areas
        or set(servers) - {"homeassistant", "homeassistant_state"}
    ):
        raise ValueError("Unsupported read-only runtime scope")
    if state is None:
        if env.get("HELIOS_HA_READ_ALIASES"):
            raise ValueError("State aliases require explicit server scope")
        return {}
    aliases = json.loads(env.get("HELIOS_HA_READ_ALIASES", "{}"))
    if (
        state.tools != ("GetEntityState",)
        or state.areas
        or not state.entities
        or len(state.entities) > 20
        or any(not re.fullmatch(r"light\.[a-z0-9_]+", entity) for entity in state.entities)
        or not isinstance(aliases, dict)
        or len(aliases) != len(state.entities)
        or any(
            not isinstance(name, str)
            or not re.fullmatch(r"[^\W_][\w À-ÿ-]{0,63}", name)
            or name != " ".join(name.casefold().split())
            or not isinstance(entity, str)
            for name, entity in aliases.items()
        )
        or set(aliases.values()) != set(state.entities)
    ):
        raise ValueError("Invalid exact state alias scope")
    return aliases


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
    if not settings.enabled or language not in {"it", "en"}:
        raise ValueError("Unsupported read-only runtime scope")
    env = dict(os.environ if environ is None else environ)
    aliases = _read_aliases(settings, env)
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
            aliases=aliases,
        )
    finally:
        ledger.close()
