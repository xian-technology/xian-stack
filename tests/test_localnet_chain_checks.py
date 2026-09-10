from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import localnet_chain_checks as checks
from localnet_accounting_checks import LedgerModel
from localnet_e2e_support import E2EError
from localnet_replay_capture import result_digest
from localnet_sync_checks import set_config


def test_accounting_model_conserves_liquid_and_staked_supply_and_rolls_back_failures():
    model = LedgerModel()
    assert model.apply("move", 0, 1, 125)
    assert model.apply("stake", 1, 0, 200)
    assert model.apply("unstake", 1, 0, 50)
    assert model.apply("burn", 0, 0, 25)
    assert model.apply("mint", 0, 2, 10)
    expected = {"balances": [850, 975, 1010, 1000], "staked": [0, 150, 0, 0], "supply": 3985}
    assert model.report() == expected
    assert not model.apply("move", 0, 1, 10000)
    assert not model.apply("unstake", 1, 0, 151)
    assert not model.apply("rollback", 0, 1, 10)
    assert not model.apply("mint", 0, 1, -1)
    assert not model.apply("burn", 0, 1, 0)
    assert model.report() == expected


@pytest.mark.parametrize(
    "changed",
    [
        {"code": 1},
        {"data": "Yg=="},
        {"gas_used": "2"},
        {"events": [{"type": "transfer", "attributes": [{"key": "amount", "value": "2"}]}]},
    ],
)
def test_replay_oracle_detects_execution_fee_and_event_drift(changed):
    result = {
        "code": 0,
        "data": "YQ==",
        "gas_used": "1",
        "events": [{"type": "transfer", "attributes": [{"key": "amount", "value": "1"}]}],
    }
    assert result_digest([result]) != result_digest([{**result, **changed}])


def test_replay_oracle_normalizes_protobuf_defaults_without_changing_event_order():
    assert result_digest([{}]) == result_digest([{"code": 0, "gas_used": "0", "events": []}])
    assert result_digest([{}]) == result_digest([{"events": None}])
    assert result_digest([{"code": 1}, {"code": 0}]) != result_digest([{"code": 0}, {"code": 1}])


def test_config_edit_is_scoped_and_rejects_a_missing_setting():
    original = "[rpc]\nenable = false\n[p2p]\nenable = false\n"
    assert set_config(original, "p2p", {"enable": True}) == (
        "[rpc]\nenable = false\n[p2p]\nenable = true\n"
    )
    with pytest.raises(E2EError, match="Missing/duplicate"):
        set_config(original, "p2p", {"missing": 2})


def test_live_agreement_rejects_result_divergence_even_when_app_hashes_match():
    nodes = [SimpleNamespace(moniker="a"), SimpleNamespace(moniker="b")]
    responses = [{"txs_results": [{"code": 0}]}, {"txs_results": [{"code": 1}]}]
    with (
        patch.object(checks, "height", AsyncMock(return_value=10)),
        patch.object(checks, "wait_height", AsyncMock(return_value=10)),
        patch.object(
            checks,
            "compare_app_hash_window",
            AsyncMock(
                return_value={
                    "ok": True,
                    "checks": [{"height": 10}],
                }
            ),
        ),
        patch.object(checks, "rpc", AsyncMock(side_effect=responses)),
    ):
        with pytest.raises(E2EError, match="result/fee/event divergence"):
            asyncio.run(checks.agreement(object(), nodes))


def test_all_lifecycle_phases_are_in_the_regular_e2e_registry():
    from localnet_e2e_phases import phase_names

    assert phase_names()[-8:] == [
        "19-crash-recovery",
        "20-quorum-recovery",
        "21-fresh-node-sync",
        "22-mixed-execution",
        "23-nonce-recovery",
        "24-accounting-invariants",
        "25-block-limits",
        "26-replay-determinism",
    ]


@pytest.mark.parametrize("address_reused", [False, True])
def test_partition_restores_network_after_workload_failure(address_reused):
    import json

    import localnet_recovery_checks as recovery

    original = {"IPAddress": "172.30.0.4", "Aliases": ["node-4", "validator"]}
    connected = {"test_localnet": original.copy()}
    calls = []

    async def command(*args):
        calls.append(args)
        if args[:3] == ("docker", "network", "inspect"):
            endpoints = (
                {"restarted-peer": {"IPv4Address": "172.30.0.4/16"}} if address_reused else {}
            )
            return json.dumps([{"Containers": endpoints}])
        if args[:2] == ("docker", "inspect"):
            return json.dumps([{"NetworkSettings": {"Networks": connected}}])
        if args[:3] == ("docker", "network", "connect"):
            connected[args[-2]] = {}
        if args[:3] == ("docker", "network", "disconnect"):
            connected.pop(args[-2])
        return ""

    async def exercise():
        runner = SimpleNamespace(run_id="test")
        node = SimpleNamespace(moniker="node-4", cometbft_container="validator-4")
        async with recovery.partition(runner, [node]):
            assert "test_localnet" not in connected
            raise RuntimeError("workload failed")

    with patch.object(recovery, "command", command):
        with pytest.raises(RuntimeError, match="workload failed"):
            asyncio.run(exercise())
    assert list(connected) == ["test_localnet"]
    expected = ("docker", "network", "connect")
    if not address_reused:
        expected += ("--ip", "172.30.0.4")
    expected += ("--alias", "node-4", "--alias", "validator", "test_localnet", "validator-4")
    assert expected in calls
    assert calls[-1] == ("docker", "network", "rm", "xian-e2e-partition-test")


def test_resource_boundary_restores_consensus_timings_after_failure(tmp_path):
    import localnet_execution_checks as execution

    original = (
        '[consensus]\ntimeout_propose = "3s"\ntimeout_prevote = "1s"\ntimeout_precommit = "1s"\n'
    )
    config = tmp_path / "config.toml"
    config.write_text(original)
    runner = SimpleNamespace(nodes=[object()], restart_localnet_and_wait_ready=AsyncMock())

    async def failed_workload(*args):
        assert 'timeout_propose = "30s"' in config.read_text()
        raise RuntimeError("boundary workload failed")

    with (
        patch.object(execution, "config_path", return_value=tmp_path / "xian.toml"),
        patch.object(execution, "limits_workload", failed_workload),
        patch.object(execution, "agreement", AsyncMock()),
        pytest.raises(RuntimeError, match="boundary workload failed"),
    ):
        asyncio.run(execution.limits_phase(runner, object()))
    assert config.read_text() == original
    assert runner.restart_localnet_and_wait_ready.await_count == 2


def test_config_publication_keeps_open_readers_on_a_complete_file(tmp_path):
    path = tmp_path / "config.toml"
    original = '[consensus]\ntimeout_propose = "3s"\n'
    updated = '[consensus]\ntimeout_propose = "30s"\n'
    path.write_text(original)
    path.chmod(0o640)
    with path.open() as reader:
        checks.write_config(path, updated)
        assert reader.read() == original
    assert path.read_text() == updated
    assert path.stat().st_mode & 0o777 == 0o640


def test_observer_readiness_hashes_exclude_mutable_peer_book(tmp_path):
    import json

    config = tmp_path / "config"
    config.mkdir()
    (config / "config.toml").write_text('[rpc]\nladdr = "tcp://0.0.0.0:26657"\n')
    (config / "addrbook.json").write_text('{"peers": []}')
    initial = checks.config_hashes(tmp_path)
    (config / "addrbook.json").write_text('{"peers": ["discovered-peer"]}')
    assert checks.config_hashes(tmp_path) == initial
    assert set(json.loads(initial)) == {"config/config.toml"}
