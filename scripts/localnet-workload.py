#!/usr/bin/env python3
"""Run deterministic workload scenarios against a local Xian network."""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import json
import random
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
from localnet_common import compare_app_hash_window, fetch_json
from xian_runtime_types.decimal import ContractingDecimal

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent
ROOT_DIR = STACK_DIR.parent
WORKLOADS_DIR = STACK_DIR / "workloads"
NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"
XIAN_CONTRACTING_SRC = ROOT_DIR / "xian-contracting" / "src"

sys.path.insert(0, str(XIAN_CONTRACTING_SRC))

from contracting.artifacts import build_contract_artifacts  # noqa: E402
from xian_py import transaction as tr  # noqa: E402
from xian_py.exception import TransportError  # noqa: E402
from xian_py.models import TransactionSubmission  # noqa: E402
from xian_py.wallet import Wallet  # noqa: E402
from xian_py.xian_async import XianAsync  # noqa: E402

COUNTER_DEPLOY_CHI = 75_000
COUNTER_TX_CHI = 1_500
COUNTER_BROADCAST_BATCH_SIZE = 40
TOKEN_DEPLOY_CHI = 150_000
PAIR_DEPLOY_CHI = 300_000
DEX_DEPLOY_CHI = 200_000
TOKEN_TX_CHI = 7_500
DEX_TX_CHI = 60_000
PARALLEL_PROBE_DEPLOY_CHI = 90_000
PARALLEL_PROBE_TX_CHI = 2_000
THROUGHPUT_TRANSFER_TX_CHI = 1_500
THROUGHPUT_CONTRACT_DEPLOY_CHI = 120_000
THROUGHPUT_CONTRACT_TX_CHI = 6_000
RECEIPT_TIMEOUT_SECONDS = 45.0
RECEIPT_FALLBACK_BLOCK_SCAN_WINDOW = 1_200
ABCI_HEALTH_QUERY_PATH = "/get/currency.balances:__xian_localnet_workload_health_probe__"


@dataclass(frozen=True)
class LocalnetNode:
    moniker: str
    rpc_url: str
    rpc_port: int
    p2p_port: int
    metrics_port: int
    abci_container: str
    cometbft_container: str
    bds_node: bool = False


@dataclass
class BroadcastRecord:
    label: str
    contract: str
    function: str
    rpc_url: str
    sender: str
    expected_success: bool
    expected_message: str | None
    response: TransactionSubmission
    tx: dict[str, Any] | None = None
    tx_hash: str | None = None
    final_success: bool | None = None
    final_message: str | None = None
    height: int | None = None
    tx_index: int | None = None
    chi_used: int | None = None
    events: list[dict[str, Any]] | None = None


class WorkloadError(RuntimeError):
    pass


async def abci_query_responsive(
    session: aiohttp.ClientSession,
    rpc_url: str,
    *,
    timeout: float = 2.0,
) -> bool:
    try:
        async with session.get(
            f"{rpc_url}/abci_query",
            params={"path": json.dumps(ABCI_HEALTH_QUERY_PATH)},
            timeout=timeout,
        ) as response:
            payload = await response.json()
        abci_response = payload.get("result", {}).get("response", {})
        return int(abci_response.get("code", 0) or 0) == 0
    except Exception:  # noqa: BLE001
        return False


class WorkloadContext:
    def __init__(
        self,
        network: dict,
        *,
        sample_nodes: int,
        submit_node_index: int,
        round_robin_submission: bool,
    ):
        self.network = network
        self.chain_id = network["chain_id"]
        self.founder_wallet = Wallet(private_key=network["founder_key"])
        self.nodes = [
            LocalnetNode(
                moniker=node["moniker"],
                rpc_url=f"http://127.0.0.1:{node['host_rpc_port']}",
                rpc_port=node["host_rpc_port"],
                p2p_port=node["host_p2p_port"],
                metrics_port=node["host_metrics_port"],
                abci_container=node["abci_container"],
                cometbft_container=node["cometbft_container"],
                bds_node=bool(node.get("bds_enabled")),
            )
            for node in network["nodes"]
        ]
        self.sample_nodes = self._select_sample_nodes(sample_nodes)
        self.submit_node_index = submit_node_index % len(self.nodes)
        self.round_robin_submission = round_robin_submission
        self._next_nonce: dict[str, int] = {}
        self._submission_affinity: dict[str, int] = {}
        self._nonce_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    def contract_submission_kwargs(
        self,
        *,
        name: str,
        code: str,
        constructor_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"name": name}
        if constructor_args is not None:
            kwargs["constructor_args"] = constructor_args
        kwargs["deployment_artifacts"] = build_deployment_artifacts(name, code)
        return kwargs

    async def __aenter__(self) -> WorkloadContext:
        connector = aiohttp.TCPConnector(limit=256, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=15, sock_connect=3, sock_read=10)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
        )
        await self.wait_for_nodes_ready()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None

    async def _retry_read(
        self,
        operation,
        *,
        max_attempts: int = 5,
        initial_delay_seconds: float = 0.1,
        max_delay_seconds: float = 1.0,
    ):
        delay = max(initial_delay_seconds, 0.0)
        for attempt in range(1, max_attempts + 1):
            try:
                return await operation()
            except TransportError:
                if attempt >= max_attempts:
                    raise
                if delay > 0:
                    await asyncio.sleep(delay)
                delay = min(max(delay * 2, delay), max_delay_seconds)

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise WorkloadError("workload context session is not initialized")
        return self._session

    def _select_sample_nodes(self, sample_nodes: int) -> list[LocalnetNode]:
        if sample_nodes <= 0:
            raise WorkloadError("state sample node count must be positive")
        if sample_nodes >= len(self.nodes):
            return list(self.nodes)
        if sample_nodes == 1:
            return [self.nodes[0]]

        indices = []
        span = len(self.nodes) - 1
        for i in range(sample_nodes):
            raw_index = round(i * span / (sample_nodes - 1))
            if raw_index not in indices:
                indices.append(raw_index)
        return [self.nodes[index] for index in indices]

    async def wait_for_nodes_ready(
        self,
        *,
        timeout_seconds: float = 45.0,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            ready_count = 0
            for index, node in enumerate(self.nodes):
                try:
                    payload = await fetch_json(
                        self.session,
                        f"{node.rpc_url}/status",
                        timeout=2.0,
                    )
                    latest_height = int(payload["result"]["sync_info"]["latest_block_height"])
                    if latest_height < 1:
                        raise WorkloadError(f"{node.moniker} has not produced a block yet")
                    await self.client(self.founder_wallet, index).refresh_nonce()
                    ready_count += 1
                except Exception as exc:  # noqa: PERF203
                    last_error = exc
                    break

            if ready_count == len(self.nodes):
                return

            await asyncio.sleep(1.0)

        raise WorkloadError("localnet nodes did not become ready") from last_error

    def client(self, wallet: Wallet, rpc_index: int) -> XianAsync:
        node = self.nodes[rpc_index % len(self.nodes)]
        return XianAsync(
            node_url=node.rpc_url,
            chain_id=self.chain_id,
            wallet=wallet,
            session=self.session,
        )

    def submission_index(self, requested_rpc_index: int) -> int:
        if self.round_robin_submission:
            return requested_rpc_index % len(self.nodes)
        return self.submit_node_index

    async def node_statuses(
        self,
        *,
        include_bds: bool = True,
    ) -> list[tuple[int, int, bool, bool]]:
        statuses: list[tuple[int, int, bool, bool]] = []

        for index, node in enumerate(self.nodes):
            if node.bds_node and not include_bds:
                continue
            try:
                payload = await fetch_json(
                    self.session,
                    f"{node.rpc_url}/status",
                    timeout=2.0,
                )
                sync_info = payload["result"]["sync_info"]
                statuses.append(
                    (
                        index,
                        int(sync_info["latest_block_height"]),
                        bool(sync_info.get("catching_up", False)),
                        await abci_query_responsive(self.session, node.rpc_url),
                    )
                )
            except Exception:  # noqa: PERF203, BLE001
                continue

        return statuses

    async def healthy_submission_indices(self, preferred_rpc_index: int) -> list[int]:
        preferred_rpc_index %= len(self.nodes)
        statuses = await self.node_statuses(include_bds=False)
        if not statuses:
            statuses = await self.node_statuses()
        if not statuses:
            return [preferred_rpc_index]

        candidates = [status for status in statuses if not status[2] and status[3]]
        if not candidates:
            return [max(statuses, key=lambda item: item[1])[0]]

        target_height = max(height for _, height, _, _ in candidates)
        healthy = {
            index for index, height, _catching_up, _abci_ok in candidates if height >= target_height
        }
        if healthy:
            return sorted(healthy, key=lambda index: (index != preferred_rpc_index, index))
        return [max(candidates, key=lambda item: item[1])[0]]

    async def healthy_submission_index(self, preferred_rpc_index: int) -> int:
        return (await self.healthy_submission_indices(preferred_rpc_index))[0]

    async def sticky_submission_index(self, wallet: Wallet, preferred_rpc_index: int) -> int:
        public_key = wallet.public_key
        if public_key not in self._submission_affinity:
            self._submission_affinity[public_key] = await self.healthy_submission_index(
                preferred_rpc_index
            )
        return self._submission_affinity[public_key]

    async def healthy_read_node(self) -> LocalnetNode:
        return (await self.healthy_read_nodes())[0]

    async def healthy_read_nodes(self) -> list[LocalnetNode]:
        sample_nodes, _skipped_nodes = await self.healthy_state_sample_nodes()
        non_bds_nodes = [node for node in sample_nodes if not node.bds_node]
        if non_bds_nodes:
            return non_bds_nodes
        if sample_nodes:
            return sample_nodes
        return list(self.nodes)

    async def get_state_from_healthy_node(
        self,
        wallet: Wallet,
        contract: str,
        variable: str,
        *keys: Any,
    ) -> Any:
        errors: list[str] = []
        for node in await self.healthy_read_nodes():
            node_index = self.nodes.index(node)
            try:
                return await self._retry_read(
                    lambda node_index=node_index: self.client(wallet, node_index).get_state(
                        contract,
                        variable,
                        *keys,
                    ),
                    max_attempts=2,
                    initial_delay_seconds=0.05,
                    max_delay_seconds=0.2,
                )
            except Exception as exc:  # noqa: PERF203, BLE001
                errors.append(f"{node.moniker}: {type(exc).__name__}: {exc}")
        raise WorkloadError(
            f"could not read {contract}.{variable} from healthy nodes: {errors[-5:]}"
        )

    async def next_nonce(self, wallet: Wallet, rpc_index: int) -> int:
        return (await self.reserve_nonces(wallet, rpc_index, count=1))[0]

    async def reserve_nonces(
        self,
        wallet: Wallet,
        rpc_index: int,
        *,
        count: int,
    ) -> list[int]:
        if count <= 0:
            raise WorkloadError("nonce reservation count must be positive")
        public_key = wallet.public_key
        async with self._nonce_lock:
            if public_key not in self._next_nonce:
                nonce_rpc_indices = await self.healthy_submission_indices(rpc_index)
                nonce_values: list[int] = []
                nonce_errors: list[str] = []
                for nonce_rpc_index in nonce_rpc_indices:
                    try:
                        nonce_values.append(
                            await self._retry_read(
                                lambda nonce_rpc_index=nonce_rpc_index: tr.get_nonce_async(
                                    self.nodes[nonce_rpc_index].rpc_url,
                                    public_key,
                                    session=self.session,
                                ),
                                max_attempts=2,
                                initial_delay_seconds=0.05,
                                max_delay_seconds=0.2,
                            )
                        )
                    except Exception as exc:  # noqa: PERF203, BLE001
                        nonce_errors.append(
                            f"{self.nodes[nonce_rpc_index].moniker}: {type(exc).__name__}: {exc}"
                        )
                if not nonce_values:
                    raise WorkloadError(
                        f"could not read nonce for {public_key}: {nonce_errors[-5:]}"
                    )
                self._next_nonce[public_key] = max(nonce_values)
            start_nonce = self._next_nonce[public_key]
            self._next_nonce[public_key] += count
        return list(range(start_nonce, start_nonce + count))

    async def broadcast_tx(
        self,
        *,
        label: str,
        wallet: Wallet,
        rpc_index: int,
        contract: str,
        function: str,
        kwargs: dict[str, Any],
        chi: int,
        expected_success: bool,
        expected_message: str | None = None,
        mode: str = "checktx",
        nonce: int | None = None,
    ) -> BroadcastRecord:
        preferred_index = self.submission_index(rpc_index)
        if nonce is None:
            nonce_index = await self.healthy_submission_index(preferred_index)
            reserved_nonce = await self.next_nonce(wallet, nonce_index)
        else:
            reserved_nonce = nonce
        payload = {
            "chain_id": self.chain_id,
            "contract": contract,
            "function": function,
            "kwargs": kwargs,
            "nonce": reserved_nonce,
            "sender": wallet.public_key,
            "chi_supplied": chi,
        }
        tx = tr.create_tx(payload, wallet)
        local_tx_hash = XianAsync._local_tx_hash(tx)
        response: TransactionSubmission | None = None
        last_error: TransportError | None = None

        tried: set[int] = set()
        for attempt in range(max(1, len(self.nodes))):
            if nonce is not None and attempt == 0:
                submission_index = await self.sticky_submission_index(wallet, preferred_index)
            else:
                submission_index = await self.healthy_submission_index(preferred_index + attempt)
            if submission_index in tried:
                continue
            tried.add(submission_index)
            client = self.client(wallet, submission_index)
            try:
                response = await client.send_tx(
                    contract=contract,
                    function=function,
                    kwargs=kwargs,
                    chi=chi,
                    nonce=reserved_nonce,
                    chain_id=self.chain_id,
                    mode=mode,
                    wait_for_tx=False,
                )
                if nonce is not None:
                    self._submission_affinity[wallet.public_key] = submission_index
                break
            except TransportError as exc:
                last_error = exc
                await asyncio.sleep(min(0.25 * (attempt + 1), 1.0))
        if response is None:
            raise WorkloadError(f"{label}: broadcast failed after node failover") from last_error

        return BroadcastRecord(
            label=label,
            contract=contract,
            function=function,
            rpc_url=self.nodes[submission_index].rpc_url,
            sender=wallet.public_key,
            expected_success=expected_success,
            expected_message=expected_message,
            response=response,
            tx=tx,
            tx_hash=response.tx_hash
            or broadcast_response_tx_hash(getattr(response, "response", None))
            or local_tx_hash,
        )

    async def resolve_records(
        self,
        records: list[BroadcastRecord],
        *,
        timeout_seconds: float = RECEIPT_TIMEOUT_SECONDS,
        concurrent: bool = False,
        max_workers: int = 16,
    ) -> None:
        if not records:
            return
        if not concurrent or len(records) < 2:
            for record in records:
                await self._resolve_record(record, timeout_seconds=timeout_seconds)
            return

        worker_count = max(1, min(max_workers, len(records)))
        semaphore = asyncio.Semaphore(worker_count)

        async def resolve_one(record: BroadcastRecord) -> None:
            async with semaphore:
                await self._resolve_record(
                    record,
                    timeout_seconds=timeout_seconds,
                )

        await asyncio.gather(*(resolve_one(record) for record in records))

    async def _resolve_record(
        self,
        record: BroadcastRecord,
        *,
        timeout_seconds: float,
    ) -> None:
        if not record.response.submitted or (
            record.response.accepted is False
            and not self._is_duplicate_tx_message(record.response.message)
        ):
            record.final_success = False
            record.final_message = record.response.message
            self._assert_record(record)
            return

        if not record.tx_hash:
            raise WorkloadError(f"{record.label}: missing tx hash")

        ordered_nodes = [node for node in self.nodes if node.rpc_url == record.rpc_url] + [
            node for node in self.nodes if node.rpc_url != record.rpc_url
        ]
        clients = [
            XianAsync(
                node_url=node.rpc_url,
                chain_id=self.chain_id,
                wallet=self.founder_wallet,
                session=self.session,
            )
            for node in ordered_nodes
        ]
        try:
            receipt = await wait_for_tx_receipt(
                clients=clients,
                tx_hash=record.tx_hash,
                timeout_seconds=timeout_seconds,
            )
        except WorkloadError as exc:
            if record.tx is None:
                raise
            receipt = await self.rebroadcast_and_wait_for_receipt(
                record,
                clients=clients,
                timeout_seconds=timeout_seconds,
                cause=exc,
            )
        record.height = receipt["height"]
        record.tx_index = receipt["tx_index"]
        record.final_success = receipt["success"]
        record.final_message = receipt["result"]
        record.chi_used = receipt["chi_used"]
        record.events = receipt["events"]
        self._assert_record(record)

    async def rebroadcast_and_wait_for_receipt(
        self,
        record: BroadcastRecord,
        *,
        clients: list[XianAsync],
        timeout_seconds: float,
        cause: Exception,
    ) -> dict[str, Any]:
        if not record.tx_hash or record.tx is None:
            raise WorkloadError(f"{record.label}: missing tx metadata for rebroadcast") from cause

        errors: list[str] = []
        tried: set[int] = set()
        for offset in range(max(1, len(self.nodes))):
            node_index = await self.healthy_submission_index(offset)
            if node_index in tried:
                continue
            tried.add(node_index)
            node = self.nodes[node_index]
            try:
                response = await tr.broadcast_tx_wait_async(
                    node.rpc_url,
                    record.tx,
                    session=self.session,
                )
            except Exception as exc:  # noqa: PERF203, BLE001
                errors.append(f"{node.moniker}: {type(exc).__name__}: {exc}")
                continue

            rebroadcast_tx_hash = broadcast_response_tx_hash(response)
            if "error" in response:
                message = response["error"].get("data") or response["error"].get("message")
                if not self._is_duplicate_tx_message(message):
                    errors.append(f"{node.moniker}: {message}")
                    continue
            else:
                checktx_result = response.get("result", {})
                accepted = int(checktx_result.get("code", 1) or 0) == 0
                duplicate_tx = self._is_duplicate_tx_message(checktx_result.get("log"))
                if not accepted and not duplicate_tx:
                    errors.append(
                        f"{node.moniker}: {checktx_result.get('log') or 'CheckTx failed'}"
                    )
                    continue

            if rebroadcast_tx_hash is not None:
                record.tx_hash = rebroadcast_tx_hash
            try:
                return await wait_for_tx_receipt(
                    clients=clients,
                    tx_hash=record.tx_hash,
                    timeout_seconds=timeout_seconds,
                )
            except WorkloadError as exc:
                errors.append(f"{node.moniker}: receipt wait failed: {exc}")

        raise WorkloadError(
            f"{record.label}: tx {record.tx_hash} was not found after rebroadcast; "
            f"errors={errors[-5:]}"
        ) from cause

    @staticmethod
    def _assert_record(record: BroadcastRecord) -> None:
        if record.final_success != record.expected_success:
            raise WorkloadError(
                f"{record.label}: expected success={record.expected_success}, "
                f"got {record.final_success} ({record.final_message})"
            )
        if (
            record.expected_message is not None
            and (record.final_message or "").find(record.expected_message) == -1
        ):
            raise WorkloadError(
                f"{record.label}: expected message containing "
                f"{record.expected_message!r}, got {record.final_message!r}"
            )

    @staticmethod
    def _is_duplicate_tx_message(message: Any) -> bool:
        if not isinstance(message, str):
            return False
        normalized = message.lower()
        return (
            "tx already exists in cache" in normalized
            or "tx already exists in mempool" in normalized
        )

    async def compare_state(
        self,
        queries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        details = []
        all_match = True
        founder = self.founder_wallet
        sample_nodes, skipped_nodes = await self.healthy_state_sample_nodes()

        for query in queries:

            async def fetch_node_state(node: LocalnetNode) -> tuple[str, Any]:
                client = XianAsync(
                    node_url=node.rpc_url,
                    chain_id=self.chain_id,
                    wallet=founder,
                    session=self.session,
                )
                value = await client.get_state(
                    query["contract"],
                    query["variable"],
                    *(str(key) for key in query.get("keys", [])),
                )
                return node.moniker, normalize_value(value)

            results = await asyncio.gather(
                *(fetch_node_state(node) for node in sample_nodes),
                return_exceptions=True,
            )
            values = {}
            errors = {}
            for node, result in zip(sample_nodes, results, strict=True):
                if isinstance(result, Exception):
                    errors[node.moniker] = f"{type(result).__name__}: {result}"
                    continue
                moniker, value = result
                values[moniker] = value
            canonical = {moniker: canonical_json(value) for moniker, value in values.items()}
            unique = set(canonical.values())
            minimum_matches = min(2, len(sample_nodes))
            ok = len(values) >= minimum_matches and len(unique) == 1
            all_match = all_match and ok
            details.append(
                {
                    "label": query["label"],
                    "contract": query["contract"],
                    "variable": query["variable"],
                    "keys": [str(key) for key in query.get("keys", [])],
                    "ok": ok,
                    "values": values,
                    "errors": errors,
                }
            )

        return {
            "ok": all_match,
            "sample_nodes": [node.moniker for node in sample_nodes],
            "skipped_nodes": skipped_nodes,
            "queries": details,
        }

    async def healthy_state_sample_nodes(self) -> tuple[list[LocalnetNode], list[str]]:
        statuses: list[tuple[LocalnetNode, int, bool]] = []
        skipped_nodes: list[str] = []
        for node in self.sample_nodes:
            try:
                payload = await fetch_json(
                    self.session,
                    f"{node.rpc_url}/status",
                    timeout=2.0,
                )
                sync_info = payload["result"]["sync_info"]
                statuses.append(
                    (
                        node,
                        int(sync_info["latest_block_height"]),
                        bool(sync_info.get("catching_up", False)),
                    )
                )
            except Exception as exc:  # noqa: PERF203, BLE001
                skipped_nodes.append(f"{node.moniker}: {type(exc).__name__}: {exc}")

        if not statuses:
            return list(self.sample_nodes), skipped_nodes

        target_height = max(height for _, height, _ in statuses)
        healthy = [
            node
            for node, height, catching_up in statuses
            if not catching_up and height >= target_height
        ]
        minimum_samples = min(2, len(self.sample_nodes))
        if len(healthy) < minimum_samples:
            return list(self.sample_nodes), skipped_nodes

        healthy_names = {node.moniker for node in healthy}
        skipped_nodes.extend(
            f"{node.moniker}: height={height}, catching_up={catching_up}, target={target_height}"
            for node, height, catching_up in statuses
            if node.moniker not in healthy_names
        )
        return healthy, skipped_nodes


def canonical_json(value: Any) -> str:
    return json.dumps(normalize_value(value), sort_keys=True, separators=(",", ":"))


def require_matching_state(scenario: str, state: dict[str, Any]) -> None:
    if state.get("ok") is True:
        return
    raise WorkloadError(f"{scenario}: state comparison failed: {json.dumps(state, sort_keys=True)}")


def compared_state_value(
    state: dict[str, Any],
    query_index: int,
    *,
    scenario: str,
) -> Any:
    queries = state.get("queries", [])
    if query_index >= len(queries):
        raise WorkloadError(f"{scenario}: missing state query #{query_index}: {state}")

    query = queries[query_index]
    values = query.get("values") or {}
    if not values:
        raise WorkloadError(
            f"{scenario}: no state samples for {query.get('label')}: "
            f"errors={query.get('errors', {})}, skipped={state.get('skipped_nodes', [])}"
        )

    for moniker in state.get("sample_nodes", []):
        if moniker in values:
            return values[moniker]
    return next(iter(values.values()))


def fixed(value: int | str | Decimal | ContractingDecimal) -> ContractingDecimal:
    if isinstance(value, ContractingDecimal):
        return value
    return ContractingDecimal(str(value))


def normalize_value(value: Any) -> Any:
    if isinstance(value, ContractingDecimal | Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): normalize_value(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return value


def coerce_numeric(value: Any) -> int | float | Decimal:
    if isinstance(value, int | float | Decimal):
        return value
    if isinstance(value, str):
        if value.isdigit():
            return int(value)
        try:
            return Decimal(value)
        except Exception as exc:  # noqa: BLE001
            raise WorkloadError(f"expected numeric workload value, got {value!r}") from exc
    raise WorkloadError(f"expected numeric workload value, got {value!r}")


def load_network() -> dict[str, Any]:
    if not NETWORK_PATH.exists():
        raise WorkloadError(
            f"localnet metadata not found at {NETWORK_PATH}; run localnet-init first"
        )
    return json.loads(NETWORK_PATH.read_text(encoding="utf-8"))


def read_fixture(path: str) -> str:
    return (WORKLOADS_DIR / path).read_text(encoding="utf-8")


@functools.lru_cache(maxsize=128)
def build_deployment_artifacts(module_name: str, source: str) -> dict[str, Any]:
    return build_contract_artifacts(module_name=module_name, source=source)


def derive_wallet(seed: str, label: str) -> Wallet:
    digest = hashlib.sha256(f"{seed}:{label}".encode("utf-8")).hexdigest()
    return Wallet(private_key=digest)


def deadline_value(*, seconds_from_now: int) -> dict[str, list[int]]:
    future = datetime.now(UTC) + timedelta(seconds=seconds_from_now)
    return {
        "__time__": [
            future.year,
            future.month,
            future.day,
            future.hour,
            future.minute,
            future.second,
            future.microsecond,
        ]
    }


def broadcast_response_tx_hash(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    tx_hash = result.get("hash")
    if not isinstance(tx_hash, str) or not tx_hash:
        return None
    return tx_hash.upper()


def parse_rfc3339_utc(value: str) -> datetime:
    normalized = value.strip()
    tz_suffix = "+00:00" if normalized.endswith("Z") else ""
    if normalized.endswith("Z"):
        normalized = normalized[:-1]
    if "." in normalized:
        head, fractional = normalized.split(".", 1)
        digits = "".join(char for char in fractional if char.isdigit())
        normalized = f"{head}.{digits[:6].ljust(6, '0')}"
    return datetime.fromisoformat(f"{normalized}{tz_suffix}")


async def wait_for_tx_receipt(
    *,
    clients: list[XianAsync],
    tx_hash: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if not clients:
        raise WorkloadError("receipt lookup requires at least one client")
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    client_index = 0

    while time.monotonic() < deadline:
        for offset in range(len(clients)):
            client = clients[(client_index + offset) % len(clients)]
            try:
                receipt = await client.wait_for_tx(
                    tx_hash,
                    timeout_seconds=min(
                        1.5,
                        max(0.5, deadline - time.monotonic()),
                    ),
                    poll_interval_seconds=0.25,
                )
                result = receipt.raw.get("result")
                return receipt_to_workload_result(tx_hash, result, receipt)
            except Exception as exc:  # noqa: PERF203
                last_error = exc
        client_index = (client_index + 1) % len(clients)
        await asyncio.sleep(0.5)

    fallback_receipt = await lookup_tx_receipt_in_recent_blocks(
        clients=clients,
        tx_hash=tx_hash,
        block_scan_window=RECEIPT_FALLBACK_BLOCK_SCAN_WINDOW,
    )
    if fallback_receipt is not None:
        return fallback_receipt

    raise WorkloadError(f"timed out waiting for tx {tx_hash}") from last_error


def receipt_to_workload_result(
    tx_hash: str,
    result: Any,
    receipt,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise WorkloadError(f"tx {tx_hash} returned no result")
    if isinstance(receipt.execution, dict):
        return {
            "height": int(result["height"]),
            "tx_index": int(result["index"]),
            "success": receipt.success,
            "result": receipt.message,
            "events": receipt.execution.get("events", []),
            "chi_used": receipt.execution.get("chi_used"),
        }
    return {
        "height": int(result["height"]),
        "tx_index": int(result["index"]),
        "success": receipt.success,
        "result": receipt.message,
        "events": [],
        "chi_used": None,
    }


async def lookup_tx_receipt_in_recent_blocks(
    *,
    clients: list[XianAsync],
    tx_hash: str,
    block_scan_window: int,
) -> dict[str, Any] | None:
    for client in clients:
        try:
            status = await tr.get_status_async(client.node_url, session=client.session)
            latest_height_raw = (
                status.get("result", {}).get("sync_info", {}).get("latest_block_height")
            )
            latest_height = int(latest_height_raw)
            lookup = await tr._lookup_tx_in_recent_blocks_async(  # noqa: SLF001
                client.node_url,
                tx_hash,
                start_height=max(1, latest_height - block_scan_window + 1),
                end_height=latest_height,
                session=client.session,
            )
            if lookup is None:
                continue
            receipt = client._normalize_tx_lookup(lookup)  # noqa: SLF001
            return receipt_to_workload_result(
                tx_hash,
                lookup.get("result"),
                receipt,
            )
        except Exception:  # noqa: BLE001, PERF203
            continue
    return None


def collect_container_memory(nodes: list[LocalnetNode]) -> dict[str, str]:
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    container_memory = {}
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        name, usage = line.split("\t", 1)
        container_memory[name] = usage.split("/")[0].strip()

    summary = {}
    for node in nodes:
        labels = []
        if node.abci_container == node.cometbft_container:
            usage = container_memory.get(node.cometbft_container)
            if usage:
                labels.append(usage)
        else:
            abci_usage = container_memory.get(node.abci_container)
            comet_usage = container_memory.get(node.cometbft_container)
            if abci_usage:
                labels.append(f"abci={abci_usage}")
            if comet_usage:
                labels.append(f"cometbft={comet_usage}")
        if labels:
            summary[node.moniker] = "  ".join(labels)
    return summary


def require_successful(record: BroadcastRecord) -> None:
    if not record.final_success:
        raise WorkloadError(f"{record.label}: {record.final_message}")


def require_all_successful(
    label: str,
    records: list[BroadcastRecord],
) -> None:
    failures = [record.label for record in records if not record.final_success]
    if failures:
        raise WorkloadError(f"{label}: workload transactions failed: {failures}")


def require_all_admitted(
    label: str,
    records: list[BroadcastRecord],
) -> None:
    failed_records = [
        record
        for record in records
        if (
            not record.response.submitted
            or (
                record.response.accepted is False
                and not WorkloadContext._is_duplicate_tx_message(record.response.message)
            )
        )
    ]
    failures = [record.label for record in failed_records]
    if failures:
        reasons = Counter(str(record.response.message or "unknown") for record in failed_records)
        raise WorkloadError(
            f"{label}: broadcast admission failed: {failures}; reasons={dict(reasons)}"
        )


def summarize_records(records: list[BroadcastRecord]) -> dict[str, Any]:
    heights = [int(record.height) for record in records if record.height is not None]
    return {
        "transaction_count": len(records),
        "successful_transactions": sum(1 for record in records if record.final_success),
        "failed_transactions": sum(1 for record in records if record.final_success is False),
        "min_height": min(heights) if heights else None,
        "max_height": max(heights) if heights else None,
        "tx_hashes": [record.tx_hash for record in records if record.tx_hash is not None],
    }


async def latest_block_height(context: WorkloadContext) -> int:
    node = await context.healthy_read_node()
    payload = await fetch_json(
        context.session,
        f"{node.rpc_url}/status",
        timeout=5.0,
    )
    return int(payload["result"]["sync_info"]["latest_block_height"])


async def summarize_committed_window_for_range(
    context: WorkloadContext,
    *,
    start_height: int,
    end_height: int,
    workload_transactions: int,
    fallback_elapsed_seconds: float,
) -> dict[str, Any]:
    if end_height <= start_height:
        return {}

    blocks = []
    previous_time: datetime | None = None
    first_time: datetime | None = None
    last_time: datetime | None = None
    per_block_tps: list[float] = []
    chain_transactions = 0

    read_node = await context.healthy_read_node()
    for height in range(start_height + 1, end_height + 1):
        payload = await tr.get_block_async(
            read_node.rpc_url,
            height,
            session=context.session,
        )
        block = payload["result"]["block"]
        tx_count = len(block["data"].get("txs") or [])
        chain_transactions += tx_count
        block_time = parse_rfc3339_utc(block["header"]["time"])
        if first_time is None:
            first_time = block_time
        last_time = block_time
        instant_tps = None
        if previous_time is not None:
            delta_seconds = (block_time - previous_time).total_seconds()
            if delta_seconds > 0:
                instant_tps = tx_count / delta_seconds
                per_block_tps.append(instant_tps)
        previous_time = block_time
        blocks.append(
            {
                "height": height,
                "tx_count": tx_count,
                "time": block["header"]["time"],
                "instant_tps": (round(instant_tps, 3) if instant_tps is not None else None),
            }
        )

    window_seconds = (
        (last_time - first_time).total_seconds()
        if first_time is not None and last_time is not None and last_time > first_time
        else fallback_elapsed_seconds
    )
    if window_seconds <= 0:
        window_seconds = fallback_elapsed_seconds

    return {
        "min_height": start_height + 1,
        "max_height": end_height,
        "block_count": len(blocks),
        "chain_transactions": chain_transactions,
        "workload_transactions": workload_transactions,
        "window_seconds": round(window_seconds, 3),
        "committed_workload_tps": round(workload_transactions / window_seconds, 3),
        "committed_chain_tps": round(chain_transactions / window_seconds, 3),
        "peak_block_tps": (round(max(per_block_tps), 3) if per_block_tps else None),
        "median_block_tps": (round(statistics.median(per_block_tps), 3) if per_block_tps else None),
        "blocks": blocks,
    }


async def wait_for_query_predicate(
    context: WorkloadContext,
    queries: list[dict[str, Any]],
    *,
    timeout_seconds: float,
    predicate,
    poll_interval_seconds: float = 0.5,
) -> None:
    if not queries:
        return

    poll_node = await context.healthy_read_node()
    poll_client = XianAsync(
        node_url=poll_node.rpc_url,
        chain_id=context.chain_id,
        wallet=context.founder_wallet,
        session=context.session,
    )
    deadline = time.monotonic() + timeout_seconds
    last_snapshot: dict[str, Any] = {}

    while time.monotonic() < deadline:
        all_ok = True
        for query in queries:
            value = normalize_value(
                await context._retry_read(
                    lambda query=query: poll_client.get_state(
                        query["contract"],
                        query["variable"],
                        *(str(key) for key in query.get("keys", [])),
                    ),
                    max_attempts=3,
                    initial_delay_seconds=0.05,
                    max_delay_seconds=0.5,
                )
            )
            last_snapshot[query["label"]] = value
            if not predicate(query, value):
                all_ok = False
        if all_ok:
            return
        await asyncio.sleep(poll_interval_seconds)

    raise WorkloadError(f"state predicate did not converge before timeout: {last_snapshot}")


async def wait_for_contract_visibility(
    context: WorkloadContext,
    contract_name: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.5,
) -> None:
    sample_nodes, skipped_nodes = await context.healthy_state_sample_nodes()
    clients = [
        XianAsync(
            node_url=node.rpc_url,
            chain_id=context.chain_id,
            wallet=context.founder_wallet,
            session=context.session,
        )
        for node in sample_nodes
    ]
    deadline = time.monotonic() + timeout_seconds
    last_snapshot: dict[str, bool] = {}

    while time.monotonic() < deadline:
        all_visible = True
        for node, client in zip(sample_nodes, clients, strict=True):
            source = await context._retry_read(
                lambda client=client: client.get_contract_source(contract_name),
                max_attempts=3,
                initial_delay_seconds=0.05,
                max_delay_seconds=0.5,
            )
            visible = isinstance(source, str) and bool(source)
            last_snapshot[node.moniker] = visible
            if not visible:
                all_visible = False
        if all_visible:
            return
        await asyncio.sleep(poll_interval_seconds)

    raise WorkloadError(
        f"contract {contract_name!r} did not become visible before timeout: "
        f"{last_snapshot}; skipped={skipped_nodes}"
    )


async def wait_for_mempool_drain(
    context: WorkloadContext,
    *,
    timeout_seconds: float,
    stable_polls: int = 3,
    poll_interval_seconds: float = 0.5,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_counts: dict[str, int] = {}
    consecutive_clean_polls = 0
    flush_submitted = False
    flush_errors: list[str] = []

    async def fetch_unconfirmed_count(node: LocalnetNode) -> int:
        payload = await fetch_json(
            context.session,
            f"{node.rpc_url}/num_unconfirmed_txs",
            timeout=5.0,
        )
        result = payload.get("result", {})
        value = result.get("n_txs")
        if value is None:
            value = result.get("total", 0)
        return int(value or 0)

    while time.monotonic() < deadline:
        all_clean = True
        drain_nodes = [node for node in context.nodes if not node.bds_node]
        if not drain_nodes:
            drain_nodes = list(context.nodes)

        for node in drain_nodes:
            count = await context._retry_read(
                lambda node=node: fetch_unconfirmed_count(node),
                max_attempts=3,
                initial_delay_seconds=0.05,
                max_delay_seconds=0.5,
            )
            last_counts[node.moniker] = count
            if count != 0:
                all_clean = False
        if all_clean:
            consecutive_clean_polls += 1
            if consecutive_clean_polls >= stable_polls:
                return
        else:
            consecutive_clean_polls = 0
            if not flush_submitted and any(count > 0 for count in last_counts.values()):
                flush_submitted = True
                try:
                    await submit_mempool_flush_tx(context, timeout_seconds=timeout_seconds)
                except Exception as exc:  # noqa: BLE001
                    flush_errors.append(f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(poll_interval_seconds)

    error_suffix = f"; flush_errors={flush_errors[-3:]}" if flush_errors else ""
    raise WorkloadError(f"mempool did not drain before timeout: {last_counts}{error_suffix}")


async def submit_mempool_flush_tx(
    context: WorkloadContext,
    *,
    timeout_seconds: float,
) -> None:
    record = await context.broadcast_tx(
        label="mempool flush",
        wallet=context.founder_wallet,
        rpc_index=context.submit_node_index,
        contract="currency",
        function="transfer",
        kwargs={"amount": fixed(1), "to": context.founder_wallet.public_key},
        chi=THROUGHPUT_TRANSFER_TX_CHI,
        expected_success=True,
    )
    await context.resolve_records(
        [record],
        timeout_seconds=min(timeout_seconds, 30.0),
    )
    require_successful(record)


def canonical_record_position(record: BroadcastRecord) -> tuple[int, int]:
    if record.height is None or record.tx_index is None:
        raise WorkloadError(f"{record.label}: missing canonical transaction position metadata")
    return int(record.height), int(record.tx_index)


def record_precedes(left: BroadcastRecord, right: BroadcastRecord) -> bool:
    return canonical_record_position(left) < canonical_record_position(right)


async def broadcast_and_confirm(
    context: WorkloadContext,
    *,
    timeout_seconds: float = RECEIPT_TIMEOUT_SECONDS,
    **kwargs: Any,
) -> BroadcastRecord:
    record = await context.broadcast_tx(**kwargs)
    await context.resolve_records([record], timeout_seconds=timeout_seconds)
    require_successful(record)
    return record


def distribute_operation_counts(total: int, lanes: int) -> list[int]:
    if total <= 0:
        raise WorkloadError("operation count must be positive")
    if lanes <= 0:
        raise WorkloadError("lane count must be positive")
    counts = [total // lanes] * lanes
    for index in range(total % lanes):
        counts[index] += 1
    return counts


async def fund_wallets(
    context: WorkloadContext,
    *,
    seed_wallet: Wallet,
    worker_wallets: list[Wallet],
    amount: ContractingDecimal,
    chi: int,
    label_prefix: str,
    timeout_seconds: float = RECEIPT_TIMEOUT_SECONDS,
) -> list[BroadcastRecord]:
    records: list[BroadcastRecord] = []
    seed_rpc_index = context.submit_node_index
    for index, wallet in enumerate(worker_wallets):
        records.append(
            await broadcast_and_confirm(
                context,
                label=f"{label_prefix} #{index}",
                wallet=seed_wallet,
                rpc_index=seed_rpc_index,
                contract="currency",
                function="transfer",
                kwargs={"amount": amount, "to": wallet.public_key},
                chi=chi,
                expected_success=True,
                timeout_seconds=timeout_seconds,
            )
        )
    return records


async def broadcast_plans(
    context: WorkloadContext,
    plans: list[dict[str, Any]],
    *,
    submit_workers: int,
) -> list[BroadcastRecord]:
    if not plans:
        return []

    worker_count = max(1, min(submit_workers, len(plans)))
    semaphore = asyncio.Semaphore(worker_count)
    records: list[BroadcastRecord | None] = [None] * len(plans)
    plan_lanes: dict[str, list[tuple[int, dict[str, Any]]]] = {}

    for index, plan in enumerate(plans):
        wallet = plan.get("wallet")
        if isinstance(wallet, Wallet):
            lane_key = wallet.public_key
        else:
            lane_key = f"plan-{index}"
        plan_lanes.setdefault(lane_key, []).append((index, plan))

    for lane_entries in plan_lanes.values():
        lane_entries.sort(
            key=lambda item: (
                item[1].get("nonce") is None,
                int(item[1].get("nonce") or 0),
                item[0],
            )
        )

    async def broadcast_one(index: int, plan: dict[str, Any]) -> None:
        async with semaphore:
            records[index] = await context.broadcast_tx(**plan)

    async def broadcast_lane(
        lane_entries: list[tuple[int, dict[str, Any]]],
    ) -> None:
        for index, plan in lane_entries:
            await broadcast_one(index, plan)

    await asyncio.gather(*(broadcast_lane(lane_entries) for lane_entries in plan_lanes.values()))
    return [record for record in records if record is not None]


async def summarize_committed_window(
    context: WorkloadContext,
    records: list[BroadcastRecord],
    *,
    fallback_elapsed_seconds: float,
) -> dict[str, Any]:
    heights = sorted({int(record.height) for record in records if record.height is not None})
    if not heights:
        return {}

    blocks = []
    previous_time: datetime | None = None
    per_block_tps: list[float] = []
    read_node = await context.healthy_read_node()
    for height in range(heights[0], heights[-1] + 1):
        payload = await tr.get_block_async(
            read_node.rpc_url,
            height,
            session=context.session,
        )
        block = payload["result"]["block"]
        tx_count = len(block["data"].get("txs") or [])
        block_time = parse_rfc3339_utc(block["header"]["time"])
        instant_tps = None
        if previous_time is not None:
            delta_seconds = (block_time - previous_time).total_seconds()
            if delta_seconds > 0:
                instant_tps = tx_count / delta_seconds
                per_block_tps.append(instant_tps)
        blocks.append(
            {
                "height": height,
                "tx_count": tx_count,
                "time": block["header"]["time"],
                "instant_tps": (round(instant_tps, 3) if instant_tps is not None else None),
            }
        )
        previous_time = block_time

    first_time = parse_rfc3339_utc(blocks[0]["time"])
    last_time = parse_rfc3339_utc(blocks[-1]["time"])
    window_seconds = (last_time - first_time).total_seconds()
    if window_seconds <= 0:
        window_seconds = fallback_elapsed_seconds

    chain_transactions = sum(block["tx_count"] for block in blocks)
    workload_transactions = len(records)
    return {
        "min_height": heights[0],
        "max_height": heights[-1],
        "block_count": len(blocks),
        "chain_transactions": chain_transactions,
        "workload_transactions": workload_transactions,
        "window_seconds": round(window_seconds, 3),
        "committed_workload_tps": round(workload_transactions / window_seconds, 3),
        "committed_chain_tps": round(chain_transactions / window_seconds, 3),
        "peak_block_tps": round(max(per_block_tps), 3) if per_block_tps else None,
        "median_block_tps": round(statistics.median(per_block_tps), 3) if per_block_tps else None,
        "blocks": blocks,
    }


async def broadcast_funding_record(
    context: WorkloadContext,
    *,
    founder: Wallet,
    wallet: Wallet,
    index: int,
    gas_amount: ContractingDecimal,
    token_amount: ContractingDecimal,
    token_a: str,
    token_b: str,
    timeout_seconds: float = RECEIPT_TIMEOUT_SECONDS,
) -> list[BroadcastRecord]:
    rpc_index = index % len(context.nodes)
    return [
        await broadcast_and_confirm(
            context,
            label=f"fund gas trader-{index}",
            wallet=founder,
            rpc_index=rpc_index,
            contract="currency",
            function="transfer",
            kwargs={"amount": gas_amount, "to": wallet.public_key},
            chi=TOKEN_TX_CHI,
            expected_success=True,
            timeout_seconds=timeout_seconds,
        ),
        await broadcast_and_confirm(
            context,
            label=f"fund tokenA trader-{index}",
            wallet=founder,
            rpc_index=rpc_index,
            contract=token_a,
            function="transfer",
            kwargs={"amount": token_amount, "to": wallet.public_key},
            chi=TOKEN_TX_CHI,
            expected_success=True,
            timeout_seconds=timeout_seconds,
        ),
        await broadcast_and_confirm(
            context,
            label=f"fund tokenB trader-{index}",
            wallet=founder,
            rpc_index=rpc_index,
            contract=token_b,
            function="transfer",
            kwargs={"amount": token_amount, "to": wallet.public_key},
            chi=TOKEN_TX_CHI,
            expected_success=True,
            timeout_seconds=timeout_seconds,
        ),
    ]


async def broadcast_approval_record(
    context: WorkloadContext,
    *,
    wallet: Wallet,
    wallet_index: int,
    dex_contract: str,
    token_name: str,
    timeout_seconds: float = RECEIPT_TIMEOUT_SECONDS,
) -> BroadcastRecord:
    return await broadcast_and_confirm(
        context,
        label=f"approve {token_name} wallet-{wallet_index}",
        wallet=wallet,
        rpc_index=wallet_index % len(context.nodes),
        contract=token_name,
        function="approve",
        kwargs={"amount": fixed(1_000_000), "to": dex_contract},
        chi=TOKEN_TX_CHI,
        expected_success=True,
        timeout_seconds=timeout_seconds,
    )


async def run_counter_basic(
    context: WorkloadContext,
    *,
    seed: str,
    operations: int,
    receipt_resolution: str,
    receipt_workers: int,
    receipt_timeout_seconds: float,
) -> dict[str, Any]:
    founder = context.founder_wallet
    worker_wallets = [
        derive_wallet(seed, f"counter-worker-{index}") for index in range(len(context.nodes))
    ]
    suffix = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    contract_name = f"con_counter_{suffix}"
    contract_code = read_fixture("counter_basic/con_counter.py")

    print(f"Deploying {contract_name}...")
    deploy_record = await context.broadcast_tx(
        label="deploy counter",
        wallet=founder,
        rpc_index=0,
        contract="submission",
        function="submit_contract",
        kwargs=context.contract_submission_kwargs(
            name=contract_name,
            code=contract_code,
        ),
        chi=COUNTER_DEPLOY_CHI,
        expected_success=True,
    )
    await context.resolve_records([deploy_record], timeout_seconds=receipt_timeout_seconds)
    require_successful(deploy_record)

    funding_records = []
    for index, wallet in enumerate(worker_wallets):
        funding_records.append(
            await broadcast_and_confirm(
                context,
                label=f"fund counter-worker-{index}",
                wallet=founder,
                rpc_index=index,
                contract="currency",
                function="transfer",
                kwargs={"amount": fixed(5_000), "to": wallet.public_key},
                chi=COUNTER_TX_CHI,
                expected_success=True,
                timeout_seconds=receipt_timeout_seconds,
            )
        )

    print(f"Broadcasting {operations} counter_basic operations...")
    records: list[BroadcastRecord] = []
    pending_records: list[BroadcastRecord] = []
    expected_counter = 0

    async def flush_pending_records() -> None:
        if not pending_records:
            return
        await context.resolve_records(
            pending_records,
            timeout_seconds=receipt_timeout_seconds,
            concurrent=receipt_resolution == "concurrent",
            max_workers=receipt_workers,
        )
        pending_records.clear()

    for index in range(operations):
        rpc_index = index % len(context.nodes)
        sender_wallet = worker_wallets[rpc_index]
        if index % 3 == 0:
            recipient = worker_wallets[(rpc_index + 1) % len(worker_wallets)].public_key
            record = await context.broadcast_tx(
                label=f"transfer #{index}",
                wallet=sender_wallet,
                rpc_index=rpc_index,
                contract="currency",
                function="transfer",
                kwargs={"amount": fixed(1), "to": recipient},
                chi=COUNTER_TX_CHI,
                expected_success=True,
                mode="async",
            )
        elif index % 3 == 1:
            expected_counter += 1
            record = await context.broadcast_tx(
                label=f"increment #{index}",
                wallet=sender_wallet,
                rpc_index=rpc_index,
                contract=contract_name,
                function="increment",
                kwargs={},
                chi=COUNTER_TX_CHI,
                expected_success=True,
                mode="async",
            )
        else:
            amount = (index % 11) + 1
            expected_counter += amount
            record = await context.broadcast_tx(
                label=f"add #{index}",
                wallet=sender_wallet,
                rpc_index=rpc_index,
                contract=contract_name,
                function="add",
                kwargs={"amount": amount},
                chi=COUNTER_TX_CHI,
                expected_success=True,
                mode="async",
            )
        records.append(record)
        pending_records.append(record)
        if len(pending_records) >= COUNTER_BROADCAST_BATCH_SIZE:
            await flush_pending_records()

    await flush_pending_records()
    successes = sum(1 for record in records if record.final_success)
    if successes != len(records):
        raise WorkloadError("counter_basic: one or more workload transactions failed")

    state = await context.compare_state(
        [
            {
                "label": "counter value",
                "contract": contract_name,
                "variable": "v",
            }
        ]
    )
    require_matching_state("counter_basic", state)
    counter_value = compared_state_value(state, 0, scenario="counter_basic")
    counter_value = coerce_numeric(counter_value)
    if counter_value != expected_counter:
        raise WorkloadError(
            f"counter_basic: expected counter value {expected_counter}, got {counter_value}"
        )

    event_counter = Counter()
    for record in records:
        for event in record.events or []:
            event_counter[f"{event.get('contract')}:{event.get('event')}"] += 1

    return {
        "scenario": "counter_basic",
        "contract_name": contract_name,
        "expected_counter": expected_counter,
        "funding_transactions": len(funding_records),
        "transaction_count": len(records),
        "successful_transactions": successes,
        "failed_transactions": len(records) - successes,
        "event_counts": dict(sorted(event_counter.items())),
        "state": state,
    }


def render_dex_contract(
    *,
    dex_pairs_contract: str,
) -> str:
    source = read_fixture("dex_mixed/con_dex.py")
    needle = 'DEX_PAIRS = "con_pairs"'
    replacement = f'DEX_PAIRS = "{dex_pairs_contract}"'
    if needle not in source:
        raise WorkloadError("DEX workload template no longer contains the expected pair constant")
    return source.replace(needle, replacement, 1)


async def run_dex_mixed(
    context: WorkloadContext,
    *,
    seed: str,
    rounds: int,
    receipt_resolution: str,
    receipt_workers: int,
    receipt_timeout_seconds: float,
) -> dict[str, Any]:
    founder = context.founder_wallet
    suffix = hashlib.sha256(f"dex:{seed}".encode("utf-8")).hexdigest()[:8]
    token_a = f"con_tokena_{suffix}"
    token_b = f"con_tokenb_{suffix}"
    pairs_contract = f"con_pairs_{suffix}"
    dex_contract = f"con_dex_{suffix}"
    token_code = read_fixture("dex_mixed/token_fixture.py")
    pairs_code = read_fixture("dex_mixed/con_pairs.py")
    dex_code = render_dex_contract(dex_pairs_contract=pairs_contract)

    print("Deploying dex_mixed contract pack...")
    deployment_records = [
        await broadcast_and_confirm(
            context,
            label=f"deploy {token_a}",
            wallet=founder,
            rpc_index=0,
            contract="submission",
            function="submit_contract",
            kwargs=context.contract_submission_kwargs(
                name=token_a,
                code=token_code,
                constructor_args={
                    "owner": founder.public_key,
                    "supply": fixed(5_000_000),
                    "name": "Workload Token A",
                    "symbol": "WTA",
                },
            ),
            chi=TOKEN_DEPLOY_CHI,
            expected_success=True,
            timeout_seconds=receipt_timeout_seconds,
        ),
        await broadcast_and_confirm(
            context,
            label=f"deploy {token_b}",
            wallet=founder,
            rpc_index=1,
            contract="submission",
            function="submit_contract",
            kwargs=context.contract_submission_kwargs(
                name=token_b,
                code=token_code,
                constructor_args={
                    "owner": founder.public_key,
                    "supply": fixed(5_000_000),
                    "name": "Workload Token B",
                    "symbol": "WTB",
                },
            ),
            chi=TOKEN_DEPLOY_CHI,
            expected_success=True,
            timeout_seconds=receipt_timeout_seconds,
        ),
        await broadcast_and_confirm(
            context,
            label=f"deploy {pairs_contract}",
            wallet=founder,
            rpc_index=2,
            contract="submission",
            function="submit_contract",
            kwargs=context.contract_submission_kwargs(
                name=pairs_contract,
                code=pairs_code,
            ),
            chi=PAIR_DEPLOY_CHI,
            expected_success=True,
            timeout_seconds=receipt_timeout_seconds,
        ),
        await broadcast_and_confirm(
            context,
            label=f"deploy {dex_contract}",
            wallet=founder,
            rpc_index=3,
            contract="submission",
            function="submit_contract",
            kwargs=context.contract_submission_kwargs(
                name=dex_contract,
                code=dex_code,
            ),
            chi=DEX_DEPLOY_CHI,
            expected_success=True,
            timeout_seconds=receipt_timeout_seconds,
        ),
    ]

    trader_wallets = [derive_wallet(seed, f"dex-trader-{index}") for index in range(4)]
    print(f"Funding {len(trader_wallets)} deterministic trader wallets...")
    funding_records: list[BroadcastRecord] = []
    for index, wallet in enumerate(trader_wallets):
        funding_records.extend(
            await broadcast_funding_record(
                context,
                founder=founder,
                wallet=wallet,
                index=index,
                gas_amount=fixed(50_000),
                token_amount=fixed(25_000),
                token_a=token_a,
                token_b=token_b,
                timeout_seconds=receipt_timeout_seconds,
            )
        )

    approval_records: list[BroadcastRecord] = []
    for wallet_index, wallet in enumerate([founder, *trader_wallets[:-1]]):
        approval_records.append(
            await broadcast_approval_record(
                context,
                wallet=wallet,
                wallet_index=wallet_index,
                dex_contract=dex_contract,
                token_name=token_a,
                timeout_seconds=receipt_timeout_seconds,
            )
        )
        approval_records.append(
            await broadcast_approval_record(
                context,
                wallet=wallet,
                wallet_index=wallet_index + len(context.nodes),
                dex_contract=dex_contract,
                token_name=token_b,
                timeout_seconds=receipt_timeout_seconds,
            )
        )

    print("Seeding initial liquidity...")
    initial_liquidity = await context.broadcast_tx(
        label="initial addLiquidity",
        wallet=founder,
        rpc_index=0,
        contract=dex_contract,
        function="addLiquidity",
        kwargs={
            "tokenA": token_a,
            "tokenB": token_b,
            "amountADesired": fixed(250_000),
            "amountBDesired": fixed(250_000),
            "amountAMin": fixed(240_000),
            "amountBMin": fixed(240_000),
            "to": founder.public_key,
            "deadline": deadline_value(seconds_from_now=300),
        },
        chi=DEX_TX_CHI,
        expected_success=True,
    )
    await context.resolve_records(
        [initial_liquidity],
        timeout_seconds=receipt_timeout_seconds,
    )
    require_successful(initial_liquidity)

    token0, token1 = sorted((token_a, token_b))
    pair_id = await context.get_state_from_healthy_node(
        founder,
        pairs_contract,
        "toks_to_pair",
        token0,
        token1,
    )
    if not isinstance(pair_id, int):
        raise WorkloadError(f"dex_mixed: expected pair id, got {pair_id!r}")

    lp_approval = await broadcast_and_confirm(
        context,
        label="approve LP liquidity for dex",
        wallet=founder,
        rpc_index=0,
        contract=pairs_contract,
        function="liqApprove",
        kwargs={"pair": pair_id, "amount": fixed(10_000), "to": dex_contract},
        chi=DEX_TX_CHI,
        expected_success=True,
        timeout_seconds=receipt_timeout_seconds,
    )

    print(f"Broadcasting dex_mixed workload plan ({rounds} rounds)...")
    plan_records: list[BroadcastRecord] = []
    approved_traders = trader_wallets[:-1]
    unapproved_trader = trader_wallets[-1]
    rng = random.Random(f"dex-plan:{seed}")

    for round_index in range(rounds):
        trader_a = approved_traders[round_index % len(approved_traders)]
        trader_b = approved_traders[(round_index + 1) % len(approved_traders)]
        trader_c = approved_traders[(round_index + 2) % len(approved_traders)]
        rpc_index = round_index % len(context.nodes)
        trade_a_amount = fixed(10 + round_index)
        trade_b_amount = fixed(7 + round_index)
        impossible_min = fixed(10_000_000 + round_index)

        round_records = [
            await context.broadcast_tx(
                label=f"swap tokenA->tokenB round-{round_index}",
                wallet=trader_a,
                rpc_index=rpc_index,
                contract=dex_contract,
                function="swapExactTokenForToken",
                kwargs={
                    "amountIn": trade_a_amount,
                    "amountOutMin": fixed("0.0001"),
                    "pair": pair_id,
                    "src": token_a,
                    "to": trader_a.public_key,
                    "deadline": deadline_value(seconds_from_now=300),
                },
                chi=DEX_TX_CHI,
                expected_success=True,
            ),
            await context.broadcast_tx(
                label=f"swap tokenB->tokenA round-{round_index}",
                wallet=trader_b,
                rpc_index=(rpc_index + 1) % len(context.nodes),
                contract=dex_contract,
                function="swapExactTokenForToken",
                kwargs={
                    "amountIn": trade_b_amount,
                    "amountOutMin": fixed("0.0001"),
                    "pair": pair_id,
                    "src": token_b,
                    "to": trader_b.public_key,
                    "deadline": deadline_value(seconds_from_now=300),
                },
                chi=DEX_TX_CHI,
                expected_success=True,
            ),
            await context.broadcast_tx(
                label=f"expired swap round-{round_index}",
                wallet=trader_c,
                rpc_index=(rpc_index + 2) % len(context.nodes),
                contract=dex_contract,
                function="swapExactTokenForToken",
                kwargs={
                    "amountIn": fixed(5),
                    "amountOutMin": fixed("0.0001"),
                    "pair": pair_id,
                    "src": token_a,
                    "to": trader_c.public_key,
                    "deadline": deadline_value(seconds_from_now=-5),
                },
                chi=DEX_TX_CHI,
                expected_success=False,
                expected_message="EXPIRED",
            ),
            await context.broadcast_tx(
                label=f"impossible output round-{round_index}",
                wallet=founder,
                rpc_index=(rpc_index + 3) % len(context.nodes),
                contract=dex_contract,
                function="swapExactTokenForToken",
                kwargs={
                    "amountIn": fixed(5),
                    "amountOutMin": impossible_min,
                    "pair": pair_id,
                    "src": token_b,
                    "to": founder.public_key,
                    "deadline": deadline_value(seconds_from_now=300),
                },
                chi=DEX_TX_CHI,
                expected_success=False,
                expected_message="INSUFFICIENT_OUTPUT_AMOUNT",
            ),
        ]
        await context.resolve_records(
            round_records,
            timeout_seconds=receipt_timeout_seconds,
            concurrent=receipt_resolution == "concurrent",
            max_workers=receipt_workers,
        )
        plan_records.extend(round_records)

    plan_records.append(
        await context.broadcast_tx(
            label="insufficient allowance swap",
            wallet=unapproved_trader,
            rpc_index=rng.randrange(len(context.nodes)),
            contract=dex_contract,
            function="swapExactTokenForToken",
            kwargs={
                "amountIn": fixed(9),
                "amountOutMin": fixed("0.0001"),
                "pair": pair_id,
                "src": token_a,
                "to": unapproved_trader.public_key,
                "deadline": deadline_value(seconds_from_now=300),
            },
            chi=DEX_TX_CHI,
            expected_success=False,
            expected_message="approved",
        )
    )
    plan_records.append(
        await context.broadcast_tx(
            label="invalid pair swap",
            wallet=approved_traders[0],
            rpc_index=rng.randrange(len(context.nodes)),
            contract=dex_contract,
            function="swapExactTokenForToken",
            kwargs={
                "amountIn": fixed(4),
                "amountOutMin": fixed("0.0001"),
                "pair": int(pair_id) + 999,
                "src": token_a,
                "to": approved_traders[0].public_key,
                "deadline": deadline_value(seconds_from_now=300),
            },
            chi=DEX_TX_CHI,
            expected_success=False,
            expected_message="INSUFFICIENT_LIQUIDITY",
        )
    )
    plan_records.append(
        await context.broadcast_tx(
            label="remove liquidity",
            wallet=founder,
            rpc_index=rng.randrange(len(context.nodes)),
            contract=dex_contract,
            function="removeLiquidity",
            kwargs={
                "tokenA": token_a,
                "tokenB": token_b,
                "liquidity": fixed(1_000),
                "amountAMin": fixed("0.0001"),
                "amountBMin": fixed("0.0001"),
                "to": founder.public_key,
                "deadline": deadline_value(seconds_from_now=300),
            },
            chi=DEX_TX_CHI,
            expected_success=True,
        )
    )

    await context.resolve_records(
        plan_records[-3:],
        timeout_seconds=receipt_timeout_seconds,
    )

    successful_records = [record for record in plan_records if record.final_success]
    failed_records = [record for record in plan_records if not record.final_success]
    if len(successful_records) + len(failed_records) != len(plan_records):
        raise WorkloadError("dex_mixed: unresolved plan records")

    event_counter = Counter()
    for record in [
        *deployment_records,
        *funding_records,
        *approval_records,
        initial_liquidity,
        lp_approval,
        *plan_records,
    ]:
        for event in record.events or []:
            event_counter[f"{event.get('contract')}:{event.get('event')}"] += 1

    required_events = {
        f"{pairs_contract}:PairCreated": 1,
        f"{pairs_contract}:Mint": 1,
        f"{pairs_contract}:Swap": 1,
        f"{pairs_contract}:Burn": 1,
    }
    missing = {
        key: minimum
        for key, minimum in required_events.items()
        if event_counter.get(key, 0) < minimum
    }
    if missing:
        raise WorkloadError(f"dex_mixed: missing expected events {missing}")

    state = await context.compare_state(
        [
            {
                "label": "pair count",
                "contract": pairs_contract,
                "variable": "pairs_num",
            },
            {
                "label": "pair id",
                "contract": pairs_contract,
                "variable": "toks_to_pair",
                "keys": [token0, token1],
            },
            {
                "label": "reserve0",
                "contract": pairs_contract,
                "variable": "pairs",
                "keys": [pair_id, "reserve0"],
            },
            {
                "label": "reserve1",
                "contract": pairs_contract,
                "variable": "pairs",
                "keys": [pair_id, "reserve1"],
            },
            {
                "label": "total supply",
                "contract": pairs_contract,
                "variable": "pairs",
                "keys": [pair_id, "totalSupply"],
            },
            {
                "label": "tokenA founder balance",
                "contract": token_a,
                "variable": "balances",
                "keys": [founder.public_key],
            },
            {
                "label": "tokenA pool balance",
                "contract": token_a,
                "variable": "balances",
                "keys": [pairs_contract],
            },
            {
                "label": "tokenB founder balance",
                "contract": token_b,
                "variable": "balances",
                "keys": [founder.public_key],
            },
            {
                "label": "tokenB pool balance",
                "contract": token_b,
                "variable": "balances",
                "keys": [pairs_contract],
            },
            {
                "label": "tokenA trader0 balance",
                "contract": token_a,
                "variable": "balances",
                "keys": [approved_traders[0].public_key],
            },
            {
                "label": "tokenB trader0 balance",
                "contract": token_b,
                "variable": "balances",
                "keys": [approved_traders[0].public_key],
            },
        ]
    )

    require_matching_state("dex_mixed", state)
    reserve0 = coerce_numeric(compared_state_value(state, 2, scenario="dex_mixed"))
    reserve1 = coerce_numeric(compared_state_value(state, 3, scenario="dex_mixed"))
    total_supply = coerce_numeric(compared_state_value(state, 4, scenario="dex_mixed"))
    if reserve0 <= 0 or reserve1 <= 0 or total_supply <= 0:
        raise WorkloadError(
            f"dex_mixed: unexpected non-positive reserves or supply "
            f"(reserve0={reserve0}, reserve1={reserve1}, total_supply={total_supply})"
        )

    return {
        "scenario": "dex_mixed",
        "contracts": {
            "token_a": token_a,
            "token_b": token_b,
            "pairs": pairs_contract,
            "dex": dex_contract,
        },
        "pair_id": pair_id,
        "rounds": rounds,
        "deployment_transactions": len(deployment_records),
        "funding_transactions": len(funding_records),
        "approval_transactions": len(approval_records),
        "workload_transactions": len(plan_records),
        "successful_transactions": len(successful_records),
        "failed_transactions": len(failed_records),
        "expected_failures": sum(1 for record in plan_records if not record.expected_success),
        "event_counts": dict(sorted(event_counter.items())),
        "state": state,
        "failures": [
            {
                "label": record.label,
                "message": record.final_message,
            }
            for record in failed_records
        ],
    }


async def run_parallel_probe(
    context: WorkloadContext,
    *,
    seed: str,
    receipt_resolution: str,
    receipt_workers: int,
    receipt_timeout_seconds: float,
) -> dict[str, Any]:
    founder = context.founder_wallet
    parallel_config = context.network.get("parallel_execution", {})
    configured_min_transactions = int(parallel_config.get("min_transactions", 8) or 8)
    parallel_batch_size = max(configured_min_transactions + 2, len(context.nodes) * 2)

    suffix = hashlib.sha256(f"parallel:{seed}:{time.time_ns()}".encode("utf-8")).hexdigest()[:8]
    contract_name = f"con_parallel_probe_{suffix}"
    contract_code = read_fixture("parallel_probe/con_parallel_probe.py")
    writer_wallets = [
        derive_wallet(seed, f"parallel-probe-writer-{index}")
        for index in range(parallel_batch_size)
    ]
    same_sender_wallet = derive_wallet(seed, "parallel-probe-same-sender")
    funded_wallets = [*writer_wallets, same_sender_wallet]

    print(f"Deploying {contract_name}...")
    deploy_record = await context.broadcast_tx(
        label="deploy parallel_probe",
        wallet=founder,
        rpc_index=0,
        contract="submission",
        function="submit_contract",
        kwargs=context.contract_submission_kwargs(
            name=contract_name,
            code=contract_code,
        ),
        chi=PARALLEL_PROBE_DEPLOY_CHI,
        expected_success=True,
    )
    await context.resolve_records([deploy_record], timeout_seconds=receipt_timeout_seconds)
    require_successful(deploy_record)

    funding_records = []
    for index, wallet in enumerate(funded_wallets):
        funding_records.append(
            await broadcast_and_confirm(
                context,
                label=f"fund parallel-probe-{index}",
                wallet=founder,
                rpc_index=index % len(context.nodes),
                contract="currency",
                function="transfer",
                kwargs={"amount": fixed(5_000), "to": wallet.public_key},
                chi=PARALLEL_PROBE_TX_CHI,
                expected_success=True,
                timeout_seconds=receipt_timeout_seconds,
            )
        )

    print(f"Broadcasting parallel_probe batches ({parallel_batch_size} tx per batch)...")

    non_conflicting_group = f"unique-{suffix}"
    non_conflicting_records: list[BroadcastRecord] = []
    for index, wallet in enumerate(writer_wallets):
        value = index + 1
        non_conflicting_records.append(
            await context.broadcast_tx(
                label=f"parallel unique #{index}",
                wallet=wallet,
                rpc_index=index % len(context.nodes),
                contract=contract_name,
                function="write_value",
                kwargs={
                    "group": non_conflicting_group,
                    "key": f"unique-{index}",
                    "value": value,
                },
                chi=PARALLEL_PROBE_TX_CHI,
                expected_success=True,
            )
        )
    await context.resolve_records(
        non_conflicting_records,
        timeout_seconds=receipt_timeout_seconds,
        concurrent=receipt_resolution == "concurrent",
        max_workers=receipt_workers,
    )
    require_all_successful("parallel_probe non_conflicting", non_conflicting_records)

    same_sender_group = f"same-{suffix}"
    same_sender_records: list[BroadcastRecord] = []
    for index in range(parallel_batch_size):
        value = 100 + index
        same_sender_records.append(
            await context.broadcast_tx(
                label=f"parallel same-sender #{index}",
                wallet=same_sender_wallet,
                rpc_index=0,
                contract=contract_name,
                function="write_value",
                kwargs={
                    "group": same_sender_group,
                    "key": f"same-{index}",
                    "value": value,
                },
                chi=PARALLEL_PROBE_TX_CHI,
                expected_success=True,
            )
        )
    await context.resolve_records(
        same_sender_records,
        timeout_seconds=receipt_timeout_seconds,
        concurrent=receipt_resolution == "concurrent",
        max_workers=receipt_workers,
    )
    require_all_successful("parallel_probe same_sender", same_sender_records)

    max_ordering_attempts = 6

    async def execute_read_after_write_attempt(attempt: int) -> dict[str, Any]:
        flag_group = f"flag-{suffix}-{attempt}"
        tail_group = f"read-tail-{suffix}-{attempt}"
        observation_tag = f"{flag_group}-observation"
        records: list[BroadcastRecord] = [
            await context.broadcast_tx(
                label=f"parallel set-flag attempt {attempt}",
                wallet=writer_wallets[0],
                rpc_index=0,
                contract=contract_name,
                function="set_flag",
                kwargs={"group": flag_group, "value": 7},
                chi=PARALLEL_PROBE_TX_CHI,
                expected_success=True,
            ),
            await context.broadcast_tx(
                label=f"parallel observe-flag attempt {attempt}",
                wallet=writer_wallets[1],
                rpc_index=0,
                contract=contract_name,
                function="observe_flag",
                kwargs={"group": flag_group, "tag": observation_tag},
                chi=PARALLEL_PROBE_TX_CHI,
                expected_success=True,
            ),
        ]
        for index in range(2, parallel_batch_size):
            value = 200 + index
            records.append(
                await context.broadcast_tx(
                    label=f"parallel read-tail attempt {attempt} #{index}",
                    wallet=writer_wallets[index],
                    rpc_index=0,
                    contract=contract_name,
                    function="write_value",
                    kwargs={
                        "group": tail_group,
                        "key": f"tail-{index}",
                        "value": value,
                    },
                    chi=PARALLEL_PROBE_TX_CHI,
                    expected_success=True,
                )
            )
        await context.resolve_records(
            records,
            timeout_seconds=receipt_timeout_seconds,
            concurrent=receipt_resolution == "concurrent",
            max_workers=receipt_workers,
        )
        require_all_successful(
            f"parallel_probe read_after_write attempt {attempt}",
            records,
        )

        observed = await context.get_state_from_healthy_node(
            founder,
            contract_name,
            "observations",
            observation_tag,
        )
        set_record, observe_record = records[:2]
        expected = 7 if record_precedes(set_record, observe_record) else 0
        if observed != expected:
            raise WorkloadError(
                "parallel_probe: unexpected read-after-write observation "
                f"{observed!r}, expected {expected!r}"
            )

        return {
            "attempt": attempt,
            "records": records,
            "tag": observation_tag,
            "observed_observation": observed,
            "expected_observation": expected,
            "exercised_conflict_path": (
                record_precedes(set_record, observe_record)
                and set_record.height == observe_record.height
            ),
        }

    read_after_write_attempts: list[dict[str, Any]] = []
    read_after_write_result: dict[str, Any] | None = None
    for attempt in range(1, max_ordering_attempts + 1):
        attempt_result = await execute_read_after_write_attempt(attempt)
        read_after_write_attempts.append(
            {
                "attempt": attempt_result["attempt"],
                "tag": attempt_result["tag"],
                "observed_observation": attempt_result["observed_observation"],
                "expected_observation": attempt_result["expected_observation"],
                "exercised_conflict_path": attempt_result["exercised_conflict_path"],
                "records": summarize_records(attempt_result["records"]),
            }
        )
        if attempt_result["exercised_conflict_path"]:
            read_after_write_result = attempt_result
            break
    if read_after_write_result is None:
        raise WorkloadError(
            "parallel_probe: could not obtain canonical writer-before-reader "
            f"ordering for read_after_write after {max_ordering_attempts} attempts"
        )

    read_after_write_records = read_after_write_result["records"]
    read_after_write_tag = read_after_write_result["tag"]
    read_after_write_observation = read_after_write_result["observed_observation"]
    expected_read_after_write_observation = read_after_write_result["expected_observation"]

    async def execute_prefix_scan_attempt(attempt: int) -> dict[str, Any]:
        group = f"prefix-{suffix}-{attempt}"
        observation_tag = f"{group}-observation"
        seed_value = 13
        snapshot_record = None
        records: list[BroadcastRecord] = []
        value_records: list[tuple[BroadcastRecord, int]] = []

        prefix_write_record = await context.broadcast_tx(
            label=f"parallel prefix-write attempt {attempt}",
            wallet=writer_wallets[0],
            rpc_index=0,
            contract=contract_name,
            function="write_value",
            kwargs={"group": group, "key": "seed", "value": seed_value},
            chi=PARALLEL_PROBE_TX_CHI,
            expected_success=True,
        )
        records.append(prefix_write_record)
        value_records.append((prefix_write_record, seed_value))

        snapshot_record = await context.broadcast_tx(
            label=f"parallel prefix-snapshot attempt {attempt}",
            wallet=writer_wallets[1],
            rpc_index=0,
            contract=contract_name,
            function="snapshot_sum",
            kwargs={"group": group, "tag": observation_tag},
            chi=PARALLEL_PROBE_TX_CHI,
            expected_success=True,
        )
        records.append(snapshot_record)

        for index in range(2, parallel_batch_size):
            value = 300 + index
            record = await context.broadcast_tx(
                label=f"parallel prefix-tail attempt {attempt} #{index}",
                wallet=writer_wallets[index],
                rpc_index=0,
                contract=contract_name,
                function="write_value",
                kwargs={
                    "group": group,
                    "key": f"tail-{index}",
                    "value": value,
                },
                chi=PARALLEL_PROBE_TX_CHI,
                expected_success=True,
            )
            records.append(record)
            value_records.append((record, value))
        await context.resolve_records(
            records,
            timeout_seconds=receipt_timeout_seconds,
            concurrent=receipt_resolution == "concurrent",
            max_workers=receipt_workers,
        )
        require_all_successful(
            f"parallel_probe prefix_scan attempt {attempt}",
            records,
        )

        observed = await context.get_state_from_healthy_node(
            founder,
            contract_name,
            "observations",
            observation_tag,
        )
        expected = sum(
            value for record, value in value_records if record_precedes(record, snapshot_record)
        )
        if observed != expected:
            raise WorkloadError(
                "parallel_probe: unexpected prefix-scan observation "
                f"{observed!r}, expected {expected!r}"
            )

        return {
            "attempt": attempt,
            "records": records,
            "tag": observation_tag,
            "group": group,
            "observed_observation": observed,
            "expected_observation": expected,
            "exercised_conflict_path": (
                record_precedes(prefix_write_record, snapshot_record)
                and prefix_write_record.height == snapshot_record.height
            ),
        }

    prefix_scan_attempts: list[dict[str, Any]] = []
    prefix_scan_result: dict[str, Any] | None = None
    for attempt in range(1, max_ordering_attempts + 1):
        attempt_result = await execute_prefix_scan_attempt(attempt)
        prefix_scan_attempts.append(
            {
                "attempt": attempt_result["attempt"],
                "tag": attempt_result["tag"],
                "observed_observation": attempt_result["observed_observation"],
                "expected_observation": attempt_result["expected_observation"],
                "exercised_conflict_path": attempt_result["exercised_conflict_path"],
                "records": summarize_records(attempt_result["records"]),
            }
        )
        if attempt_result["exercised_conflict_path"]:
            prefix_scan_result = attempt_result
            break
    if prefix_scan_result is None:
        raise WorkloadError(
            "parallel_probe: could not obtain canonical writer-before-reader "
            f"ordering for prefix_scan after {max_ordering_attempts} attempts"
        )

    prefix_scan_records = prefix_scan_result["records"]
    prefix_scan_tag = prefix_scan_result["tag"]
    prefix_scan_group = prefix_scan_result["group"]
    prefix_scan_observation = prefix_scan_result["observed_observation"]
    expected_prefix_observation = prefix_scan_result["expected_observation"]

    state = await context.compare_state(
        [
            {
                "label": "parallel flag observation",
                "contract": contract_name,
                "variable": "observations",
                "keys": [read_after_write_tag],
            },
            {
                "label": "parallel prefix observation",
                "contract": contract_name,
                "variable": "observations",
                "keys": [prefix_scan_tag],
            },
            {
                "label": "parallel final prefix seed",
                "contract": contract_name,
                "variable": "values",
                "keys": [prefix_scan_group, "seed"],
            },
        ]
    )

    overall_heights = [
        height
        for batch in (
            non_conflicting_records,
            same_sender_records,
            read_after_write_records,
            prefix_scan_records,
        )
        for height in (int(record.height) for record in batch if record.height is not None)
    ]

    return {
        "scenario": "parallel_probe",
        "contract_name": contract_name,
        "parallel_batch_size": parallel_batch_size,
        "parallel_config": {
            "enabled": bool(parallel_config.get("enabled")),
            "workers": int(parallel_config.get("workers", 0) or 0),
            "min_transactions": configured_min_transactions,
            "access_estimates_enabled": bool(parallel_config.get("access_estimates_enabled", True)),
        },
        "funding_transactions": len(funding_records),
        "batches": {
            "non_conflicting": summarize_records(non_conflicting_records),
            "same_sender": summarize_records(same_sender_records),
            "read_after_write": {
                **summarize_records(read_after_write_records),
                "tag": read_after_write_tag,
                "attempt_count": len(read_after_write_attempts),
                "attempts": read_after_write_attempts,
                "expected_observation": expected_read_after_write_observation,
                "observed_observation": read_after_write_observation,
            },
            "prefix_scan": {
                **summarize_records(prefix_scan_records),
                "tag": prefix_scan_tag,
                "group": prefix_scan_group,
                "attempt_count": len(prefix_scan_attempts),
                "attempts": prefix_scan_attempts,
                "expected_observation": expected_prefix_observation,
                "observed_observation": prefix_scan_observation,
            },
        },
        "overall_height_window": {
            "min_height": min(overall_heights) if overall_heights else None,
            "max_height": max(overall_heights) if overall_heights else None,
        },
        "state": state,
    }


async def run_transfer_fanout(
    context: WorkloadContext,
    *,
    seed: str,
    operations: int,
    wallet_count: int,
    submit_workers: int,
    receipt_resolution: str,
    receipt_workers: int,
    receipt_timeout_seconds: float,
    broadcast_mode: str,
) -> dict[str, Any]:
    sender_wallets = [
        derive_wallet(seed, f"transfer-fanout-sender-{index}") for index in range(wallet_count)
    ]
    recipient_wallets = [
        derive_wallet(seed, f"transfer-fanout-recipient-{index}") for index in range(wallet_count)
    ]
    counts = distribute_operation_counts(operations, wallet_count)
    funding_amount = fixed(max(5_000, max(counts) * 4 + 1_000))
    funding_records = await fund_wallets(
        context,
        seed_wallet=context.founder_wallet,
        worker_wallets=sender_wallets,
        amount=funding_amount,
        chi=THROUGHPUT_TRANSFER_TX_CHI,
        label_prefix="fund throughput sender",
        timeout_seconds=receipt_timeout_seconds,
    )

    plans: list[dict[str, Any]] = []
    for index, sender_wallet in enumerate(sender_wallets):
        count = counts[index]
        if count == 0:
            continue
        submission_index = context.submission_index(index)
        nonces = await context.reserve_nonces(
            sender_wallet,
            submission_index,
            count=count,
        )
        recipient = recipient_wallets[index].public_key
        for local_index, nonce in enumerate(nonces):
            plans.append(
                {
                    "label": f"fanout transfer #{len(plans)}",
                    "wallet": sender_wallet,
                    "rpc_index": index,
                    "contract": "currency",
                    "function": "transfer",
                    "kwargs": {"amount": fixed(1), "to": recipient},
                    "chi": THROUGHPUT_TRANSFER_TX_CHI,
                    "expected_success": True,
                    "mode": broadcast_mode,
                    "nonce": nonce,
                }
            )

    random.Random(f"{seed}:transfer-fanout").shuffle(plans)
    start_height = await latest_block_height(context)
    started_at = time.monotonic()
    records = await broadcast_plans(
        context,
        plans,
        submit_workers=submit_workers,
    )
    require_all_admitted("transfer_fanout", records)
    await context.resolve_records(
        records,
        timeout_seconds=receipt_timeout_seconds,
        concurrent=receipt_resolution == "concurrent",
        max_workers=receipt_workers,
    )
    require_all_successful("transfer_fanout", records)
    elapsed = time.monotonic() - started_at
    sample_indices = sorted(
        {
            0,
            len(recipient_wallets) // 2,
            len(recipient_wallets) - 1,
        }
    )
    verification_queries = [
        {
            "label": f"recipient balance {index}",
            "contract": "currency",
            "variable": "balances",
            "keys": [recipient_wallets[index].public_key],
        }
        for index in sample_indices
        if counts[index] > 0
    ]
    expected_balances = {
        f"recipient balance {index}": counts[index] for index in sample_indices if counts[index] > 0
    }
    await wait_for_query_predicate(
        context,
        verification_queries,
        timeout_seconds=receipt_timeout_seconds,
        predicate=lambda query, value: coerce_numeric(value) == expected_balances[query["label"]],
    )
    await wait_for_mempool_drain(
        context,
        timeout_seconds=receipt_timeout_seconds,
    )
    end_height = await latest_block_height(context)
    queries = [
        {
            "label": f"recipient balance {index}",
            "contract": "currency",
            "variable": "balances",
            "keys": [recipient_wallets[index].public_key],
        }
        for index in sample_indices
        if counts[index] > 0
    ]
    state = await context.compare_state(queries) if queries else {"ok": True}
    require_matching_state("transfer_fanout", state)
    for query_index, query in enumerate(state.get("queries", [])):
        index = int(str(query["label"]).rsplit(" ", 1)[-1])
        sample_value = compared_state_value(state, query_index, scenario="transfer_fanout")
        if coerce_numeric(sample_value) != counts[index]:
            raise WorkloadError(
                "transfer_fanout: recipient balance mismatch for "
                f"wallet {index}: expected {counts[index]}, got {sample_value}"
            )

    return {
        "scenario": "transfer_fanout",
        "wallet_count": wallet_count,
        "submit_workers": submit_workers,
        "broadcast_mode": broadcast_mode,
        "funding_amount": funding_amount,
        "funding_transactions": len(funding_records),
        "transaction_count": len(records),
        "successful_transactions": sum(1 for record in records if record.final_success),
        "failed_transactions": sum(1 for record in records if record.final_success is False),
        "elapsed_workload_seconds": round(elapsed, 3),
        "committed_window": await summarize_committed_window_for_range(
            context,
            start_height=start_height,
            end_height=end_height,
            workload_transactions=len(records),
            fallback_elapsed_seconds=elapsed,
        ),
        "state": state,
    }


async def run_contract_heavy(
    context: WorkloadContext,
    *,
    seed: str,
    operations: int,
    wallet_count: int,
    submit_workers: int,
    receipt_resolution: str,
    receipt_workers: int,
    receipt_timeout_seconds: float,
    broadcast_mode: str,
    rounds: int,
) -> dict[str, Any]:
    suffix = hashlib.sha256(f"{seed}:contract-heavy".encode("utf-8")).hexdigest()[:8]
    contract_name = f"con_hash_stress_{suffix}"
    contract_code = read_fixture("throughput/con_hash_stress.py")
    deploy_record = await context.broadcast_tx(
        label=f"deploy {contract_name}",
        wallet=context.founder_wallet,
        rpc_index=0,
        contract="submission",
        function="submit_contract",
        kwargs=context.contract_submission_kwargs(
            name=contract_name,
            code=contract_code,
        ),
        chi=THROUGHPUT_CONTRACT_DEPLOY_CHI,
        expected_success=True,
        mode="checktx",
    )
    require_all_admitted("contract_heavy deploy", [deploy_record])
    await context.resolve_records(
        [deploy_record],
        timeout_seconds=receipt_timeout_seconds,
        concurrent=False,
        max_workers=1,
    )
    require_successful(deploy_record)
    await wait_for_contract_visibility(
        context,
        contract_name,
        timeout_seconds=receipt_timeout_seconds,
    )

    worker_wallets = [
        derive_wallet(seed, f"contract-heavy-worker-{index}") for index in range(wallet_count)
    ]
    counts = distribute_operation_counts(operations, wallet_count)
    funding_amount = fixed(max(5_000, max(counts) * 2 + 1_000))
    funding_records = await fund_wallets(
        context,
        seed_wallet=context.founder_wallet,
        worker_wallets=worker_wallets,
        amount=funding_amount,
        chi=THROUGHPUT_TRANSFER_TX_CHI,
        label_prefix="fund heavy worker",
        timeout_seconds=receipt_timeout_seconds,
    )

    sample_slots: dict[int, str] = {}
    final_slots: dict[int, str] = {}
    plans: list[dict[str, Any]] = []
    for index, worker_wallet in enumerate(worker_wallets):
        count = counts[index]
        if count == 0:
            continue
        submission_index = context.submission_index(index)
        nonces = await context.reserve_nonces(
            worker_wallet,
            submission_index,
            count=count,
        )
        for local_index, nonce in enumerate(nonces):
            slot = f"{worker_wallet.public_key}-{local_index}"
            if index not in sample_slots:
                sample_slots[index] = slot
            final_slots[index] = slot
            plans.append(
                {
                    "label": f"contract heavy #{len(plans)}",
                    "wallet": worker_wallet,
                    "rpc_index": index,
                    "contract": contract_name,
                    "function": "crunch",
                    "kwargs": {
                        "slot": slot,
                        "payload": f"{seed}-{slot}",
                        "rounds": rounds,
                    },
                    "chi": THROUGHPUT_CONTRACT_TX_CHI,
                    "expected_success": True,
                    "mode": broadcast_mode,
                    "nonce": nonce,
                }
            )

    random.Random(f"{seed}:contract-heavy").shuffle(plans)
    start_height = await latest_block_height(context)
    started_at = time.monotonic()
    records = await broadcast_plans(
        context,
        plans,
        submit_workers=submit_workers,
    )
    require_all_admitted("contract_heavy", records)
    await context.resolve_records(
        records,
        timeout_seconds=receipt_timeout_seconds,
        concurrent=receipt_resolution == "concurrent",
        max_workers=receipt_workers,
    )
    require_all_successful("contract_heavy", records)
    elapsed = time.monotonic() - started_at
    sample_indices = sorted(
        {
            0,
            len(worker_wallets) // 2,
            len(worker_wallets) - 1,
        }
    )
    verification_queries = [
        {
            "label": f"contract final slot {index}",
            "contract": contract_name,
            "variable": "results",
            "keys": [final_slots[index]],
        }
        for index in sample_indices
        if index in final_slots
    ]
    await wait_for_query_predicate(
        context,
        verification_queries,
        timeout_seconds=receipt_timeout_seconds,
        predicate=lambda _query, value: isinstance(value, str) and bool(value),
    )
    await wait_for_mempool_drain(
        context,
        timeout_seconds=receipt_timeout_seconds,
    )
    end_height = await latest_block_height(context)
    queries = [
        {
            "label": f"contract slot {index}",
            "contract": contract_name,
            "variable": "results",
            "keys": [sample_slots[index]],
        }
        for index in sample_indices
        if index in sample_slots
    ]
    state = await context.compare_state(queries) if queries else {"ok": True}
    require_matching_state("contract_heavy", state)
    for query_index, query in enumerate(state.get("queries", [])):
        sample_value = compared_state_value(state, query_index, scenario="contract_heavy")
        if not isinstance(sample_value, str) or not sample_value:
            raise WorkloadError(
                "contract_heavy: expected a non-empty digest result for "
                f"{query['label']}, got {sample_value!r}"
            )

    return {
        "scenario": "contract_heavy",
        "contract_name": contract_name,
        "wallet_count": wallet_count,
        "submit_workers": submit_workers,
        "broadcast_mode": broadcast_mode,
        "rounds": rounds,
        "funding_amount": funding_amount,
        "deploy_transaction_hash": deploy_record.tx_hash,
        "funding_transactions": len(funding_records),
        "transaction_count": len(records),
        "successful_transactions": sum(1 for record in records if record.final_success),
        "failed_transactions": sum(1 for record in records if record.final_success is False),
        "elapsed_workload_seconds": round(elapsed, 3),
        "committed_window": await summarize_committed_window_for_range(
            context,
            start_height=start_height,
            end_height=end_height,
            workload_transactions=len(records),
            fallback_elapsed_seconds=elapsed,
        ),
        "state": state,
    }


async def run_scenario(
    args: argparse.Namespace,
    context: WorkloadContext,
) -> dict[str, Any]:
    if args.scenario == "counter_basic":
        return await run_counter_basic(
            context,
            seed=args.seed,
            operations=args.counter_ops,
            receipt_resolution=args.receipt_resolution,
            receipt_workers=args.receipt_workers,
            receipt_timeout_seconds=args.receipt_timeout_seconds,
        )
    if args.scenario == "dex_mixed":
        return await run_dex_mixed(
            context,
            seed=args.seed,
            rounds=args.dex_rounds,
            receipt_resolution=args.receipt_resolution,
            receipt_workers=args.receipt_workers,
            receipt_timeout_seconds=args.receipt_timeout_seconds,
        )
    if args.scenario == "parallel_probe":
        return await run_parallel_probe(
            context,
            seed=args.seed,
            receipt_resolution=args.receipt_resolution,
            receipt_workers=args.receipt_workers,
            receipt_timeout_seconds=args.receipt_timeout_seconds,
        )
    if args.scenario == "transfer_fanout":
        return await run_transfer_fanout(
            context,
            seed=args.seed,
            operations=args.throughput_ops,
            wallet_count=args.wallet_count,
            submit_workers=args.submit_workers,
            receipt_resolution=args.receipt_resolution,
            receipt_workers=args.receipt_workers,
            receipt_timeout_seconds=args.receipt_timeout_seconds,
            broadcast_mode=args.broadcast_mode,
        )
    if args.scenario == "contract_heavy":
        return await run_contract_heavy(
            context,
            seed=args.seed,
            operations=args.throughput_ops,
            wallet_count=args.wallet_count,
            submit_workers=args.submit_workers,
            receipt_resolution=args.receipt_resolution,
            receipt_workers=args.receipt_workers,
            receipt_timeout_seconds=args.receipt_timeout_seconds,
            broadcast_mode=args.broadcast_mode,
            rounds=args.heavy_rounds,
        )
    raise WorkloadError(f"unsupported scenario: {args.scenario}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic workload scenarios against the localnet",
    )
    parser.add_argument(
        "--scenario",
        choices=(
            "counter_basic",
            "dex_mixed",
            "parallel_probe",
            "transfer_fanout",
            "contract_heavy",
        ),
        default="counter_basic",
    )
    parser.add_argument(
        "--seed",
        default="xian-localnet-workload-v1",
        help="Deterministic seed used for generated wallets and tx plans",
    )
    parser.add_argument(
        "--counter-ops",
        type=int,
        default=180,
        help="Number of transactions to broadcast for counter_basic",
    )
    parser.add_argument(
        "--dex-rounds",
        type=int,
        default=6,
        help="Number of swap/failure rounds to run for dex_mixed",
    )
    parser.add_argument(
        "--throughput-ops",
        type=int,
        default=12000,
        help="Number of transactions to broadcast for throughput scenarios",
    )
    parser.add_argument(
        "--wallet-count",
        type=int,
        default=64,
        help="Number of sender wallets to use for throughput scenarios",
    )
    parser.add_argument(
        "--submit-workers",
        type=int,
        default=128,
        help="Maximum concurrent broadcast workers for throughput scenarios",
    )
    parser.add_argument(
        "--broadcast-mode",
        choices=("async", "checktx"),
        default="checktx",
        help=(
            "Broadcast mode to use for throughput scenarios "
            "(default: checktx; async is best-effort stress mode)"
        ),
    )
    parser.add_argument(
        "--receipt-timeout-seconds",
        type=float,
        default=RECEIPT_TIMEOUT_SECONDS,
        help="Receipt timeout to use when resolving workload transactions",
    )
    parser.add_argument(
        "--heavy-rounds",
        type=int,
        default=64,
        help="Digest loop count for contract_heavy throughput scenario",
    )
    parser.add_argument(
        "--state-sample-nodes",
        type=int,
        default=2,
        help="How many nodes to compare for state cross-checks",
    )
    parser.add_argument(
        "--app-hash-window",
        type=int,
        default=3,
        help="How many recent heights to compare for app_hash determinism",
    )
    parser.add_argument(
        "--submit-node-index",
        type=int,
        default=0,
        help="Preferred RPC node index for transaction submission",
    )
    parser.add_argument(
        "--round-robin-submission",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Submit transactions across RPC endpoints instead of pinning to one node",
    )
    parser.add_argument(
        "--receipt-resolution",
        choices=("serial", "concurrent"),
        default="serial",
        help="Resolve tx receipts one-by-one or concurrently after broadcast",
    )
    parser.add_argument(
        "--receipt-workers",
        type=int,
        default=16,
        help="Maximum concurrent receipt pollers when using concurrent resolution",
    )
    parser.add_argument(
        "--measure-memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Collect docker memory samples at the end of the workload",
    )
    parser.add_argument(
        "--json",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Emit the final summary as JSON",
    )
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    network = load_network()
    context = WorkloadContext(
        network,
        sample_nodes=args.state_sample_nodes,
        submit_node_index=args.submit_node_index,
        round_robin_submission=args.round_robin_submission,
    )

    async with context:
        print(f"Localnet workload scenario: {args.scenario}")
        print(f"Chain ID: {context.chain_id}")
        print(f"Sample nodes: {', '.join(node.moniker for node in context.sample_nodes)}")

        started_at = time.monotonic()
        scenario_summary = await run_scenario(args, context)
        consensus_summary = await compare_app_hash_window(
            context.session,
            context.nodes,
            window=args.app_hash_window,
        )
        state_summary = scenario_summary.get("state", {"ok": True})
        if not consensus_summary["ok"]:
            raise WorkloadError("app_hash mismatch detected across localnet nodes")
        if not state_summary["ok"]:
            raise WorkloadError("state mismatch detected across sampled localnet nodes")

        elapsed = time.monotonic() - started_at
        final_summary = {
            "ok": True,
            "chain_id": context.chain_id,
            "scenario": scenario_summary["scenario"],
            "elapsed_seconds": round(elapsed, 3),
            "seed": args.seed,
            "consensus": consensus_summary,
            "scenario_summary": normalize_value(scenario_summary),
            "memory": (collect_container_memory(context.nodes) if args.measure_memory else {}),
            "receipt_resolution": args.receipt_resolution,
        }

        if args.json:
            print(json.dumps(final_summary, indent=2, sort_keys=True))
        else:
            print(
                f"Scenario {scenario_summary['scenario']} completed in {elapsed:.2f}s; "
                f"consensus and sampled state matched."
            )
        return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
