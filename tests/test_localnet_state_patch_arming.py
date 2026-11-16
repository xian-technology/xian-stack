from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

# Not resolve(): mutation runs execute the tests from a symlinked overlay.
SCRIPTS = Path(__file__).absolute().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from localnet_e2e_support import E2EError  # noqa: E402
from xian_py.exception import SimulationError  # noqa: E402


def localnet_e2e():
    if "localnet_e2e" not in sys.modules:
        spec = importlib.util.spec_from_file_location("localnet_e2e", SCRIPTS / "localnet-e2e.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules["localnet_e2e"]


def arming_runner(tmp_path, monkeypatch, *, existing_patch):
    from contextlib import asynccontextmanager

    e2e = localnet_e2e()

    monkeypatch.setattr(e2e, "STACK_DIR", tmp_path)
    monkeypatch.setattr(
        e2e,
        "fetch_json",
        AsyncMock(return_value={"result": {"sync_info": {"latest_block_height": "100"}}}),
    )
    monkeypatch.setattr(e2e, "fetch_abci_query", AsyncMock(return_value=2))
    runner = object.__new__(e2e.E2ERunner)
    runner.nodes = [
        SimpleNamespace(
            moniker=f"node-{index}",
            index=index,
            rpc_url=f"http://node-{index}",
            cometbft_container=f"comet-{index}",
        )
        for index in range(5)
    ]
    runner.bds_node = runner.nodes[0]
    runner.founder_wallet = "founder"
    runner.validator_wallets = [f"validator-{index}" for index in range(5)]
    runner.network = {"chain_id": "xian-localnet-e2e"}
    runner.restart_localnet_and_wait_ready = AsyncMock()
    monkeypatch.setattr(e2e, "run_cmd", Mock())
    runner.approve_governance_proposal = AsyncMock(return_value={"proposal_receipt": {"ok": 1}})
    clients = []

    @asynccontextmanager
    async def client(wallet, index, session):
        connection = SimpleNamespace(
            wallet=wallet,
            refresh_nonce=AsyncMock(),
            call=AsyncMock(side_effect=SimulationError("missing patch"))
            if existing_patch is None
            else AsyncMock(return_value=existing_patch),
        )
        clients.append(connection)
        yield connection

    runner.client = client
    return e2e, runner, clients


def arm(runner):
    return asyncio.run(
        runner.arm_state_patch(
            object(),
            patch_id="zk-off-abc",
            summary="Switch zk off",
            changes=[{"key": "__runtime_features.zk", "value": False, "comment": "zk off"}],
            label_prefix="zk-feature-off",
        )
    )


def written_bundles(tmp_path):
    return [
        json.loads(path.read_text())
        for path in sorted(tmp_path.glob(".localnet/*/.cometbft/config/state-patches/*.json"))
    ]


def test_arming_writes_one_bundle_per_node_restarts_and_approves(tmp_path, monkeypatch):
    e2e, runner, clients = arming_runner(tmp_path, monkeypatch, existing_patch=None)

    armed = arm(runner)

    # min_patch_delay 2 + headroom 8 = 10 is above the 8-block floor.
    assert armed["activation_height"] == 110
    bundles = written_bundles(tmp_path)
    assert len(bundles) == 5 and all(bundle == armed["bundle"] for bundle in bundles)
    assert armed["bundle"]["bundle_hash"] == e2e.compute_patch_bundle_hash(armed["bundle"])
    # Exercise the real consumer: a hashed file with the wrong schema cannot load.
    from xian.utils.state_patches import StatePatchManager

    for node in runner.nodes:
        manager = StatePatchManager(None, chain_id=runner.network["chain_id"])
        manager.load_patches(
            tmp_path / ".localnet" / node.moniker / ".cometbft/config/state-patches"
        )
        assert manager.local_bundles["zk-off-abc"].bundle_hash == armed["bundle"]["bundle_hash"]
    runner.restart_localnet_and_wait_ready.assert_awaited_once()
    proposer, voters = runner.approve_governance_proposal.await_args.args
    assert proposer.wallet == "validator-0"
    assert [(name, voter.wallet) for name, voter in voters] == [
        (f"node{index}", f"validator-{index}") for index in range(1, 5)
    ]
    kwargs = runner.approve_governance_proposal.await_args.kwargs
    assert kwargs["proposal_kwargs"]["activation_height"] == 110
    assert kwargs["proposal_kwargs"]["bundle_hash"] == armed["bundle"]["bundle_hash"]
    assert kwargs["proposal_kwargs"]["emergency"] is False
    assert kwargs["label_prefix"] == "zk-feature-off"
    assert armed["proposal_receipt"] == {"ok": 1}
    assert all(client.refresh_nonce.await_count == 1 for client in clients[1:])


@pytest.mark.parametrize("status", ["approved", "applied"])
def test_arming_on_resume_adopts_the_governed_activation_height(tmp_path, monkeypatch, status):
    e2e, runner, _ = arming_runner(tmp_path, monkeypatch, existing_patch=None)
    first = arm(runner)["bundle"]
    resumed_bundle = {**first, "activation_height": 95}
    del resumed_bundle["bundle_hash"]
    existing = {
        "activation_height": 95,
        "bundle_hash": e2e.compute_patch_bundle_hash(resumed_bundle),
        "status": status,
    }
    _, runner, _ = arming_runner(tmp_path, monkeypatch, existing_patch=existing)

    armed = arm(runner)

    assert armed["activation_height"] == 95
    assert armed["bundle"]["bundle_hash"] == existing["bundle_hash"]
    runner.approve_governance_proposal.assert_not_awaited()
    assert armed["proposal_receipt"] is None

    _, runner, _ = arming_runner(
        tmp_path,
        monkeypatch,
        existing_patch={"activation_height": 95, "bundle_hash": "other", "status": status},
    )
    with pytest.raises(E2EError, match="does not match"):
        arm(runner)


def test_restart_cannot_consume_the_governed_proposal_delay(tmp_path, monkeypatch):
    e2e, runner, _ = arming_runner(tmp_path, monkeypatch, existing_patch=None)
    committed_height = 100

    async def status(*args, **kwargs):
        return {"result": {"sync_info": {"latest_block_height": str(committed_height)}}}

    async def restart(session):
        nonlocal committed_height
        # Readiness may include arbitrarily many catch-up blocks.
        committed_height += 15

    async def approve(*args, **kwargs):
        assert kwargs["proposal_kwargs"]["activation_height"] >= committed_height + 20
        bundles = written_bundles(tmp_path)
        assert len(bundles) == 5
        assert all(
            bundle["bundle_hash"] == kwargs["proposal_kwargs"]["bundle_hash"] for bundle in bundles
        )
        return {"proposal_receipt": {"ok": 1}}

    monkeypatch.setattr(e2e, "fetch_json", status)
    monkeypatch.setattr(e2e, "fetch_abci_query", AsyncMock(return_value=20))
    runner.restart_localnet_and_wait_ready.side_effect = restart
    runner.approve_governance_proposal.side_effect = approve

    armed = arm(runner)

    assert armed["activation_height"] == 128
    assert armed["proposal_receipt"] == {"ok": 1}
    e2e.run_cmd.assert_called_once_with(
        ["docker", "stop", "comet-1", "comet-2", "comet-3", "comet-4"], cwd=tmp_path
    )
    runner.restart_localnet_and_wait_ready.assert_awaited_once()


def test_nonce_preparation_precedes_the_authoritative_height_sample(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    e2e, runner, _ = arming_runner(tmp_path, monkeypatch, existing_patch=None)
    original_client = runner.client
    committed_height = 100

    def nonce_prepared():
        nonlocal committed_height
        committed_height = 115

    @asynccontextmanager
    async def client(*args):
        async with original_client(*args) as connection:
            connection.refresh_nonce.side_effect = nonce_prepared
            yield connection

    async def status(*args, **kwargs):
        return {"result": {"sync_info": {"latest_block_height": str(committed_height)}}}

    runner.client = client
    monkeypatch.setattr(e2e, "fetch_json", status)
    monkeypatch.setattr(e2e, "fetch_abci_query", AsyncMock(return_value=20))

    assert arm(runner)["activation_height"] == 143


def test_bundle_write_failure_never_proposes_or_stops_validators(tmp_path, monkeypatch):
    e2e, runner, _ = arming_runner(tmp_path, monkeypatch, existing_patch=None)
    monkeypatch.setattr(Path, "write_text", Mock(side_effect=OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        arm(runner)

    e2e.run_cmd.assert_not_called()
    runner.restart_localnet_and_wait_ready.assert_not_awaited()
    runner.approve_governance_proposal.assert_not_awaited()


def test_missing_governed_delay_uses_the_default_delay(tmp_path, monkeypatch):
    e2e, runner, _ = arming_runner(tmp_path, monkeypatch, existing_patch=None)
    monkeypatch.setattr(e2e, "fetch_abci_query", AsyncMock(return_value=None))

    armed = arm(runner)

    assert armed["activation_height"] == 116
    assert armed["governance_min_patch_delay"] == 8


def test_partial_peer_stop_failure_still_restarts_the_network(tmp_path, monkeypatch):
    e2e, runner, _ = arming_runner(tmp_path, monkeypatch, existing_patch=None)
    monkeypatch.setattr(e2e, "run_cmd", Mock(side_effect=RuntimeError("peer unavailable")))

    with pytest.raises(RuntimeError, match="peer unavailable"):
        arm(runner)

    runner.restart_localnet_and_wait_ready.assert_awaited_once()
    runner.approve_governance_proposal.assert_awaited_once()


@pytest.mark.parametrize("status", ["pending", "expired", "rejected", None])
def test_resume_refuses_a_patch_without_completed_approval(tmp_path, monkeypatch, status):
    e2e, runner, _ = arming_runner(tmp_path, monkeypatch, existing_patch=None)
    bundle = arm(runner)["bundle"]
    _, runner, _ = arming_runner(tmp_path, monkeypatch, existing_patch={**bundle, "status": status})

    with pytest.raises(E2EError, match="complete governance approval before resuming"):
        arm(runner)

    runner.approve_governance_proposal.assert_not_awaited()
    runner.restart_localnet_and_wait_ready.assert_not_awaited()
    e2e.run_cmd.assert_not_called()
