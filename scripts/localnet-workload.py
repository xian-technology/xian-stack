#!/usr/bin/env python3
"""Run deterministic workload scenarios against a local Xian network."""

from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
import random
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from xian_runtime_types.decimal import ContractingDecimal

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent
WORKLOADS_DIR = STACK_DIR / "workloads"
NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"

from xian_py import transaction as tr  # noqa: E402
from xian_py.models import TransactionSubmission  # noqa: E402
from xian_py.wallet import Wallet  # noqa: E402
from xian_py.xian_async import XianAsync  # noqa: E402


COUNTER_DEPLOY_STAMPS = 75_000
COUNTER_TX_STAMPS = 1_500
TOKEN_DEPLOY_STAMPS = 150_000
PAIR_DEPLOY_STAMPS = 300_000
DEX_DEPLOY_STAMPS = 200_000
TOKEN_TX_STAMPS = 7_500
DEX_TX_STAMPS = 60_000
RECEIPT_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True)
class LocalnetNode:
    moniker: str
    rpc_url: str
    rpc_port: int
    p2p_port: int
    metrics_port: int
    abci_container: str
    cometbft_container: str


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
    tx_hash: str | None = None
    final_success: bool | None = None
    final_message: str | None = None
    height: int | None = None
    stamps_used: int | None = None
    events: list[dict[str, Any]] | None = None


class WorkloadError(RuntimeError):
    pass


class WorkloadContext:
    def __init__(
        self,
        network: dict,
        *,
        sample_nodes: int,
        submit_node_index: int,
        round_robin_submission: bool,
    ):
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
            )
            for node in network["nodes"]
        ]
        self.sample_nodes = self._select_sample_nodes(sample_nodes)
        self.submit_node_index = submit_node_index % len(self.nodes)
        self.round_robin_submission = round_robin_submission
        self._next_nonce: dict[str, int] = {}
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> WorkloadContext:
        connector = aiohttp.TCPConnector(limit=256, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=15, sock_connect=3, sock_read=10)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None

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

    async def next_nonce(self, wallet: Wallet, rpc_index: int) -> int:
        public_key = wallet.public_key
        if public_key not in self._next_nonce:
            self._next_nonce[public_key] = await tr.get_nonce_async(
                self.nodes[rpc_index % len(self.nodes)].rpc_url,
                public_key,
                session=self.session,
            )
        nonce = self._next_nonce[public_key]
        self._next_nonce[public_key] += 1
        return nonce

    async def broadcast_tx(
        self,
        *,
        label: str,
        wallet: Wallet,
        rpc_index: int,
        contract: str,
        function: str,
        kwargs: dict[str, Any],
        stamps: int,
        expected_success: bool,
        expected_message: str | None = None,
    ) -> BroadcastRecord:
        submission_index = self.submission_index(rpc_index)
        client = self.client(wallet, submission_index)
        response = await client.send_tx(
            contract=contract,
            function=function,
            kwargs=kwargs,
            stamps=stamps,
            nonce=await self.next_nonce(wallet, submission_index),
            chain_id=self.chain_id,
            mode="checktx",
            wait_for_tx=False,
        )
        return BroadcastRecord(
            label=label,
            contract=contract,
            function=function,
            rpc_url=self.nodes[submission_index].rpc_url,
            sender=wallet.public_key,
            expected_success=expected_success,
            expected_message=expected_message,
            response=response,
            tx_hash=response.tx_hash,
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
        if not record.response.submitted or record.response.accepted is False:
            record.final_success = False
            record.final_message = record.response.message
            self._assert_record(record)
            return

        if not record.tx_hash:
            raise WorkloadError(f"{record.label}: missing tx hash")

        client = XianAsync(
            node_url=record.rpc_url,
            chain_id=self.chain_id,
            wallet=self.founder_wallet,
            session=self.session,
        )
        receipt = await wait_for_tx_receipt(
            client=client,
            tx_hash=record.tx_hash,
            timeout_seconds=timeout_seconds,
        )
        record.height = receipt["height"]
        record.final_success = receipt["success"]
        record.final_message = receipt["result"]
        record.stamps_used = receipt["stamps_used"]
        record.events = receipt["events"]
        self._assert_record(record)

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

    async def compare_state(
        self,
        queries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        details = []
        all_match = True
        founder = self.founder_wallet

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

            values = dict(
                await asyncio.gather(
                    *(fetch_node_state(node) for node in self.sample_nodes)
                )
            )
            canonical = {
                moniker: canonical_json(value)
                for moniker, value in values.items()
            }
            unique = set(canonical.values())
            ok = len(unique) == 1
            all_match = all_match and ok
            details.append(
                {
                    "label": query["label"],
                    "contract": query["contract"],
                    "variable": query["variable"],
                    "keys": [str(key) for key in query.get("keys", [])],
                    "ok": ok,
                    "values": values,
                }
            )

        return {
            "ok": all_match,
            "sample_nodes": [node.moniker for node in self.sample_nodes],
            "queries": details,
        }


def canonical_json(value: Any) -> str:
    return json.dumps(normalize_value(value), sort_keys=True, separators=(",", ":"))


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
            raise WorkloadError(
                f"expected numeric workload value, got {value!r}"
            ) from exc
    raise WorkloadError(f"expected numeric workload value, got {value!r}")


def load_network() -> dict[str, Any]:
    if not NETWORK_PATH.exists():
        raise WorkloadError(
            f"localnet metadata not found at {NETWORK_PATH}; run localnet-init first"
        )
    return json.loads(NETWORK_PATH.read_text(encoding="utf-8"))


def read_fixture(path: str) -> str:
    return (WORKLOADS_DIR / path).read_text(encoding="utf-8")


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


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    async with session.get(url, timeout=timeout) as response:
        return await response.json()


async def wait_for_tx_receipt(
    *,
    client: XianAsync,
    tx_hash: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            receipt = await client.wait_for_tx(
                tx_hash,
                timeout_seconds=min(5.0, max(0.5, deadline - time.monotonic())),
                poll_interval_seconds=0.5,
            )
            result = receipt.raw.get("result")
            if not isinstance(result, dict):
                raise WorkloadError(f"tx {tx_hash} returned no result")
            if isinstance(receipt.execution, dict):
                return {
                    "height": int(result["height"]),
                    "success": receipt.success,
                    "result": receipt.message,
                    "events": receipt.execution.get("events", []),
                    "stamps_used": receipt.execution.get("stamps_used"),
                }
            return {
                "height": int(result["height"]),
                "success": receipt.success,
                "result": receipt.message,
                "events": [],
                "stamps_used": None,
            }
        except Exception as exc:  # noqa: PERF203
            last_error = exc
            await asyncio.sleep(0.5)

    raise WorkloadError(f"timed out waiting for tx {tx_hash}") from last_error


async def compare_app_hash_window(
    session: aiohttp.ClientSession,
    nodes: list[LocalnetNode],
    *,
    window: int,
) -> dict[str, Any]:
    heights = {}
    for node in nodes:
        payload = await fetch_json(
            session,
            f"{node.rpc_url}/status",
            timeout=5.0,
        )
        heights[node.moniker] = int(
            payload["result"]["sync_info"]["latest_block_height"]
        )

    min_height = min(heights.values())
    start_height = max(1, min_height - max(1, window) + 1)
    checks = []
    all_match = True

    for height in range(start_height, min_height + 1):
        hashes = {}
        for node in nodes:
            payload = await fetch_json(
                session,
                f"{node.rpc_url}/block?height={height}",
                timeout=5.0,
            )
            hashes[node.moniker] = payload["result"]["block"]["header"]["app_hash"]
        unique = set(hashes.values())
        ok = len(unique) == 1
        all_match = all_match and ok
        checks.append({"height": height, "ok": ok, "app_hashes": hashes})

    return {"ok": all_match, "heights": heights, "checks": checks}


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


async def broadcast_and_confirm(
    context: WorkloadContext,
    **kwargs: Any,
) -> BroadcastRecord:
    record = await context.broadcast_tx(**kwargs)
    await context.resolve_records([record])
    require_successful(record)
    return record


async def broadcast_funding_record(
    context: WorkloadContext,
    *,
    founder: Wallet,
    wallet: Wallet,
    index: int,
    gas_amount: float,
    token_amount: float,
    token_a: str,
    token_b: str,
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
            stamps=TOKEN_TX_STAMPS,
            expected_success=True,
        ),
        await broadcast_and_confirm(
            context,
            label=f"fund tokenA trader-{index}",
            wallet=founder,
            rpc_index=rpc_index,
            contract=token_a,
            function="transfer",
            kwargs={"amount": token_amount, "to": wallet.public_key},
            stamps=TOKEN_TX_STAMPS,
            expected_success=True,
        ),
        await broadcast_and_confirm(
            context,
            label=f"fund tokenB trader-{index}",
            wallet=founder,
            rpc_index=rpc_index,
            contract=token_b,
            function="transfer",
            kwargs={"amount": token_amount, "to": wallet.public_key},
            stamps=TOKEN_TX_STAMPS,
            expected_success=True,
        ),
    ]


async def broadcast_approval_record(
    context: WorkloadContext,
    *,
    wallet: Wallet,
    wallet_index: int,
    dex_contract: str,
    token_name: str,
) -> BroadcastRecord:
    return await broadcast_and_confirm(
        context,
        label=f"approve {token_name} wallet-{wallet_index}",
        wallet=wallet,
        rpc_index=wallet_index % len(context.nodes),
        contract=token_name,
        function="approve",
        kwargs={"amount": 1_000_000.0, "to": dex_contract},
        stamps=TOKEN_TX_STAMPS,
        expected_success=True,
    )


async def run_counter_basic(
    context: WorkloadContext,
    *,
    seed: str,
    operations: int,
    receipt_resolution: str,
    receipt_workers: int,
) -> dict[str, Any]:
    founder = context.founder_wallet
    worker_wallets = [derive_wallet(seed, f"counter-worker-{index}") for index in range(len(context.nodes))]
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
        kwargs={"name": contract_name, "code": contract_code},
        stamps=COUNTER_DEPLOY_STAMPS,
        expected_success=True,
    )
    await context.resolve_records([deploy_record])
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
                kwargs={"amount": 5_000.0, "to": wallet.public_key},
                stamps=COUNTER_TX_STAMPS,
                expected_success=True,
            )
        )

    print(f"Broadcasting {operations} counter_basic operations...")
    records: list[BroadcastRecord] = []
    expected_counter = 0
    for index in range(operations):
        rpc_index = index % len(context.nodes)
        sender_wallet = worker_wallets[rpc_index]
        if index % 3 == 0:
            recipient = worker_wallets[(rpc_index + 1) % len(worker_wallets)].public_key
            records.append(
                await context.broadcast_tx(
                    label=f"transfer #{index}",
                    wallet=sender_wallet,
                    rpc_index=rpc_index,
                    contract="currency",
                    function="transfer",
                    kwargs={"amount": 1.0, "to": recipient},
                    stamps=COUNTER_TX_STAMPS,
                    expected_success=True,
                )
            )
        elif index % 3 == 1:
            expected_counter += 1
            records.append(
                await context.broadcast_tx(
                    label=f"increment #{index}",
                    wallet=sender_wallet,
                    rpc_index=rpc_index,
                    contract=contract_name,
                    function="increment",
                    kwargs={},
                    stamps=COUNTER_TX_STAMPS,
                    expected_success=True,
                )
            )
        else:
            amount = float((index % 11) + 1)
            expected_counter += int(amount)
            records.append(
                await context.broadcast_tx(
                    label=f"add #{index}",
                    wallet=sender_wallet,
                    rpc_index=rpc_index,
                    contract=contract_name,
                    function="add",
                    kwargs={"amount": amount},
                    stamps=COUNTER_TX_STAMPS,
                    expected_success=True,
                )
            )

    await context.resolve_records(
        records,
        concurrent=receipt_resolution == "concurrent",
        max_workers=receipt_workers,
    )
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
    counter_value = state["queries"][0]["values"][context.sample_nodes[0].moniker]
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
        raise WorkloadError(
            "DEX workload template no longer contains the expected pair constant"
        )
    return source.replace(needle, replacement, 1)


async def run_dex_mixed(
    context: WorkloadContext,
    *,
    seed: str,
    rounds: int,
    receipt_resolution: str,
    receipt_workers: int,
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
            kwargs={
                "name": token_a,
                "code": token_code,
                "constructor_args": {
                    "owner": founder.public_key,
                    "supply": 5_000_000.0,
                    "name": "Workload Token A",
                    "symbol": "WTA",
                },
            },
            stamps=TOKEN_DEPLOY_STAMPS,
            expected_success=True,
        ),
        await broadcast_and_confirm(
            context,
            label=f"deploy {token_b}",
            wallet=founder,
            rpc_index=1,
            contract="submission",
            function="submit_contract",
            kwargs={
                "name": token_b,
                "code": token_code,
                "constructor_args": {
                    "owner": founder.public_key,
                    "supply": 5_000_000.0,
                    "name": "Workload Token B",
                    "symbol": "WTB",
                },
            },
            stamps=TOKEN_DEPLOY_STAMPS,
            expected_success=True,
        ),
        await broadcast_and_confirm(
            context,
            label=f"deploy {pairs_contract}",
            wallet=founder,
            rpc_index=2,
            contract="submission",
            function="submit_contract",
            kwargs={"name": pairs_contract, "code": pairs_code},
            stamps=PAIR_DEPLOY_STAMPS,
            expected_success=True,
        ),
        await broadcast_and_confirm(
            context,
            label=f"deploy {dex_contract}",
            wallet=founder,
            rpc_index=3,
            contract="submission",
            function="submit_contract",
            kwargs={"name": dex_contract, "code": dex_code},
            stamps=DEX_DEPLOY_STAMPS,
            expected_success=True,
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
                gas_amount=50_000.0,
                token_amount=25_000.0,
                token_a=token_a,
                token_b=token_b,
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
            )
        )
        approval_records.append(
            await broadcast_approval_record(
                context,
                wallet=wallet,
                wallet_index=wallet_index + len(context.nodes),
                dex_contract=dex_contract,
                token_name=token_b,
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
            "amountADesired": 250_000.0,
            "amountBDesired": 250_000.0,
            "amountAMin": 240_000.0,
            "amountBMin": 240_000.0,
            "to": founder.public_key,
            "deadline": deadline_value(seconds_from_now=300),
        },
        stamps=DEX_TX_STAMPS,
        expected_success=True,
    )
    await context.resolve_records([initial_liquidity])
    require_successful(initial_liquidity)

    token0, token1 = sorted((token_a, token_b))
    pair_id = await context.client(founder, 0).get_state(
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
        kwargs={"pair": pair_id, "amount": 10_000.0, "to": dex_contract},
        stamps=DEX_TX_STAMPS,
        expected_success=True,
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
        trade_a_amount = float(10 + round_index)
        trade_b_amount = float(7 + round_index)
        impossible_min = float(10_000_000 + round_index)

        round_records = [
            await context.broadcast_tx(
                label=f"swap tokenA->tokenB round-{round_index}",
                wallet=trader_a,
                rpc_index=rpc_index,
                contract=dex_contract,
                function="swapExactTokenForToken",
                kwargs={
                    "amountIn": trade_a_amount,
                    "amountOutMin": 0.0001,
                    "pair": pair_id,
                    "src": token_a,
                    "to": trader_a.public_key,
                    "deadline": deadline_value(seconds_from_now=300),
                },
                stamps=DEX_TX_STAMPS,
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
                    "amountOutMin": 0.0001,
                    "pair": pair_id,
                    "src": token_b,
                    "to": trader_b.public_key,
                    "deadline": deadline_value(seconds_from_now=300),
                },
                stamps=DEX_TX_STAMPS,
                expected_success=True,
            ),
            await context.broadcast_tx(
                label=f"expired swap round-{round_index}",
                wallet=trader_c,
                rpc_index=(rpc_index + 2) % len(context.nodes),
                contract=dex_contract,
                function="swapExactTokenForToken",
                kwargs={
                    "amountIn": 5.0,
                    "amountOutMin": 0.0001,
                    "pair": pair_id,
                    "src": token_a,
                    "to": trader_c.public_key,
                    "deadline": deadline_value(seconds_from_now=-5),
                },
                stamps=DEX_TX_STAMPS,
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
                    "amountIn": 5.0,
                    "amountOutMin": impossible_min,
                    "pair": pair_id,
                    "src": token_b,
                    "to": founder.public_key,
                    "deadline": deadline_value(seconds_from_now=300),
                },
                stamps=DEX_TX_STAMPS,
                expected_success=False,
                expected_message="INSUFFICIENT_OUTPUT_AMOUNT",
            ),
        ]
        await context.resolve_records(
            round_records,
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
                "amountIn": 9.0,
                "amountOutMin": 0.0001,
                "pair": pair_id,
                "src": token_a,
                "to": unapproved_trader.public_key,
                "deadline": deadline_value(seconds_from_now=300),
            },
            stamps=DEX_TX_STAMPS,
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
                "amountIn": 4.0,
                "amountOutMin": 0.0001,
                "pair": int(pair_id) + 999,
                "src": token_a,
                "to": approved_traders[0].public_key,
                "deadline": deadline_value(seconds_from_now=300),
            },
            stamps=DEX_TX_STAMPS,
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
                "liquidity": 1000.0,
                "amountAMin": 0.0001,
                "amountBMin": 0.0001,
                "to": founder.public_key,
                "deadline": deadline_value(seconds_from_now=300),
            },
            stamps=DEX_TX_STAMPS,
            expected_success=True,
        )
    )

    await context.resolve_records(plan_records[-3:])

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

    first_node = context.sample_nodes[0].moniker
    reserve0 = coerce_numeric(state["queries"][2]["values"][first_node])
    reserve1 = coerce_numeric(state["queries"][3]["values"][first_node])
    total_supply = coerce_numeric(state["queries"][4]["values"][first_node])
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
        "expected_failures": sum(
            1 for record in plan_records if not record.expected_success
        ),
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
        )
    if args.scenario == "dex_mixed":
        return await run_dex_mixed(
            context,
            seed=args.seed,
            rounds=args.dex_rounds,
            receipt_resolution=args.receipt_resolution,
            receipt_workers=args.receipt_workers,
        )
    raise WorkloadError(f"unsupported scenario: {args.scenario}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic workload scenarios against the localnet",
    )
    parser.add_argument(
        "--scenario",
        choices=("counter_basic", "dex_mixed"),
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
        print(
            f"Sample nodes: {', '.join(node.moniker for node in context.sample_nodes)}"
        )

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
            "scenario_summary": scenario_summary,
            "memory": (
                collect_container_memory(context.nodes)
                if args.measure_memory
                else {}
            ),
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
