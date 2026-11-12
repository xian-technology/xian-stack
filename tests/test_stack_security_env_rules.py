"""Every rule in ensure_stack_security_env, reported together and in a fixed order."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

backend = importlib.import_module("backend")

PINNED = "@sha256:" + "a" * 64


def security_errors(tmp_path: Path, **env: str) -> list[str]:
    environment = {
        "XIAN_STACK_SECRETS_ENV": str(tmp_path / "secrets.env"),
        "XIAN_BDS_PASSWORD": "bds-secret",
        "XIAN_POSTGRAPHILE_PASSWORD": "graphql-secret",
        **env,
    }
    try:
        backend.ensure_stack_security_env(environment)
    except ValueError as exc:
        header, *lines = str(exc).splitlines()
        assert header == "xian-stack security configuration error:"
        return [line.removeprefix("  - ") for line in lines]
    return []


def test_safe_defaults_pass_and_fill_the_environment(tmp_path):
    environment = {"XIAN_STACK_SECRETS_ENV": str(tmp_path / "secrets.env")}

    result = backend.ensure_stack_security_env(environment)

    assert result is environment
    assert result["XIAN_COMETBFT_RPC_HOST"] == "127.0.0.1"
    assert result["XIAN_POSTGRAPHILE_USER"] == "xian_graphql"
    assert result["XIAN_GRAFANA_IMAGE"] == "grafana/grafana:12.4.5"
    assert result["XIAN_BDS_PASSWORD"]
    assert result["XIAN_POSTGRAPHILE_PASSWORD"] != result["XIAN_BDS_PASSWORD"]
    assert result["XIAN_STACK_SECRETS_ENV"] == str((tmp_path / "secrets.env").resolve())


def test_explicit_values_are_kept(tmp_path):
    result = backend.ensure_stack_security_env(
        {
            "XIAN_STACK_SECRETS_ENV": str(tmp_path / "secrets.env"),
            "XIAN_DASHBOARD_PORT": "9999",
            "XIAN_BDS_PASSWORD": "chosen",
        }
    )

    assert result["XIAN_DASHBOARD_PORT"] == "9999"
    assert result["XIAN_BDS_PASSWORD"] == "chosen"


@pytest.mark.parametrize(
    ("env", "errors"),
    [
        ({}, []),
        (
            {"XIAN_BDS_PASSWORD": "xian"},
            ["XIAN_BDS_PASSWORD cannot use the weak default value 'xian'"],
        ),
        (
            {"XIAN_POSTGRAPHILE_PASSWORD": "bds-secret"},
            ["XIAN_POSTGRAPHILE_PASSWORD must differ from XIAN_BDS_PASSWORD"],
        ),
        (
            {"XIAN_POSTGRAPHILE_USER": "xian"},
            ["XIAN_POSTGRAPHILE_USER must differ from XIAN_BDS_USER"],
        ),
        (
            {"XIAN_COMETBFT_RPC_HOST": "0.0.0.0"},
            ["Public RPC exposure requires XIAN_PUBLIC_RPC_ENABLED=1"],
        ),
        ({"XIAN_COMETBFT_RPC_HOST": "0.0.0.0", "XIAN_PUBLIC_RPC_ENABLED": "1"}, []),
        (
            {"XIAN_APP_METRICS_HOST": "0.0.0.0"},
            ["Public metrics exposure requires XIAN_PUBLIC_METRICS_ENABLED=1"],
        ),
        (
            {"XIAN_COMETBFT_METRICS_HOST": "0.0.0.0"},
            ["Public metrics exposure requires XIAN_PUBLIC_METRICS_ENABLED=1"],
        ),
        ({"XIAN_APP_METRICS_HOST": "0.0.0.0", "XIAN_PUBLIC_METRICS_ENABLED": "1"}, []),
        (
            {"XIAN_POSTGRAPHILE_HOST": "0.0.0.0"},
            ["Public PostGraphile exposure requires XIAN_BDS_ENABLED=1"],
        ),
        (
            {"XIAN_POSTGRAPHILE_HOST": "0.0.0.0", "XIAN_BDS_ENABLED": "1"},
            ["Public query exposure requires XIAN_PUBLIC_QUERY_ENABLED=1"],
        ),
        (
            {
                "XIAN_POSTGRAPHILE_HOST": "0.0.0.0",
                "XIAN_BDS_ENABLED": "1",
                "XIAN_PUBLIC_QUERY_ENABLED": "1",
            },
            [],
        ),
        ({"XIAN_DASHBOARD_HOST": "0.0.0.0"}, []),
        (
            {"XIAN_DASHBOARD_ENABLED": "1", "XIAN_DASHBOARD_HOST": "0.0.0.0"},
            ["Public dashboard exposure requires XIAN_PUBLIC_QUERY_ENABLED=1"],
        ),
        (
            {
                "XIAN_DASHBOARD_ENABLED": "1",
                "XIAN_DASHBOARD_HOST": "0.0.0.0",
                "XIAN_PUBLIC_QUERY_ENABLED": "1",
            },
            [],
        ),
        (
            {"XIAN_GRAFANA_HOST": "0.0.0.0"},
            [
                "Public monitoring exposure requires XIAN_PUBLIC_MONITORING_ENABLED=1",
                "Public monitoring exposure requires XIAN_MONITORING_PUBLIC_AUTH_CONFIRMED=1",
            ],
        ),
        (
            {"XIAN_PROMETHEUS_HOST": "0.0.0.0", "XIAN_PUBLIC_MONITORING_ENABLED": "1"},
            ["Public monitoring exposure requires XIAN_MONITORING_PUBLIC_AUTH_CONFIRMED=1"],
        ),
        (
            {
                "XIAN_PROMETHEUS_HOST": "0.0.0.0",
                "XIAN_PUBLIC_MONITORING_ENABLED": "1",
                "XIAN_MONITORING_PUBLIC_AUTH_CONFIRMED": "1",
            },
            [],
        ),
        (
            {"XIAN_DEX_AUTOMATION_ENABLED": "1", "XIAN_DEX_AUTOMATION_HOST": "0.0.0.0"},
            ["Public DEX automation exposure requires XIAN_PUBLIC_QUERY_ENABLED=1"],
        ),
        ({"XIAN_DEX_AUTOMATION_HOST": "0.0.0.0"}, []),
        (
            {
                "XIAN_REQUIRE_DIGEST_PINNED_THIRD_PARTY_IMAGES": "1",
                "XIAN_PROMETHEUS_IMAGE": "prom/prometheus:v3.12.0" + PINNED,
            },
            [
                "XIAN_POSTGRES_IMAGE must be digest-pinned when "
                "XIAN_REQUIRE_DIGEST_PINNED_THIRD_PARTY_IMAGES=1",
                "XIAN_GRAFANA_IMAGE must be digest-pinned when "
                "XIAN_REQUIRE_DIGEST_PINNED_THIRD_PARTY_IMAGES=1",
            ],
        ),
        (
            {
                "XIAN_BDS_PASSWORD": "xian",
                "XIAN_POSTGRAPHILE_USER": "xian",
                "XIAN_COMETBFT_RPC_HOST": "0.0.0.0",
                "XIAN_COMETBFT_METRICS_HOST": "0.0.0.0",
                "XIAN_POSTGRAPHILE_HOST": "0.0.0.0",
                "XIAN_DASHBOARD_ENABLED": "1",
                "XIAN_DASHBOARD_HOST": "0.0.0.0",
                "XIAN_GRAFANA_HOST": "0.0.0.0",
                "XIAN_DEX_AUTOMATION_ENABLED": "1",
                "XIAN_DEX_AUTOMATION_HOST": "0.0.0.0",
                "XIAN_REQUIRE_DIGEST_PINNED_THIRD_PARTY_IMAGES": "yes",
            },
            [
                "XIAN_BDS_PASSWORD cannot use the weak default value 'xian'",
                "XIAN_POSTGRAPHILE_USER must differ from XIAN_BDS_USER",
                "Public RPC exposure requires XIAN_PUBLIC_RPC_ENABLED=1",
                "Public metrics exposure requires XIAN_PUBLIC_METRICS_ENABLED=1",
                "Public PostGraphile exposure requires XIAN_BDS_ENABLED=1",
                "Public dashboard exposure requires XIAN_PUBLIC_QUERY_ENABLED=1",
                "Public monitoring exposure requires XIAN_PUBLIC_MONITORING_ENABLED=1",
                "Public monitoring exposure requires XIAN_MONITORING_PUBLIC_AUTH_CONFIRMED=1",
                "Public DEX automation exposure requires XIAN_PUBLIC_QUERY_ENABLED=1",
                "XIAN_POSTGRES_IMAGE must be digest-pinned when "
                "XIAN_REQUIRE_DIGEST_PINNED_THIRD_PARTY_IMAGES=1",
                "XIAN_PROMETHEUS_IMAGE must be digest-pinned when "
                "XIAN_REQUIRE_DIGEST_PINNED_THIRD_PARTY_IMAGES=1",
                "XIAN_GRAFANA_IMAGE must be digest-pinned when "
                "XIAN_REQUIRE_DIGEST_PINNED_THIRD_PARTY_IMAGES=1",
            ],
        ),
    ],
)
def test_security_rules(tmp_path, env, errors):
    assert security_errors(tmp_path, **env) == errors
