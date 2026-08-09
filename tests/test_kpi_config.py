from pathlib import Path

import pytest

import config


def test_llm_target_keeps_new_tier_after_all_legacy_positional_fields() -> None:
    target = config.LLMTargetSettings(
        "remote",
        "provider",
        "model",
        (),
        None,
        ("en",),
        4_096,
        256,
        80,
        2,
        3,
        (("temperature", 0),),
    )

    assert target.languages == ("en",)
    assert target.context_window == 4_096
    assert target.options == (("temperature", 0),)
    assert target.tier is None


def test_kpi_defaults_are_disabled_and_project_rooted(tmp_path: Path) -> None:
    settings = config.Settings.from_env(tmp_path, environ={})

    assert settings.kpi.enabled is False
    assert settings.kpi.dashboard_enabled is False
    assert settings.kpi.dashboard_host == "127.0.0.1"
    assert settings.kpi.dashboard_port == 8765
    assert settings.kpi.storage_path == (tmp_path / "logs/helios-kpi.sqlite3").resolve()


def test_kpi_environment_overrides_are_typed_and_project_rooted(tmp_path: Path) -> None:
    settings = config.Settings.from_env(
        tmp_path,
        environ={
            "HELIOS_KPI_ENABLED": "true",
            "HELIOS_KPI_STORAGE_PATH": "state/kpi.sqlite3",
            "HELIOS_KPI_QUEUE_SIZE": "512",
            "HELIOS_KPI_BATCH_SIZE": "32",
            "HELIOS_KPI_FLUSH_INTERVAL_SECONDS": "0.2",
            "HELIOS_KPI_RAW_RETENTION_DAYS": "7",
            "HELIOS_KPI_ROLLUP_RETENTION_DAYS": "30",
            "HELIOS_KPI_MAX_DATABASE_MB": "64",
            "HELIOS_KPI_RESOURCE_INTERVAL_SECONDS": "2.5",
            "HELIOS_KPI_DASHBOARD_ENABLED": "true",
            "HELIOS_KPI_DASHBOARD_PORT": "9000",
        },
    )

    assert settings.kpi.enabled is True
    assert settings.kpi.storage_path == (tmp_path / "state/kpi.sqlite3").resolve()
    assert settings.kpi.queue_size == 512
    assert settings.kpi.batch_size == 32
    assert settings.kpi.flush_interval_seconds == pytest.approx(0.2)
    assert settings.kpi.resource_sample_interval_seconds == pytest.approx(2.5)
    assert settings.kpi.dashboard_port == 9000


def test_invalid_kpi_environment_fails_closed_without_affecting_llm(tmp_path: Path) -> None:
    settings = config.Settings.from_env(
        tmp_path,
        environ={
            "HELIOS_KPI_ENABLED": "true",
            "HELIOS_KPI_QUEUE_SIZE": "4",
            "HELIOS_KPI_BATCH_SIZE": "8",
        },
    )

    assert settings.kpi.enabled is False
    assert settings.llm.emergency_local_only is False


def test_non_loopback_dashboard_requires_explicit_lan_and_authentication() -> None:
    with pytest.raises(config.ConfigurationError, match="dashboard_allow_lan"):
        config.KPISettings(dashboard_enabled=True, dashboard_host="0.0.0.0")

    with pytest.raises(config.ConfigurationError, match="authentication"):
        config.KPISettings(
            dashboard_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_allow_lan=True,
        )

    settings = config.KPISettings(
        dashboard_enabled=True,
        dashboard_host="0.0.0.0",
        dashboard_allow_lan=True,
        dashboard_auth_token_env="HELIOS_KPI_SECRET",
    )
    assert settings.dashboard_auth_token_env == "HELIOS_KPI_SECRET"
    assert "secret-value" not in repr(settings)


@pytest.mark.parametrize(
    "values",
    [
        {"queue_size": 0},
        {"batch_size": 3, "queue_size": 2},
        {"flush_interval_seconds": float("inf")},
        {"raw_retention_days": 20, "rollup_retention_days": 10},
        {"dashboard_port": 65536},
        {"maximum_query_points": True},
        {"maximum_query_points": 1_001},
        {"maximum_query_days": 91},
        {"maximum_export_rows": 100_001},
    ],
)
def test_kpi_settings_reject_invalid_bounds(values: dict[str, object]) -> None:
    with pytest.raises(config.ConfigurationError):
        config.KPISettings(**values)
