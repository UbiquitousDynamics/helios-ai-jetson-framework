import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from api.budget import BudgetError, BudgetLedger, BudgetLimits
from api.catalog import ModelPrice
from api.providers.contracts import Usage


def model_price():
    return ModelPrice(
        id="vendor/model",
        provider="vendor",
        model="model",
        input_per_million_usd=Decimal("1"),
        output_per_million_usd=Decimal("2"),
        context_window=4096,
        max_output_tokens=2048,
        free_tier=False,
    )


def fixed_clock():
    return datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def test_each_attempt_is_reserved_and_exact_usage_is_settled(tmp_path):
    path = tmp_path / "usage.jsonl"
    ledger = BudgetLedger(
        path,
        BudgetLimits(
            per_request_usd=Decimal("0.01"),
            daily_usd=Decimal("0.02"),
            monthly_usd=Decimal("0.03"),
        ),
        clock=fixed_clock,
    )
    reservation = ledger.reserve(
        provider="vendor",
        model="model",
        attempt_id="attempt-1",
        price=model_price(),
        estimated_input_tokens=1000,
        max_output_tokens=1000,
    )
    assert reservation.reserved_usd == Decimal("0.003")

    settlement = ledger.settle(
        reservation.reservation_id,
        usage=Usage(input_tokens=100, output_tokens=10),
        price=model_price(),
    )
    assert settlement.charged_usd == Decimal("0.00012")
    assert settlement.conservative is False
    assert ledger.snapshot().daily_usd == Decimal("0.00012")

    reopened = BudgetLedger(path, ledger.limits, clock=fixed_clock)
    assert reopened.snapshot() == ledger.snapshot()
    with pytest.raises(BudgetError, match="attempt"):
        reopened.reserve(
            provider="vendor",
            model="model",
            attempt_id="attempt-1",
            amount_usd="0",
        )


def test_missing_usage_settles_full_reservation_and_records_no_content(tmp_path):
    path = tmp_path / "usage.jsonl"
    ledger = BudgetLedger(path, BudgetLimits(), clock=fixed_clock)
    reservation = ledger.reserve(
        provider="vendor",
        model="model",
        amount_usd="0.004",
    )

    settlement = ledger.settle(reservation.reservation_id)

    assert settlement.conservative
    assert settlement.charged_usd == Decimal("0.004")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert all("prompt" not in key and "hash" not in key for row in records for key in row)


def test_limits_and_corrupt_ledger_fail_closed(tmp_path):
    path = tmp_path / "usage.jsonl"
    ledger = BudgetLedger(
        path,
        BudgetLimits(daily_usd=Decimal("0.005")),
        clock=fixed_clock,
    )
    ledger.reserve(provider="vendor", model="model", amount_usd="0.004")
    with pytest.raises(BudgetError, match="daily"):
        ledger.reserve(provider="vendor", model="model", amount_usd="0.002")

    zero_path = tmp_path / "zero.jsonl"
    zero = BudgetLedger(
        zero_path,
        BudgetLimits(zero_cost_only=True),
        clock=fixed_clock,
    )
    with pytest.raises(BudgetError, match="zero-cost"):
        zero.reserve(provider="vendor", model="model", amount_usd="0.000001")

    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text('{"schema_version":1}', encoding="utf-8")
    with pytest.raises(BudgetError, match="continuity"):
        BudgetLedger(corrupt, BudgetLimits(), clock=fixed_clock)


def test_clock_rollback_fails_closed(tmp_path):
    path = tmp_path / "usage.jsonl"

    def later():
        return datetime(2026, 7, 28, tzinfo=timezone.utc)

    ledger = BudgetLedger(path, BudgetLimits(), clock=later)
    ledger.reserve(provider="vendor", model="model", amount_usd="0")

    def earlier():
        return datetime(2026, 7, 27, tzinfo=timezone.utc)

    with pytest.raises(BudgetError, match="clock moved backwards"):
        BudgetLedger(path, BudgetLimits(), clock=earlier)


def test_separate_ledger_instances_reload_under_the_file_lock(tmp_path):
    path = tmp_path / "usage.jsonl"
    limits = BudgetLimits(daily_usd=Decimal("0.005"))
    first = BudgetLedger(path, limits, clock=fixed_clock)
    second = BudgetLedger(path, limits, clock=fixed_clock)

    first.reserve(provider="vendor", model="model", amount_usd="0.004")

    with pytest.raises(BudgetError, match="daily"):
        second.reserve(provider="vendor", model="model", amount_usd="0.002")


def test_unreserved_settlement_is_recorded_then_blocks_future_spending(tmp_path):
    path = tmp_path / "usage.jsonl"
    limits = BudgetLimits(
        per_request_usd=Decimal("0.01"),
        daily_usd=Decimal("0.01"),
        monthly_usd=Decimal("0.01"),
    )
    ledger = BudgetLedger(path, limits, clock=fixed_clock)
    reservation = ledger.reserve(provider="vendor", model="model", amount_usd="0.001")

    settlement = ledger.settle(
        reservation.reservation_id,
        actual_amount_usd="1",
    )

    assert settlement.charged_usd == Decimal("1")
    assert ledger.snapshot().daily_usd == Decimal("1")
    assert ledger.spending_blocked
    with pytest.raises(BudgetError, match="fail-closed"):
        ledger.reserve(provider="vendor", model="model", amount_usd="0")
    assert BudgetLedger(path, limits, clock=fixed_clock).spending_blocked


def test_malformed_usage_is_settled_conservatively_without_corrupting_ledger(tmp_path):
    path = tmp_path / "usage.jsonl"
    ledger = BudgetLedger(path, BudgetLimits(), clock=fixed_clock)
    reservation = ledger.reserve(
        provider="vendor",
        model="model",
        price=model_price(),
        estimated_input_tokens=100,
        max_output_tokens=10,
    )

    settlement = ledger.settle(
        reservation.reservation_id,
        usage=Usage(input_tokens=-1, output_tokens=2),
        price=model_price(),
    )

    assert settlement.conservative
    assert BudgetLedger(path, BudgetLimits(), clock=fixed_clock).snapshot().daily_usd == (
        reservation.reserved_usd
    )


def test_actual_charge_cannot_write_unvalidated_usage_content(tmp_path):
    path = tmp_path / "usage.jsonl"
    ledger = BudgetLedger(path, BudgetLimits(), clock=fixed_clock)
    reservation = ledger.reserve(provider="vendor", model="model", amount_usd="0.001")

    ledger.settle(
        reservation.reservation_id,
        actual_amount_usd="0.001",
        usage=Usage(input_tokens="private-content"),
    )

    assert "private-content" not in path.read_text(encoding="utf-8")
    BudgetLedger(path, BudgetLimits(), clock=fixed_clock)


@pytest.mark.parametrize("mutation", ["delete", "truncate", "modify"])
def test_continuity_state_detects_missing_or_changed_ledger(tmp_path, mutation):
    path = tmp_path / f"{mutation}.jsonl"
    ledger = BudgetLedger(path, BudgetLimits(), clock=fixed_clock)
    ledger.reserve(provider="vendor", model="model", amount_usd="0")

    if mutation == "delete":
        path.unlink()
    elif mutation == "truncate":
        path.write_bytes(b"")
    else:
        data = bytearray(path.read_bytes())
        data[0] = ord("[")
        path.write_bytes(data)

    with pytest.raises(BudgetError, match="continuity|size|integrity"):
        BudgetLedger(path, BudgetLimits(), clock=fixed_clock)
