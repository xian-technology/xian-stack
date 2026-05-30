from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "localnet-workload.py"
sys.path.insert(0, str(MODULE_PATH.parent))

if "localnet_workload" in sys.modules:
    localnet_workload = sys.modules["localnet_workload"]
else:
    SPEC = importlib.util.spec_from_file_location("localnet_workload", MODULE_PATH)
    if SPEC is None or SPEC.loader is None:
        raise RuntimeError(f"unable to load {MODULE_PATH}")
    localnet_workload = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = localnet_workload
    SPEC.loader.exec_module(localnet_workload)


def _network() -> dict:
    founder = localnet_workload.Wallet()
    nodes = []
    for index in range(3):
        nodes.append(
            {
                "moniker": f"node-{index}",
                "host_rpc_port": 27657 + index * 100,
                "host_p2p_port": 27656 + index * 100,
                "host_metrics_port": 27660 + index * 100,
                "abci_container": f"xian-node-{index}",
                "cometbft_container": f"xian-node-{index}",
                "bds_enabled": index == 0,
            }
        )
    return {
        "chain_id": "test-chain",
        "founder_key": founder.private_key,
        "nodes": nodes,
    }


def test_healthy_submission_index_skips_bds_node_when_available() -> None:
    context = localnet_workload.WorkloadContext(
        _network(),
        sample_nodes=3,
        submit_node_index=0,
        round_robin_submission=True,
    )
    context._session = object()

    async def fake_fetch_json(_session, url: str, *, timeout: float):
        port = int(url.rsplit(":", 1)[1].split("/", 1)[0])
        heights = {
            27657: 100,
            27757: 99,
            27857: 100,
        }
        return {
            "result": {
                "sync_info": {
                    "latest_block_height": str(heights[port]),
                    "catching_up": False,
                }
            }
        }

    with patch.object(localnet_workload, "fetch_json", side_effect=fake_fetch_json):
        selected = asyncio.run(context.healthy_submission_index(0))

    assert selected == 1


def test_healthy_submission_index_uses_highest_non_bds_when_preferred_lags() -> None:
    context = localnet_workload.WorkloadContext(
        _network(),
        sample_nodes=3,
        submit_node_index=1,
        round_robin_submission=True,
    )
    context._session = object()

    async def fake_fetch_json(_session, url: str, *, timeout: float):
        port = int(url.rsplit(":", 1)[1].split("/", 1)[0])
        heights = {
            27657: 120,
            27757: 40,
            27857: 118,
        }
        return {
            "result": {
                "sync_info": {
                    "latest_block_height": str(heights[port]),
                    "catching_up": False,
                }
            }
        }

    with patch.object(localnet_workload, "fetch_json", side_effect=fake_fetch_json):
        selected = asyncio.run(context.healthy_submission_index(1))

    assert selected == 2


def test_broadcast_tx_retries_transport_timeout_on_next_healthy_node() -> None:
    context = localnet_workload.WorkloadContext(
        _network(),
        sample_nodes=3,
        submit_node_index=1,
        round_robin_submission=True,
    )
    context._session = object()
    context.healthy_submission_index = AsyncMock(side_effect=[1, 2])
    sends = []

    class FakeClient:
        def __init__(self, node_index: int):
            self.node_index = node_index

        async def send_tx(self, **kwargs):
            sends.append((self.node_index, kwargs))
            if self.node_index == 1:
                raise localnet_workload.TransportError("node timed out")
            return SimpleNamespace(
                submitted=True,
                accepted=True,
                message=None,
                tx_hash="tx-hash",
            )

    context.client = lambda _wallet, node_index: FakeClient(node_index)
    wallet = localnet_workload.Wallet()

    record = asyncio.run(
        context.broadcast_tx(
            label="retrying broadcast",
            wallet=wallet,
            rpc_index=1,
            contract="currency",
            function="transfer",
            kwargs={"amount": "1", "to": wallet.public_key},
            chi=1_500,
            expected_success=True,
            nonce=7,
        )
    )

    assert record.tx_hash == "tx-hash"
    assert record.rpc_url == context.nodes[2].rpc_url
    assert [(node_index, kwargs["nonce"]) for node_index, kwargs in sends] == [
        (1, 7),
        (2, 7),
    ]
