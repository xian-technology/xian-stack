from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        SimpleNamespace(moniker=f"node-{index}", index=index, rpc_url=f"http://node-{index}")
        for index in range(5)
    ]
    runner.bds_node = runner.nodes[0]
    runner.founder_wallet = "founder"
    runner.validator_wallets = [f"validator-{index}" for index in range(5)]
    runner.network = {"chain_id": "xian-localnet-e2e"}
    runner.restart_localnet_and_wait_ready = AsyncMock()
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


def test_arming_on_resume_adopts_the_governed_activation_height(tmp_path, monkeypatch):
    e2e, runner, _ = arming_runner(tmp_path, monkeypatch, existing_patch=None)
    first = arm(runner)["bundle"]
    resumed_bundle = {**first, "activation_height": 95}
    del resumed_bundle["bundle_hash"]
    existing = {
        "activation_height": 95,
        "bundle_hash": e2e.compute_patch_bundle_hash(resumed_bundle),
    }
    _, runner, _ = arming_runner(tmp_path, monkeypatch, existing_patch=existing)

    armed = arm(runner)

    assert armed["activation_height"] == 95
    assert armed["bundle"]["bundle_hash"] == existing["bundle_hash"]
    runner.approve_governance_proposal.assert_not_awaited()
    assert armed["proposal_receipt"] is None

    _, runner, _ = arming_runner(
        tmp_path, monkeypatch, existing_patch={"activation_height": 95, "bundle_hash": "other"}
    )
    with pytest.raises(E2EError, match="does not match"):
        arm(runner)
