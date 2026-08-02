"""Append-only, content-free budget reservations for provider attempts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from api._strict_json import duplicate_key_rejecting_hook
from api.catalog import ModelPrice
from api.providers.contracts import Usage


class BudgetError(RuntimeError):
    """Budget state is unsafe or a configured limit would be exceeded."""


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    per_request_usd: Decimal | None = None
    daily_usd: Decimal | None = None
    monthly_usd: Decimal | None = None
    zero_cost_only: bool = False

    def __post_init__(self) -> None:
        for name in ("per_request_usd", "daily_usd", "monthly_usd"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, Decimal):
                    raise TypeError(f"{name} must be a Decimal")
                if not value.is_finite() or value < 0:
                    raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: str
    attempt_id: str
    provider: str
    model: str
    created_at: datetime
    reserved_usd: Decimal


@dataclass(frozen=True, slots=True)
class Settlement:
    reservation_id: str
    settled_at: datetime
    charged_usd: Decimal
    conservative: bool


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    daily_usd: Decimal
    monthly_usd: Decimal
    outstanding_usd: Decimal


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")
_RESERVE_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "reservation_id",
        "attempt_id",
        "provider",
        "model",
        "created_at",
        "reserved_usd",
    }
)
_SETTLE_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "reservation_id",
        "settled_at",
        "charged_usd",
        "conservative",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
)


_strict_json_object = duplicate_key_rejecting_hook(
    BudgetError,
    "budget ledger contains a duplicate record key",
)


class BudgetLedger:
    """Fail-closed ledger with locked, durable, append-only events."""

    def __init__(
        self,
        path: str | Path,
        limits: BudgetLimits,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._state_path = self.path.with_name(f"{self.path.name}.state")
        self.limits = limits
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._broken = False
        self._spending_blocked = False
        self._reservations: dict[str, Reservation] = {}
        self._attempt_ids: set[str] = set()
        self._settlements: dict[str, Settlement] = {}
        self._latest_timestamp: datetime | None = None
        with self._process_lock():
            self._load()
            self._refresh_policy_status()
            if self._latest_timestamp is not None:
                self._now()

    def reserve(
        self,
        *,
        provider: str,
        model: str,
        attempt_id: str | None = None,
        price: ModelPrice | None = None,
        estimated_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        amount_usd: Decimal | str | None = None,
    ) -> Reservation:
        provider = self._identifier(provider, "provider")
        model = self._identifier(model, "model")
        attempt_id = self._identifier(attempt_id or uuid.uuid4().hex, "attempt_id")
        if (price is None) == (amount_usd is None):
            raise ValueError("provide exactly one of price or amount_usd")
        if price is not None:
            if estimated_input_tokens is None or max_output_tokens is None:
                raise ValueError("price-based reservations require input and maximum output tokens")
            self._match_price(price, provider, model)
            if max_output_tokens > price.max_output_tokens:
                raise BudgetError("requested output exceeds the catalog model limit")
            amount = price.estimate(
                input_tokens=self._tokens(estimated_input_tokens),
                output_tokens=self._tokens(max_output_tokens),
            )
        else:
            amount = self._amount(amount_usd)

        with self._lock:
            self._ensure_healthy()
            with self._process_lock():
                self._reload()
                self._ensure_can_spend()
                if attempt_id in self._attempt_ids:
                    raise BudgetError("attempt id already has a budget reservation")
                now = self._now()
                self._check_limits(amount, now)
                reservation = Reservation(
                    reservation_id=uuid.uuid4().hex,
                    attempt_id=attempt_id,
                    provider=provider,
                    model=model,
                    created_at=now,
                    reserved_usd=amount,
                )
                record = {
                    "schema_version": 1,
                    "event": "reserve",
                    "reservation_id": reservation.reservation_id,
                    "attempt_id": attempt_id,
                    "provider": provider,
                    "model": model,
                    "created_at": self._format_time(now),
                    "reserved_usd": str(amount),
                }
                self._append(record)
                self._reservations[reservation.reservation_id] = reservation
                self._attempt_ids.add(attempt_id)
                self._latest_timestamp = now
                return reservation

    def settle(
        self,
        reservation_id: str,
        *,
        usage: Usage | None = None,
        price: ModelPrice | None = None,
        actual_amount_usd: Decimal | str | None = None,
    ) -> Settlement:
        reservation_id = self._identifier(reservation_id, "reservation_id")
        with self._lock:
            self._ensure_healthy()
            with self._process_lock():
                self._reload()
                try:
                    reservation = self._reservations[reservation_id]
                except KeyError:
                    raise BudgetError("unknown budget reservation") from None
                if reservation_id in self._settlements:
                    raise BudgetError("budget reservation is already settled")

                usage_record, usage_valid = self._usage_record(usage)
                if actual_amount_usd is not None:
                    charged = self._amount(actual_amount_usd)
                    conservative = False
                elif usage is not None and price is not None:
                    self._match_price(price, reservation.provider, reservation.model)
                    try:
                        estimated = (
                            price.estimate_usage(Usage(**usage_record)) if usage_valid else None
                        )
                    except ValueError:
                        estimated = None
                    conservative = estimated is None
                    charged = reservation.reserved_usd if estimated is None else estimated
                else:
                    conservative = True
                    charged = reservation.reserved_usd

                now = self._now()
                settlement = Settlement(
                    reservation_id=reservation_id,
                    settled_at=now,
                    charged_usd=charged,
                    conservative=conservative,
                )
                record = {
                    "schema_version": 1,
                    "event": "settle",
                    "reservation_id": reservation_id,
                    "settled_at": self._format_time(now),
                    "charged_usd": str(charged),
                    "conservative": conservative,
                    **usage_record,
                }
                self._append(record)
                self._settlements[reservation_id] = settlement
                self._latest_timestamp = now
                self._refresh_policy_status()
                return settlement

    def snapshot(self, *, at: datetime | None = None) -> BudgetSnapshot:
        with self._lock:
            self._ensure_healthy()
            with self._process_lock():
                self._reload()
                now = self._normalize_time(at) if at is not None else self._now()
                return self._snapshot(now)

    @property
    def spending_blocked(self) -> bool:
        """Whether prior ledger events prohibit any new remote reservation."""

        with self._lock:
            self._ensure_healthy()
            with self._process_lock():
                self._reload()
                return self._spending_blocked

    def _check_limits(self, amount: Decimal, now: datetime) -> None:
        if self.limits.zero_cost_only and amount != 0:
            raise BudgetError("zero-cost-only policy rejects this provider attempt")
        if self.limits.per_request_usd is not None and amount > self.limits.per_request_usd:
            raise BudgetError("per-request budget would be exceeded")
        snapshot = self._snapshot(now)
        if self.limits.daily_usd is not None and (
            snapshot.daily_usd + amount > self.limits.daily_usd
        ):
            raise BudgetError("daily budget would be exceeded")
        if self.limits.monthly_usd is not None and (
            snapshot.monthly_usd + amount > self.limits.monthly_usd
        ):
            raise BudgetError("monthly budget would be exceeded")

    def _snapshot(self, now: datetime) -> BudgetSnapshot:
        daily = Decimal(0)
        monthly = Decimal(0)
        outstanding = Decimal(0)
        for reservation_id, reservation in self._reservations.items():
            settlement = self._settlements.get(reservation_id)
            amount = settlement.charged_usd if settlement is not None else reservation.reserved_usd
            if settlement is None:
                outstanding += reservation.reserved_usd
            if reservation.created_at.date() == now.date():
                daily += amount
            if (
                reservation.created_at.year == now.year
                and reservation.created_at.month == now.month
            ):
                monthly += amount
        return BudgetSnapshot(daily, monthly, outstanding)

    def _reload(self) -> None:
        self._reservations.clear()
        self._attempt_ids.clear()
        self._settlements.clear()
        self._latest_timestamp = None
        self._spending_blocked = False
        self._load()
        self._refresh_policy_status()

    def _refresh_policy_status(self) -> None:
        daily: dict[object, Decimal] = {}
        monthly: dict[tuple[int, int], Decimal] = {}
        breached = False
        for reservation_id, reservation in self._reservations.items():
            settlement = self._settlements.get(reservation_id)
            amount = settlement.charged_usd if settlement is not None else reservation.reserved_usd
            if settlement is not None and settlement.charged_usd > reservation.reserved_usd:
                breached = True
            if self.limits.zero_cost_only and amount != 0:
                breached = True
            if self.limits.per_request_usd is not None and amount > self.limits.per_request_usd:
                breached = True
            day = reservation.created_at.date()
            month = (reservation.created_at.year, reservation.created_at.month)
            daily[day] = daily.get(day, Decimal(0)) + amount
            monthly[month] = monthly.get(month, Decimal(0)) + amount
        if self.limits.daily_usd is not None and any(
            amount > self.limits.daily_usd for amount in daily.values()
        ):
            breached = True
        if self.limits.monthly_usd is not None and any(
            amount > self.limits.monthly_usd for amount in monthly.values()
        ):
            breached = True
        self._spending_blocked = breached

    def _load(self) -> None:
        try:
            self._verify_continuity()
            if not self.path.exists():
                return
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.endswith("\n"):
                        raise BudgetError("budget ledger has a truncated final record")
                    payload = json.loads(line, object_pairs_hook=_strict_json_object)
                    self._apply_loaded(payload, line_number)
        except BudgetError:
            self._broken = True
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            self._broken = True
            raise BudgetError("budget ledger is unreadable or corrupt") from None

    def _apply_loaded(self, payload: Mapping[str, Any], line_number: int) -> None:
        if (
            not isinstance(payload, Mapping)
            or isinstance(payload.get("schema_version"), bool)
            or payload.get("schema_version") != 1
        ):
            raise BudgetError(f"invalid budget record at line {line_number}")
        event = payload.get("event")
        expected = _RESERVE_KEYS if event == "reserve" else _SETTLE_KEYS
        if event not in {"reserve", "settle"} or frozenset(payload) != expected:
            raise BudgetError(f"invalid budget record at line {line_number}")
        if event == "reserve":
            reservation_id = self._identifier(payload["reservation_id"], "reservation_id")
            attempt_id = self._identifier(payload["attempt_id"], "attempt_id")
            if reservation_id in self._reservations or attempt_id in self._attempt_ids:
                raise BudgetError("duplicate budget reservation")
            created_at = self._parse_time(payload["created_at"])
            self._check_loaded_timestamp(created_at)
            reservation = Reservation(
                reservation_id=reservation_id,
                attempt_id=attempt_id,
                provider=self._identifier(payload["provider"], "provider"),
                model=self._identifier(payload["model"], "model"),
                created_at=created_at,
                reserved_usd=self._amount(payload["reserved_usd"]),
            )
            self._reservations[reservation_id] = reservation
            self._attempt_ids.add(attempt_id)
            self._latest_timestamp = created_at
            return

        reservation_id = self._identifier(payload["reservation_id"], "reservation_id")
        if reservation_id not in self._reservations or reservation_id in self._settlements:
            raise BudgetError("orphaned or duplicate budget settlement")
        if not isinstance(payload["conservative"], bool):
            raise BudgetError("invalid conservative flag in budget ledger")
        for token_field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            value = payload[token_field]
            if value is not None:
                self._tokens(value)
        settled_at = self._parse_time(payload["settled_at"])
        self._check_loaded_timestamp(settled_at)
        settlement = Settlement(
            reservation_id=reservation_id,
            settled_at=settled_at,
            charged_usd=self._amount(payload["charged_usd"]),
            conservative=payload["conservative"],
        )
        self._settlements[reservation_id] = settlement
        self._latest_timestamp = settled_at

    def _append(self, payload: Mapping[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = (
                json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
            ).encode("ascii")
            descriptor = os.open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                written = os.write(descriptor, data)
                if written != len(data):
                    raise OSError("partial ledger write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._write_continuity_state()
        except OSError:
            self._broken = True
            raise BudgetError("budget ledger could not be durably updated") from None

    def _verify_continuity(self) -> None:
        ledger_exists = self.path.exists()
        state_exists = self._state_path.exists()
        if not ledger_exists and not state_exists:
            return
        if ledger_exists != state_exists:
            raise BudgetError("budget ledger continuity metadata is missing")
        try:
            raw_state = self._state_path.read_text(encoding="ascii")
            if not raw_state.endswith("\n"):
                raise BudgetError("budget ledger continuity metadata is truncated")
            state = json.loads(raw_state, object_pairs_hook=_strict_json_object)
            if (
                not isinstance(state, Mapping)
                or frozenset(state) != {"schema_version", "ledger_size", "sha256"}
                or isinstance(state.get("schema_version"), bool)
                or state.get("schema_version") != 1
                or isinstance(state.get("ledger_size"), bool)
                or not isinstance(state.get("ledger_size"), int)
                or state["ledger_size"] < 0
                or not isinstance(state.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", state["sha256"]) is None
            ):
                raise BudgetError("budget ledger continuity metadata is invalid")
            if self.path.stat().st_size != state["ledger_size"]:
                raise BudgetError("budget ledger size does not match its continuity metadata")
            if self._ledger_digest() != state["sha256"]:
                raise BudgetError("budget ledger integrity check failed")
        except BudgetError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise BudgetError("budget ledger continuity metadata is unreadable") from None

    def _write_continuity_state(self) -> None:
        temporary_path = self._state_path.with_name(
            f"{self._state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        state = {
            "schema_version": 1,
            "ledger_size": self.path.stat().st_size,
            "sha256": self._ledger_digest(),
        }
        data = (
            json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("ascii")
        descriptor = os.open(
            temporary_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            written = os.write(descriptor, data)
            if written != len(data):
                raise OSError("partial continuity-state write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary_path, self._state_path)

    def _ledger_digest(self) -> str:
        digest = hashlib.sha256()
        with self.path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        handle = None
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except BudgetError:
            raise
        except OSError:
            self._broken = True
            raise BudgetError("budget ledger lock could not be acquired") from None
        finally:
            if handle is not None:
                handle.close()

    def _ensure_healthy(self) -> None:
        if self._broken:
            raise BudgetError("budget ledger is in a fail-closed state")

    def _ensure_can_spend(self) -> None:
        self._ensure_healthy()
        if self._spending_blocked:
            raise BudgetError("prior spend placed the budget ledger in a fail-closed state")

    def _now(self) -> datetime:
        current = self._normalize_time(self._clock())
        if self._latest_timestamp is not None and current < self._latest_timestamp:
            self._broken = True
            raise BudgetError("budget clock moved backwards; remote spending is disabled")
        return current

    def _check_loaded_timestamp(self, value: datetime) -> None:
        if self._latest_timestamp is not None and value < self._latest_timestamp:
            raise BudgetError("budget ledger timestamps move backwards")

    @staticmethod
    def _normalize_time(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BudgetError("budget clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @classmethod
    def _parse_time(cls, value: Any) -> datetime:
        if not isinstance(value, str):
            raise BudgetError("invalid timestamp in budget ledger")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise BudgetError("invalid timestamp in budget ledger") from None
        return cls._normalize_time(parsed)

    @staticmethod
    def _format_time(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _identifier(value: Any, label: str) -> str:
        if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
            raise ValueError(f"{label} contains unsafe characters")
        return value

    @staticmethod
    def _tokens(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("token counts must be non-negative integers")
        return value

    @staticmethod
    def _amount(value: Any) -> Decimal:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("currency amounts must be decimal strings or Decimal values")
        try:
            amount = Decimal(value)
        except InvalidOperation:
            raise ValueError("invalid currency amount") from None
        if not amount.is_finite() or amount < 0:
            raise ValueError("currency amount must be finite and non-negative")
        return amount

    @staticmethod
    def _match_price(price: ModelPrice, provider: str, model: str) -> None:
        if price.provider != provider or price.model != model:
            raise BudgetError("catalog price does not match the provider attempt")

    @classmethod
    def _usage_record(cls, usage: Usage | None) -> tuple[dict[str, int | None], bool]:
        names = (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
        )
        empty = {name: None for name in names}
        if usage is None:
            return empty, True
        try:
            record = {
                name: None if getattr(usage, name) is None else cls._tokens(getattr(usage, name))
                for name in names
            }
        except (TypeError, ValueError):
            return empty, False
        if (
            record["cached_input_tokens"] is not None
            and record["input_tokens"] is not None
            and record["cached_input_tokens"] > record["input_tokens"]
        ):
            return empty, False
        if (
            record["reasoning_tokens"] is not None
            and record["output_tokens"] is not None
            and record["reasoning_tokens"] > record["output_tokens"]
        ):
            return empty, False
        return record, True


__all__ = [
    "BudgetError",
    "BudgetLedger",
    "BudgetLimits",
    "BudgetSnapshot",
    "Reservation",
    "Settlement",
]
