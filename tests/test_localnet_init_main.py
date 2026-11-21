"""`localnet-init.py` end to end: settings from the environment reach the summary."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).absolute().parents[1]
SPEC = importlib.util.spec_from_file_location("main_localnet", ROOT / "scripts/localnet-init.py")
localnet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(localnet)

LOCALNET_ENV_PREFIXES = ("XIAN_LOCALNET_", "XIAN_PERF_", "XIAN_TX_FEE_MODE", "XIAN_FREE_")


@pytest.fixture
def run_init(tmp_path, monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith(LOCALNET_ENV_PREFIXES):
            monkeypatch.delenv(name)
    monkeypatch.setattr(localnet, "STACK_DIR", tmp_path)
    monkeypatch.setattr(localnet, "LOCALNET_DIR", tmp_path / ".localnet")

    def run(*argv: str, **env: str) -> dict:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(sys, "argv", ["localnet-init.py", *argv])
        localnet.main()
        return json.loads((tmp_path / ".localnet" / "network.json").read_text())

    return run


def test_default_profile_summary(run_init, tmp_path):
    summary = run_init("--chain-id", "xian-pin-1")

    assert summary["chain_id"] == "xian-pin-1"
    assert summary["genesis_network"] == "local"
    assert summary["runtime_features"] == {"zk": True}
    assert summary["localnet_profile"] == "default"
    assert summary["consensus"] == {
        "block_policy_mode": "periodic",
        "block_policy_interval": "5s",
        "timeout_propose": None,
        "timeout_propose_delta": None,
        "timeout_prevote": None,
        "timeout_prevote_delta": None,
        "timeout_precommit": None,
        "timeout_precommit_delta": None,
        "timeout_commit": None,
        "skip_timeout_commit": None,
    }
    assert summary["mempool"] == {"size": None, "cache_size": None}
    assert summary["bds"] == {"enabled": False, "bds_node_index": None, "bds_rpc_url": None}
    assert summary["port_offset"] == 0
    assert summary["profiling"] == {"enabled": False, "recent_blocks": 32}
    assert summary["logging"] == {
        "transaction_trace_logging": False,
        "app_log_level": "INFO",
        "app_log_json": False,
        "app_log_rotation_hours": 1,
        "app_log_retention_days": 7,
        "app_log_max_file_mb": 50,
        "app_log_max_total_mb": 250,
    }
    assert "parallel_execution" not in summary
    nodes = summary["nodes"]
    assert [n["moniker"] for n in nodes] == ["node-0", "node-1", "node-2", "node-3"]
    assert [n["host_rpc_port"] for n in nodes] == [26657, 26757, 26857, 26957]
    assert nodes[1]["host_p2p_port"] == 26756
    assert nodes[1]["host_metrics_port"] == 26760
    assert nodes[1]["host_xian_metrics_port"] == 9208
    assert nodes[2]["abci_container"] == nodes[2]["cometbft_container"] == "xian-node-2"
    assert summary["founder_key"] == nodes[0]["account_private_key"]
    assert not any(n["bds_enabled"] for n in nodes)
    for node in nodes:
        assert (tmp_path / ".localnet" / node["moniker"] / "tmp").is_dir()
    assert (tmp_path / "docker-compose-localnet.yml").is_file()


def test_localnet_genesis_exercises_official_040_authority_in_every_init_chain(run_init, tmp_path):
    run_init("--nodes", "5", "--chain-id", "xian-authority-test")
    genesis = [
        json.loads((tmp_path / f".localnet/node-{i}/.cometbft/config/genesis.json").read_text())
        for i in range(5)
    ]
    assert all(value == genesis[0] for value in genesis)
    assert genesis[0]["consensus_params"]["authority"] == {"authority": "xian-localnet-e2e"}
    assert len(genesis[0]["app_hash"]) == 64


def test_image_override_reaches_every_generated_node_service(tmp_path, monkeypatch):
    image = "xian-review-current-source:isolated"
    monkeypatch.setenv("XIAN_NODE_INTEGRATED_IMAGE", image)
    spec = importlib.util.spec_from_file_location("image_override_localnet", SPEC.origin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STACK_DIR", tmp_path)
    monkeypatch.setattr(module, "LOCALNET_DIR", tmp_path / ".localnet")
    monkeypatch.setattr(sys, "argv", ["localnet-init.py"])

    module.main()

    services = json.loads((tmp_path / "docker-compose-localnet.yml").read_text())["services"]
    assert services
    assert {service["image"] for service in services.values()} == {image}


def test_throughput_profile_fills_unset_consensus_and_mempool(run_init):
    summary = run_init(
        "--nodes",
        "5",
        XIAN_LOCALNET_PROFILE="throughput",
        XIAN_LOCALNET_CONSENSUS_TIMEOUT_COMMIT="900ms",
        XIAN_LOCALNET_MEMPOOL_SIZE="7",
        XIAN_LOCALNET_SKIP_TIMEOUT_COMMIT="no",
        XIAN_LOCALNET_ENABLE_BDS="1",
        XIAN_LOCALNET_BDS_NODE_INDEX="4",
        XIAN_LOCALNET_PORT_OFFSET="1000",
        XIAN_PERF_ENABLED="yes",
        XIAN_LOCALNET_APP_LOG_JSON="true",
    )

    assert summary["consensus"] == {
        "block_policy_mode": "on_demand",
        "block_policy_interval": "0s",
        "timeout_propose": "500ms",
        "timeout_propose_delta": "100ms",
        "timeout_prevote": "200ms",
        "timeout_prevote_delta": "50ms",
        "timeout_precommit": "200ms",
        "timeout_precommit_delta": "50ms",
        "timeout_commit": "900ms",
        "skip_timeout_commit": False,
    }
    assert summary["mempool"] == {"size": 7, "cache_size": 100_000}
    assert summary["port_offset"] == 1000
    assert summary["bds"] == {
        "enabled": True,
        "bds_node_index": 4,
        "bds_rpc_url": "http://127.0.0.1:28057",
    }
    assert [n["bds_enabled"] for n in summary["nodes"]] == [False, False, False, False, True]
    assert summary["nodes"][0]["host_rpc_port"] == 27657
    assert summary["nodes"][0]["abci_container"] == "xian-node-0"
    assert summary["profiling"]["enabled"] is True
    assert summary["logging"]["app_log_json"] is True


def test_throughput_profile_skips_timeout_commit_by_default(run_init):
    summary = run_init(XIAN_LOCALNET_PROFILE="throughput")

    assert summary["consensus"]["skip_timeout_commit"] is True
    assert summary["mempool"] == {"size": 50_000, "cache_size": 100_000}


@pytest.mark.parametrize(
    ("argv", "env"),
    [
        (["--nodes", "3"], {}),
        ([], {"XIAN_LOCALNET_PROFILE": "turbo"}),
        ([], {"XIAN_LOCALNET_ENABLE_BDS": "1", "XIAN_LOCALNET_BDS_NODE_INDEX": "4"}),
    ],
)
def test_invalid_settings_exit_before_writing_a_summary(run_init, tmp_path, argv, env):
    with pytest.raises(SystemExit) as exit_info:
        run_init(*argv, **env)

    assert exit_info.value.code == 1
    assert not (tmp_path / ".localnet" / "network.json").exists()


def test_existing_localnet_needs_clean_and_bad_fee_policy_keeps_it(run_init, tmp_path):
    existing = tmp_path / ".localnet"
    existing.mkdir()
    (existing / "keep").write_text("x")

    with pytest.raises(SystemExit):
        run_init()
    with pytest.raises(ValueError):
        run_init("--clean", XIAN_TX_FEE_MODE="gratis")
    assert (existing / "keep").exists()

    summary = run_init("--clean", XIAN_TX_FEE_MODE="paid_metered")
    assert not (existing / "keep").exists()
    assert summary["chain_id"] == "xian-localnet-1"


def test_bds_node_and_postgres_share_one_generated_password_secret(run_init, tmp_path):
    import stat
    import tomllib

    run_init(XIAN_LOCALNET_ENABLE_BDS="1", XIAN_LOCALNET_BDS_NODE_INDEX="1")

    password_file = tmp_path / ".localnet" / "bds-password"
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    password = password_file.read_text(encoding="utf-8")
    assert len(password) >= 32
    compose = json.loads((tmp_path / "docker-compose-localnet.yml").read_text())
    assert compose["secrets"] == {"xian_bds_password": {"file": "./.localnet/bds-password"}}
    postgres = compose["services"]["localnet-postgres"]
    assert postgres["secrets"] == ["xian_bds_password"]
    assert postgres["environment"]["POSTGRES_PASSWORD_FILE"] == "/run/secrets/xian_bds_password"
    assert "POSTGRES_PASSWORD" not in postgres["environment"]
    assert compose["services"]["node-1"]["secrets"] == ["xian_bds_password"]
    assert password not in json.dumps(compose)
    bds = tomllib.loads(
        (tmp_path / ".localnet" / "node-1" / ".cometbft" / "config" / "xian.toml").read_text()
    )["bds"]
    assert bds["password_file"] == "/run/secrets/xian_bds_password"
    assert "password" not in bds
    assert password not in json.dumps(bds)


def test_localnet_without_bds_has_no_password_secret(run_init, tmp_path):
    run_init()

    assert not (tmp_path / ".localnet" / "bds-password").exists()
    assert "secrets" not in json.loads((tmp_path / "docker-compose-localnet.yml").read_text())


def test_consensus_and_mempool_overrides_reach_every_node_config(run_init, tmp_path):
    import tomllib

    run_init(
        XIAN_LOCALNET_CONSENSUS_TIMEOUT_PROPOSE="500ms",
        XIAN_LOCALNET_CONSENSUS_TIMEOUT_PROPOSE_DELTA="100ms",
        XIAN_LOCALNET_CONSENSUS_TIMEOUT_PREVOTE="200ms",
        XIAN_LOCALNET_CONSENSUS_TIMEOUT_PREVOTE_DELTA="50ms",
        XIAN_LOCALNET_CONSENSUS_TIMEOUT_PRECOMMIT="210ms",
        XIAN_LOCALNET_CONSENSUS_TIMEOUT_PRECOMMIT_DELTA="60ms",
        XIAN_LOCALNET_CONSENSUS_TIMEOUT_COMMIT="900ms",
        XIAN_LOCALNET_SKIP_TIMEOUT_COMMIT="yes",
        XIAN_LOCALNET_MEMPOOL_SIZE="7",
        XIAN_LOCALNET_MEMPOOL_CACHE_SIZE="11",
    )
    config = tomllib.loads(
        (tmp_path / ".localnet" / "node-3" / ".cometbft" / "config" / "config.toml").read_text()
    )

    assert {
        key: config["consensus"][key]
        for key in (
            "timeout_propose",
            "timeout_propose_delta",
            "timeout_prevote",
            "timeout_prevote_delta",
            "timeout_precommit",
            "timeout_precommit_delta",
            "timeout_commit",
            "skip_timeout_commit",
        )
    } == {
        "timeout_propose": "500ms",
        "timeout_propose_delta": "100ms",
        "timeout_prevote": "200ms",
        "timeout_prevote_delta": "50ms",
        "timeout_precommit": "210ms",
        "timeout_precommit_delta": "60ms",
        "timeout_commit": "900ms",
        "skip_timeout_commit": True,
    }
    assert (config["mempool"]["size"], config["mempool"]["cache_size"]) == (7, 11)
    assert config["p2p"]["allow_duplicate_ip"] is True
    assert config["rpc"]["laddr"] == "tcp://0.0.0.0:26657"
