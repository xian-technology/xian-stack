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

    assert selected == 2


def test_healthy_submission_index_does_not_probe_bds_abci() -> None:
    context = localnet_workload.WorkloadContext(
        _network(),
        sample_nodes=3,
        submit_node_index=0,
        round_robin_submission=True,
    )
    context._session = object()
    status_ports = []
    abci_ports = []

    async def fake_fetch_json(_session, url: str, *, timeout: float):
        port = int(url.rsplit(":", 1)[1].split("/", 1)[0])
        status_ports.append(port)
        return {
            "result": {
                "sync_info": {
                    "latest_block_height": "100",
                    "catching_up": False,
                }
            }
        }

    async def fake_abci_query_responsive(_session, url: str, *, timeout: float = 2.0):
        port = int(url.rsplit(":", 1)[1])
        abci_ports.append(port)
        return True

    with (
        patch.object(localnet_workload, "fetch_json", side_effect=fake_fetch_json),
        patch.object(
            localnet_workload,
            "abci_query_responsive",
            side_effect=fake_abci_query_responsive,
        ),
    ):
        selected = asyncio.run(context.healthy_submission_index(0))

    assert selected == 1
    assert status_ports == [27757, 27857]
    assert abci_ports == [27757, 27857]


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


def test_healthy_submission_index_skips_abci_unresponsive_node() -> None:
    context = localnet_workload.WorkloadContext(
        _network(),
        sample_nodes=3,
        submit_node_index=1,
        round_robin_submission=True,
    )
    context._session = object()

    async def fake_fetch_json(_session, url: str, *, timeout: float):
        return {
            "result": {
                "sync_info": {
                    "latest_block_height": "100",
                    "catching_up": False,
                }
            }
        }

    async def fake_abci_query_responsive(_session, url: str, *, timeout: float = 2.0):
        port = int(url.rsplit(":", 1)[1])
        return port != 27757

    with (
        patch.object(localnet_workload, "fetch_json", side_effect=fake_fetch_json),
        patch.object(
            localnet_workload,
            "abci_query_responsive",
            side_effect=fake_abci_query_responsive,
        ),
    ):
        selected = asyncio.run(context.healthy_submission_index(1))

    assert selected == 2


def test_healthy_state_sample_nodes_skip_lagging_nodes() -> None:
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
            27657: 120,
            27757: 90,
            27857: 120,
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
        nodes, skipped = asyncio.run(context.healthy_state_sample_nodes())

    assert [node.moniker for node in nodes] == ["node-0", "node-2"]
    assert skipped == ["node-1: height=90, catching_up=False, target=120"]


def test_compared_state_value_uses_returned_sample_nodes() -> None:
    state = {
        "ok": True,
        "sample_nodes": ["node-2"],
        "skipped_nodes": ["node-0: height=99, catching_up=False, target=100"],
        "queries": [
            {
                "label": "counter value",
                "values": {"node-2": "17"},
                "errors": {},
            }
        ],
    }

    localnet_workload.require_matching_state("counter_basic", state)
    assert (
        localnet_workload.compared_state_value(state, 0, scenario="counter_basic")
        == "17"
    )


def test_require_matching_state_reports_mismatched_samples() -> None:
    state = {
        "ok": False,
        "sample_nodes": ["node-0", "node-1"],
        "skipped_nodes": [],
        "queries": [
            {
                "label": "counter value",
                "values": {"node-0": "1", "node-1": "2"},
                "errors": {},
            }
        ],
    }

    try:
        localnet_workload.require_matching_state("counter_basic", state)
    except localnet_workload.WorkloadError as exc:
        assert "counter_basic: state comparison failed" in str(exc)
        assert "node-1" in str(exc)
    else:
        raise AssertionError("expected WorkloadError")


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


def test_reserve_nonces_uses_highest_nonce_from_healthy_nodes() -> None:
    context = localnet_workload.WorkloadContext(
        _network(),
        sample_nodes=3,
        submit_node_index=0,
        round_robin_submission=True,
    )
    context._session = object()
    context.healthy_submission_indices = AsyncMock(return_value=[1, 2])
    wallet = localnet_workload.Wallet()

    async def fake_get_nonce(url: str, _address: str, *, session):
        port = int(url.rsplit(":", 1)[1])
        return {
            27757: 41,
            27857: 43,
        }[port]

    with patch.object(localnet_workload.tr, "get_nonce_async", side_effect=fake_get_nonce):
        nonces = asyncio.run(context.reserve_nonces(wallet, 0, count=3))

    assert nonces == [43, 44, 45]


def test_resolve_record_rebroadcasts_signed_tx_after_receipt_timeout() -> None:
    context = localnet_workload.WorkloadContext(
        _network(),
        sample_nodes=3,
        submit_node_index=0,
        round_robin_submission=True,
    )
    context._session = object()
    context.healthy_submission_index = AsyncMock(return_value=1)
    record = localnet_workload.BroadcastRecord(
        label="recover missing tx",
        contract="currency",
        function="transfer",
        rpc_url=context.nodes[2].rpc_url,
        sender="sender",
        expected_success=True,
        expected_message=None,
        response=SimpleNamespace(submitted=True, accepted=True, message=None),
        tx={"signed": "payload"},
        tx_hash="tx-hash",
    )
    receipt = {
        "height": 12,
        "tx_index": 0,
        "success": True,
        "result": None,
        "events": [],
        "chi_used": 20,
    }
    wait_for_receipt = AsyncMock(
        side_effect=[
            localnet_workload.WorkloadError("missing before rebroadcast"),
            receipt,
        ]
    )
    rebroadcast = AsyncMock(return_value={"result": {"code": 0, "hash": "tx-hash"}})

    with (
        patch.object(localnet_workload, "wait_for_tx_receipt", wait_for_receipt),
        patch.object(localnet_workload.tr, "broadcast_tx_wait_async", rebroadcast),
    ):
        asyncio.run(context._resolve_record(record, timeout_seconds=1.0))

    assert record.height == 12
    assert record.final_success is True
    rebroadcast.assert_awaited_once_with(
        context.nodes[1].rpc_url,
        {"signed": "payload"},
        session=context.session,
    )


def test_wait_for_tx_receipt_scans_wide_recent_blocks_after_timeout() -> None:
    session = object()
    scan_args = {}

    class Client:
        node_url = "http://node-1"

        async def wait_for_tx(self, *_args, **_kwargs):
            raise TimeoutError("not indexed")

        def _normalize_tx_lookup(self, lookup):
            return SimpleNamespace(
                raw=lookup,
                execution={"events": [{"event": "Transfer"}], "chi_used": 11},
                success=True,
                message=None,
            )

    client = Client()
    client.session = session

    async def fake_status(_node_url: str, *, session):
        return {"result": {"sync_info": {"latest_block_height": "1500"}}}

    async def fake_lookup(node_url: str, tx_hash: str, **kwargs):
        scan_args.update({"node_url": node_url, "tx_hash": tx_hash, **kwargs})
        return {"result": {"height": "1200", "index": "3"}}

    with (
        patch.object(localnet_workload.tr, "get_status_async", side_effect=fake_status),
        patch.object(
            localnet_workload.tr,
            "_lookup_tx_in_recent_blocks_async",
            side_effect=fake_lookup,
        ),
    ):
        receipt = asyncio.run(
            localnet_workload.wait_for_tx_receipt(
                clients=[client],
                tx_hash="tx-hash",
                timeout_seconds=0.0,
            )
        )

    assert receipt == {
        "height": 1200,
        "tx_index": 3,
        "success": True,
        "result": None,
        "events": [{"event": "Transfer"}],
        "chi_used": 11,
    }
    assert scan_args == {
        "node_url": "http://node-1",
        "tx_hash": "tx-hash",
        "start_height": 301,
        "end_height": 1500,
        "session": session,
    }


def test_wait_for_mempool_drain_flushes_stale_pending_tx() -> None:
    context = localnet_workload.WorkloadContext(
        _network(),
        sample_nodes=3,
        submit_node_index=0,
        round_robin_submission=True,
    )
    context._session = object()
    call_count = 0
    flush_record = localnet_workload.BroadcastRecord(
        label="mempool flush",
        contract="currency",
        function="transfer",
        rpc_url=context.nodes[1].rpc_url,
        sender="founder",
        expected_success=True,
        expected_message=None,
        response=SimpleNamespace(submitted=True, accepted=True, message=None),
        tx_hash="flush-hash",
        final_success=True,
    )
    context.broadcast_tx = AsyncMock(return_value=flush_record)
    context.resolve_records = AsyncMock()
    polls_by_port = {}

    async def fake_fetch_json(_session, url: str, *, timeout: float):
        nonlocal call_count
        port = int(url.rsplit(":", 1)[1].split("/", 1)[0])
        round_index = polls_by_port.get(port, 0)
        polls_by_port[port] = round_index + 1
        call_count += 1
        pending = 1 if round_index == 0 and port == 27757 else 0
        return {"result": {"n_txs": str(pending)}}

    with patch.object(localnet_workload, "fetch_json", side_effect=fake_fetch_json):
        asyncio.run(
            localnet_workload.wait_for_mempool_drain(
                context,
                timeout_seconds=2.0,
                stable_polls=2,
                poll_interval_seconds=0.01,
            )
        )

    context.broadcast_tx.assert_awaited_once()
    context.resolve_records.assert_awaited_once_with([flush_record], timeout_seconds=2.0)
    assert 27657 not in polls_by_port
