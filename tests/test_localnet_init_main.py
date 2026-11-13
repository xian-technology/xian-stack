"""`localnet-init.py` end to end: settings from the environment reach the summary."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
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
    assert summary["topology"] == "integrated"
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


def test_throughput_profile_fills_unset_consensus_and_mempool(run_init):
    summary = run_init(
        "--nodes",
        "5",
        "--topology",
        "fidelity",
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
    assert summary["nodes"][0]["abci_container"] == "xian-node-0-abci"
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
