import json
from datetime import date
from decimal import Decimal

import pytest

from api.catalog import CatalogError, ModelCatalog
from api.providers.contracts import Usage


def catalog_payload():
    return {
        "schema_version": 1,
        "catalog_revision": "2026-07-27",
        "verified_on": "2026-07-27",
        "expires_on": "2026-08-27",
        "models": [
            {
                "id": "vendor/model",
                "provider": "vendor",
                "model": "model",
                "input_per_million_usd": "0.10",
                "output_per_million_usd": "0.40",
                "cached_input_per_million_usd": "0.05",
                "reasoning_output_per_million_usd": "0.50",
                "context_window": 4096,
                "max_output_tokens": 512,
                "free_tier": False,
            }
        ],
    }


def test_strict_catalog_parses_decimal_rates_and_estimates_usage():
    catalog = ModelCatalog.from_mapping(catalog_payload(), today=date(2026, 8, 1))
    price = catalog.get("vendor/model")

    assert price.input_per_million_usd == Decimal("0.10")
    assert price.estimate(
        input_tokens=1000,
        cached_input_tokens=200,
        output_tokens=100,
        reasoning_tokens=20,
    ) == Decimal("0.000132")
    assert price.estimate_usage(Usage(input_tokens=1000, output_tokens=100)) == Decimal("0.000140")
    assert price.estimate_usage(Usage(total_tokens=100)) is None


def test_stale_missing_and_unknown_fields_fail_closed():
    catalog = ModelCatalog.from_mapping(catalog_payload(), today=date(2026, 9, 1))
    assert catalog.is_stale()
    with pytest.raises(CatalogError, match="stale"):
        catalog.get("vendor/model")
    with pytest.raises(CatalogError, match="future"):
        ModelCatalog.from_mapping(catalog_payload(), today=date(2026, 1, 1)).get("vendor/model")
    with pytest.raises(CatalogError, match="absent"):
        catalog.get("missing", require_fresh=False)

    payload = catalog_payload()
    payload["unexpected"] = True
    with pytest.raises(CatalogError, match="unknown"):
        ModelCatalog.from_mapping(payload)


def test_rates_must_be_json_strings_and_dates_are_ordered():
    payload = catalog_payload()
    payload["models"][0]["input_per_million_usd"] = 0.1
    with pytest.raises(CatalogError, match="decimal string"):
        ModelCatalog.from_mapping(payload)

    payload = catalog_payload()
    payload["expires_on"] = "2026-01-01"
    with pytest.raises(CatalogError, match="precedes"):
        ModelCatalog.from_mapping(payload)


def test_load_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1,'
        '"catalog_revision":"r","verified_on":"2026-01-01",'
        '"expires_on":"2026-02-01","models":[]}',
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="duplicate"):
        ModelCatalog.load(path)

    path.write_text(json.dumps(catalog_payload()), encoding="utf-8")
    assert ModelCatalog.load(path, today=date(2026, 8, 1)).model_ids == ("vendor/model",)


def test_free_tier_label_does_not_override_verified_token_prices():
    payload = catalog_payload()
    payload["models"][0]["free_tier"] = True
    price = ModelCatalog.from_mapping(payload, today=date(2026, 8, 1)).get(
        "vendor/model"
    )

    assert price.free_tier
    assert price.estimate(input_tokens=1000, output_tokens=100) > 0
