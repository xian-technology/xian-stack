#!/usr/bin/env python3
"""Run a layered 4-node localnet end-to-end program against real services."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent
ROOT_DIR = STACK_DIR.parent
NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"
WORKLOADS_DIR = STACK_DIR / "workloads"
OUTPUT_ROOT = STACK_DIR / ".localnet" / "e2e"
CONTRACTS_DIR = ROOT_DIR / "xian-contracts" / "contracts"
XIAN_ZK_PYTHON_DIR = (
    ROOT_DIR / "xian-contracting" / "packages" / "xian-zk" / "python"
)
XIAN_ABCI_SRC = ROOT_DIR / "xian-abci" / "src"
RUST_TRACER_MODE = "native_instruction_v1"
DEFAULT_TX_STAMPS = 15_000
DEFAULT_TRANSFER_STAMPS = 2_000
GOVERNANCE_TX_STAMPS = 50_000
STATE_PATCH_DELAY_BLOCKS = 8
SIMULATOR_BURST_REQUESTS = 128
WEBSOCKET_TIMEOUT_SECONDS = 20.0

sys.path.append(str(XIAN_ZK_PYTHON_DIR))
sys.path.append(str(XIAN_ABCI_SRC))

from xian_py.wallet import Wallet  # noqa: E402
from xian_py.xian_async import XianAsync  # noqa: E402

try:  # noqa: SIM105
    from xian_zk import (  # noqa: E402
        ShieldedDepositRequest,
        ShieldedKeyBundle,
        ShieldedNote,
        ShieldedNoteProver,
        ShieldedOutput,
        ShieldedTransferRequest,
        ShieldedWithdrawRequest,
        recover_encrypted_notes,
        scan_notes,
        tree_state,
    )
except Exception as exc:  # noqa: BLE001
    ShieldedNoteProver = None
    XIAN_ZK_IMPORT_ERROR = str(exc)
else:
    XIAN_ZK_IMPORT_ERROR = None


@dataclass(frozen=True)
class LocalnetNode:
    index: int
    moniker: str
    rpc_url: str
    rpc_port: int
    p2p_port: int
    metrics_port: int
    abci_container: str
    cometbft_container: str
    account_public_key: str
    account_private_key: str
    service_node: bool


@dataclass
class PhaseResult:
    name: str
    ok: bool
    started_at: str
    ended_at: str
    details: dict[str, Any]


class E2EError(RuntimeError):
    pass


def normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): normalize_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    return value


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_network() -> dict[str, Any]:
    if not NETWORK_PATH.exists():
        raise E2EError(
            f"localnet metadata not found at {NETWORK_PATH}; bootstrap first"
        )
    return json.loads(NETWORK_PATH.read_text(encoding="utf-8"))


def derive_wallet(seed: str, label: str) -> Wallet:
    digest = hashlib.sha256(f"{seed}:{label}".encode("utf-8")).hexdigest()
    return Wallet(private_key=digest)


def make_localnet_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    env["XIAN_LOCALNET_TRACER_MODE"] = RUST_TRACER_MODE
    env["XIAN_LOCALNET_ENABLE_BDS"] = "1"
    env["XIAN_LOCALNET_BDS_NODE_INDEX"] = str(args.bds_node_index)
    env["XIAN_LOCALNET_APP_LOG_LEVEL"] = args.log_level
    env["XIAN_LOCALNET_APP_LOG_JSON"] = "0"
    env["XIAN_LOCALNET_TRANSACTION_TRACE_LOGGING"] = "0"
    env["LOCALNET_NODES"] = str(args.nodes)
    env["XIAN_LOCALNET_TOPOLOGY"] = args.topology
    return env


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path = STACK_DIR,
    env: dict[str, str] | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=capture_output,
        text=True,
    )


def run_make(target: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run_cmd(["make", target], env=env)


async def fetch_abci_query(
    session: aiohttp.ClientSession,
    rpc_url: str,
    path: str,
    *,
    timeout: float = 10.0,
) -> Any:
    encoded_path = json.dumps(path)
    async with session.get(
        f"{rpc_url}/abci_query",
        params={"path": encoded_path},
        timeout=timeout,
    ) as response:
        payload = await response.json()
    abci_response = payload.get("result", {}).get("response", {})
    if int(abci_response.get("code", 0) or 0) != 0:
        raise E2EError(
            f"ABCI query failed for {path}: {abci_response.get('log')}"
        )
    encoded_value = abci_response.get("value")
    if not encoded_value:
        return None
    decoded = base64.b64decode(encoded_value).decode("utf-8")
    try:
        return json.loads(decoded)
    except json.JSONDecodeError:
        return decoded


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def build_nodes(network: dict[str, Any]) -> list[LocalnetNode]:
    nodes: list[LocalnetNode] = []
    for index, node in enumerate(network["nodes"]):
        nodes.append(
            LocalnetNode(
                index=index,
                moniker=node["moniker"],
                rpc_url=f"http://127.0.0.1:{node['host_rpc_port']}",
                rpc_port=int(node["host_rpc_port"]),
                p2p_port=int(node["host_p2p_port"]),
                metrics_port=int(node["host_metrics_port"]),
                abci_container=node["abci_container"],
                cometbft_container=node["cometbft_container"],
                account_public_key=node["account_public_key"],
                account_private_key=node["account_private_key"],
                service_node=bool(node.get("service_node")),
            )
        )
    return nodes


async def wait_for_localnet_ready(
    session: aiohttp.ClientSession,
    nodes: list[LocalnetNode],
    *,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        statuses = []
        for node in nodes:
            try:
                payload = await fetch_json(session, f"{node.rpc_url}/status", timeout=2.0)
                statuses.append(payload)
            except Exception:  # noqa: BLE001
                break
        if len(statuses) == len(nodes):
            heights = [
                int(payload["result"]["sync_info"]["latest_block_height"])
                for payload in statuses
            ]
            if all(height > 0 for height in heights):
                return statuses
        await asyncio.sleep(1.0)
    raise E2EError("localnet nodes did not become ready in time")


async def wait_for_height(
    session: aiohttp.ClientSession,
    rpc_url: str,
    target_height: int,
    *,
    timeout_seconds: float,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = await fetch_json(session, f"{rpc_url}/status", timeout=5.0)
        height = int(payload["result"]["sync_info"]["latest_block_height"])
        if height >= target_height:
            return height
        await asyncio.sleep(0.5)
    raise E2EError(f"node {rpc_url} did not reach height {target_height}")


async def latest_height(
    session: aiohttp.ClientSession,
    rpc_url: str,
) -> int:
    payload = await fetch_json(session, f"{rpc_url}/status", timeout=5.0)
    return int(payload["result"]["sync_info"]["latest_block_height"])


async def wait_for_bds_indexed(
    client: XianAsync,
    *,
    target_height: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        status = await client.get_bds_status()
        last_status = normalize_value(status.raw)
        indexed_height = status.indexed_height
        if indexed_height is not None and indexed_height >= target_height:
            return last_status
        await asyncio.sleep(0.5)
    raise E2EError(
        f"BDS did not reach indexed height {target_height}; last={last_status}"
    )


async def compare_app_hash_window(
    session: aiohttp.ClientSession,
    nodes: list[LocalnetNode],
    *,
    window: int,
) -> dict[str, Any]:
    heights: dict[str, int] = {}
    for node in nodes:
        payload = await fetch_json(session, f"{node.rpc_url}/status", timeout=5.0)
        heights[node.moniker] = int(payload["result"]["sync_info"]["latest_block_height"])

    min_height = min(heights.values())
    start_height = max(1, min_height - max(window, 1) + 1)
    checks: list[dict[str, Any]] = []
    overall_ok = True

    for height in range(start_height, min_height + 1):
        app_hashes: dict[str, str] = {}
        for node in nodes:
            payload = await fetch_json(
                session,
                f"{node.rpc_url}/block",
                timeout=5.0,
                params={"height": str(height)},
            )
            app_hashes[node.moniker] = payload["result"]["block"]["header"]["app_hash"]
        ok = len(set(app_hashes.values())) == 1
        overall_ok = overall_ok and ok
        checks.append({"height": height, "ok": ok, "app_hashes": app_hashes})

    return {"ok": overall_ok, "heights": heights, "checks": checks}


async def query_state_from_all_nodes(
    session: aiohttp.ClientSession,
    nodes: list[LocalnetNode],
    *,
    contract: str,
    variable: str,
    keys: list[str] | None = None,
) -> dict[str, Any]:
    path = f"/get/{contract}.{variable}"
    if keys:
        path = f"{path}:{':'.join(keys)}"
    results: dict[str, Any] = {}
    for node in nodes:
        results[node.moniker] = await fetch_abci_query(session, node.rpc_url, path)
    return results


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: float = 10.0,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    async with session.get(url, params=params, timeout=timeout) as response:
        return await response.json()


def ensure_positive_submission(
    submission,
    *,
    label: str,
) -> dict[str, Any]:
    if not submission.submitted:
        raise E2EError(f"{label}: transaction was not submitted")
    if submission.accepted is False:
        raise E2EError(f"{label}: CheckTx rejected: {submission.message}")
    if submission.receipt is None:
        raise E2EError(f"{label}: receipt missing")
    return normalize_receipt(submission, label=label)


def normalize_receipt(submission, *, label: str) -> dict[str, Any]:
    execution = submission.receipt.execution if submission.receipt else None
    state = []
    events = []
    if isinstance(execution, dict):
        state = execution.get("state", []) or []
        events = execution.get("events", []) or []
    return {
        "label": label,
        "submitted": submission.submitted,
        "accepted": submission.accepted,
        "finalized": submission.finalized,
        "success": None if submission.receipt is None else submission.receipt.success,
        "message": submission.message
        if submission.receipt is None
        else submission.receipt.message,
        "tx_hash": submission.tx_hash,
        "nonce": submission.nonce,
        "stamps_supplied": submission.stamps_supplied,
        "stamps_used": None if execution is None else execution.get("stamps_used"),
        "state_write_count": len(state),
        "event_count": len(events),
        "events": events,
    }


def json_file_name(phase_name: str) -> str:
    return phase_name.replace(" ", "_").replace("/", "_") + ".json"


def local_log_paths(node: LocalnetNode) -> list[Path]:
    log_dir = STACK_DIR / ".localnet" / node.moniker / ".cometbft" / "xian" / "logs"
    if not log_dir.exists():
        return []
    return sorted(log_dir.glob("xian-abci-*.log"), key=lambda path: path.stat().st_mtime)


def update_logging_config(
    *,
    level: str,
    trace_logging: bool,
    json_logging: bool,
) -> None:
    for config_path in sorted(
        (STACK_DIR / ".localnet").glob("node-*/.cometbft/config/config.toml")
    ):
        text = config_path.read_text(encoding="utf-8")
        replacements = {
            "transaction_trace_logging": "true" if trace_logging else "false",
            "app_log_level": f'"{level}"',
            "app_log_json": "true" if json_logging else "false",
        }
        for key, value in replacements.items():
            text, count = re.subn(
                rf"^{re.escape(key)} = .*$",
                f"{key} = {value}",
                text,
                flags=re.MULTILINE,
            )
            if count != 1:
                raise E2EError(f"could not rewrite {key} in {config_path}")
        config_path.write_text(text, encoding="utf-8")


def compute_patch_bundle_hash(payload: dict[str, Any]) -> str:
    canonical_changes = sorted(
        (
            {
                "comment": change.get("comment", ""),
                "key": change["key"],
                "value": change["value"],
            }
            for change in payload["changes"]
        ),
        key=lambda item: item["key"],
    )
    canonical_payload = {
        "activation_height": payload["activation_height"],
        "chain_id": payload.get("chain_id"),
        "changes": canonical_changes,
        "governance_contract": payload["governance_contract"],
        "patch_id": payload["patch_id"],
        "summary": payload.get("summary", ""),
        "uri": payload.get("uri", ""),
        "version": payload["version"],
    }
    serialized = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class E2ERunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.output_dir = OUTPUT_ROOT / self.run_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.network: dict[str, Any] | None = None
        self.nodes: list[LocalnetNode] = []
        self.founder_wallet: Wallet | None = None
        self.validator_wallets: list[Wallet] = []
        self.phase_results: list[PhaseResult] = []
        self.seed = args.seed
        self.contracts: dict[str, str] = {}
        self.service_node: LocalnetNode | None = None
        self.sample_tx_hash: str | None = None
        self.sample_event_tx_hash: str | None = None

    def write_phase(self, phase: PhaseResult) -> None:
        self.phase_results.append(phase)
        payload = {
            "name": phase.name,
            "ok": phase.ok,
            "started_at": phase.started_at,
            "ended_at": phase.ended_at,
            "details": normalize_value(phase.details),
        }
        (self.output_dir / json_file_name(phase.name)).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    async def run_phase(self, name: str, fn) -> dict[str, Any]:
        started = datetime.now(UTC).isoformat()
        details = await fn()
        ended = datetime.now(UTC).isoformat()
        phase = PhaseResult(
            name=name,
            ok=True,
            started_at=started,
            ended_at=ended,
            details=details,
        )
        self.write_phase(phase)
        return details

    async def bootstrap(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        env = make_localnet_env(self.args)
        outputs: dict[str, Any] = {
            "env": {
                "XIAN_LOCALNET_TRACER_MODE": env["XIAN_LOCALNET_TRACER_MODE"],
                "XIAN_LOCALNET_ENABLE_BDS": env["XIAN_LOCALNET_ENABLE_BDS"],
                "XIAN_LOCALNET_BDS_NODE_INDEX": env["XIAN_LOCALNET_BDS_NODE_INDEX"],
                "XIAN_LOCALNET_APP_LOG_LEVEL": env["XIAN_LOCALNET_APP_LOG_LEVEL"],
                "XIAN_LOCALNET_TOPOLOGY": env["XIAN_LOCALNET_TOPOLOGY"],
            }
        }
        if self.args.bootstrap:
            outputs["localnet_init"] = run_make("localnet-init", env=env).stdout
            if self.args.build:
                outputs["localnet_build"] = run_make("localnet-build", env=env).stdout
            outputs["localnet_up"] = run_make("localnet-up", env=env).stdout

        self.network = load_network()
        self.nodes = build_nodes(self.network)
        self.service_node = next(
            (node for node in self.nodes if node.service_node),
            self.nodes[self.args.bds_node_index],
        )
        self.founder_wallet = Wallet(private_key=self.network["founder_key"])
        self.validator_wallets = [
            Wallet(private_key=node.account_private_key) for node in self.nodes
        ]
        await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=self.args.rpc_timeout_seconds,
        )
        outputs["network"] = {
            "chain_id": self.network["chain_id"],
            "node_count": len(self.nodes),
            "tracer_mode": self.network["tracer_mode"],
            "bds": self.network.get("bds", {}),
        }
        (self.output_dir / "network.json").write_text(
            json.dumps(self.network, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return outputs

    async def health_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        statuses = []
        for node in self.nodes:
            status = await fetch_json(session, f"{node.rpc_url}/status", timeout=5.0)
            net_info = await fetch_json(session, f"{node.rpc_url}/net_info", timeout=5.0)
            validators = await fetch_json(
                session,
                f"{node.rpc_url}/validators",
                timeout=5.0,
            )
            statuses.append(
                {
                    "moniker": node.moniker,
                    "height": int(status["result"]["sync_info"]["latest_block_height"]),
                    "app_hash": status["result"]["sync_info"]["latest_app_hash"],
                    "peer_count": int(net_info["result"]["n_peers"]),
                    "validator_count": len(validators["result"]["validators"]),
                }
            )

        consensus = await compare_app_hash_window(
            session,
            self.nodes,
            window=self.args.app_hash_window,
        )
        service_bds = None
        if self.service_node is not None:
            async with self.client(self.founder_wallet, self.service_node.index, session) as client:
                try:
                    service_bds = normalize_value((await client.get_bds_status()).raw)
                except Exception as exc:  # noqa: BLE001
                    service_bds = {"error": str(exc)}

        return {
            "nodes": statuses,
            "consensus": consensus,
            "service_bds": service_bds,
        }

    def client(
        self,
        wallet: Wallet | None,
        node_index: int,
        session: aiohttp.ClientSession,
    ) -> XianAsync:
        if wallet is None:
            raise E2EError("wallet is required")
        return XianAsync(
            node_url=self.nodes[node_index].rpc_url,
            chain_id=self.network["chain_id"],
            wallet=wallet,
            session=session,
        )

    async def fund_wallets(
        self,
        session: aiohttp.ClientSession,
        wallets: list[Wallet],
        *,
        amount: int,
    ) -> list[dict[str, Any]]:
        receipts = []
        founder = self.founder_wallet
        async with self.client(founder, 0, session) as client:
            for wallet in wallets:
                submission = await client.send(
                    amount=amount,
                    to_address=wallet.public_key,
                    stamps=DEFAULT_TRANSFER_STAMPS,
                    wait_for_tx=True,
                )
                receipts.append(
                    ensure_positive_submission(
                        submission,
                        label=f"fund {wallet.public_key[:12]}",
                    )
                )
        return receipts

    async def xian_py_smoke(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        founder = self.founder_wallet
        wallets = [derive_wallet(self.seed, f"e2e-wallet-{index}") for index in range(4)]
        funding = await self.fund_wallets(session, wallets, amount=5_000)

        conflict_contract = f"con_e2e_conflict_{short_hash(self.run_id)}"
        patch_contract = f"con_e2e_patch_{short_hash(self.run_id + 'patch')}"
        self.contracts["conflict"] = conflict_contract
        self.contracts["patch_target"] = patch_contract

        async with self.client(founder, 0, session) as client:
            conflict_submission = await client.submit_contract(
                name=conflict_contract,
                code=read_text(WORKLOADS_DIR / "e2e" / "conflict_guard.py"),
                stamps=120_000,
                wait_for_tx=True,
            )
            patch_submission = await client.submit_contract(
                name=patch_contract,
                code=read_text(WORKLOADS_DIR / "e2e" / "patch_target.py"),
                stamps=90_000,
                wait_for_tx=True,
            )
            conflict_receipt = ensure_positive_submission(
                conflict_submission,
                label=f"deploy {conflict_contract}",
            )
            patch_receipt = ensure_positive_submission(
                patch_submission,
                label=f"deploy {patch_contract}",
            )
            self.sample_tx_hash = conflict_receipt["tx_hash"]

            balance = await client.get_balance(founder.public_key)
            simulated = await client.simulate("currency", "balance_of", {"address": founder.public_key})
            counter_state = await client.get_state(conflict_contract, "counter")
            patch_status = await client.call(patch_contract, "get_status", {})

        return {
            "wallets": [wallet.public_key for wallet in wallets],
            "funding": funding,
            "deployments": [conflict_receipt, patch_receipt],
            "founder_balance": normalize_value(balance),
            "simulate_balance": normalize_value(simulated),
            "conflict_counter_state": counter_state,
            "patch_status": patch_status,
        }

    async def periodic_load(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        wallets = [derive_wallet(self.seed, f"periodic-wallet-{index}") for index in range(4)]
        await self.fund_wallets(session, wallets, amount=3_000)
        streams = []
        tx_records = []

        async def send_periodic(
            wallet: Wallet,
            node_index: int,
            recipient: str,
            stream_index: int,
        ) -> None:
            async with self.client(wallet, node_index, session) as client:
                for step in range(self.args.periodic_rounds):
                    submission = await client.send(
                        amount=1,
                        to_address=recipient,
                        stamps=DEFAULT_TRANSFER_STAMPS,
                        wait_for_tx=True,
                    )
                    tx_records.append(
                        ensure_positive_submission(
                            submission,
                            label=f"periodic-{stream_index}-{step}",
                        )
                    )
                    await asyncio.sleep(self.args.periodic_interval_seconds)

        started = time.monotonic()
        for index, wallet in enumerate(wallets):
            recipient = wallets[(index + 1) % len(wallets)].public_key
            streams.append(send_periodic(wallet, index % len(self.nodes), recipient, index))
        await asyncio.gather(*streams)
        elapsed = time.monotonic() - started
        return {
            "transaction_count": len(tx_records),
            "elapsed_seconds": round(elapsed, 3),
            "approx_tps": round(len(tx_records) / elapsed, 3),
            "records": tx_records[-8:],
        }

    async def run_localnet_workload(
        self,
        *,
        scenario: str,
        counter_ops: int | None = None,
        dex_rounds: int | None = None,
    ) -> dict[str, Any]:
        cmd = [
            "uv",
            "run",
            "--project",
            str(ROOT_DIR / "xian-py"),
            "python3",
            str(SCRIPT_DIR / "localnet-workload.py"),
            "--scenario",
            scenario,
            "--seed",
            self.seed,
            "--state-sample-nodes",
            str(self.args.state_sample_nodes),
            "--app-hash-window",
            str(self.args.app_hash_window),
            "--receipt-resolution",
            "concurrent",
            "--receipt-workers",
            str(self.args.receipt_workers),
            "--round-robin-submission",
        ]
        if counter_ops is not None:
            cmd.extend(["--counter-ops", str(counter_ops)])
        if dex_rounds is not None:
            cmd.extend(["--dex-rounds", str(dex_rounds)])

        result = run_cmd(cmd, cwd=STACK_DIR)
        payload = None
        for line_index in range(len(result.stdout.splitlines())):
            candidate = "\n".join(result.stdout.splitlines()[line_index:])
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict) and decoded.get("ok") is True:
                payload = decoded
                break
        if payload is None:
            raise E2EError(f"could not parse workload output for {scenario}")
        return payload

    async def burst_phase(self) -> dict[str, Any]:
        payload = await self.run_localnet_workload(
            scenario="counter_basic",
            counter_ops=self.args.burst_counter_ops,
        )
        tx_count = payload["scenario_summary"]["successful_transactions"]
        elapsed = float(payload["elapsed_seconds"])
        payload["approx_tps"] = round(tx_count / elapsed, 3)
        return payload

    async def conflict_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        conflict_contract = self.contracts["conflict"]
        wallets = [derive_wallet(self.seed, f"conflict-wallet-{index}") for index in range(2)]
        await self.fund_wallets(session, wallets, amount=500)

        async def claim_once(wallet: Wallet, node_index: int, slot: str, label: str):
            async with self.client(wallet, node_index, session) as client:
                submission = await client.send_tx(
                    conflict_contract,
                    "claim",
                    {"slot": slot, "amount": 1},
                    stamps=DEFAULT_TX_STAMPS,
                    wait_for_tx=True,
                )
                return normalize_receipt(submission, label=label)

        race_slot = f"race-{short_hash(self.run_id)}"
        race_results = await asyncio.gather(
            claim_once(wallets[0], 0, race_slot, "claim-a"),
            claim_once(wallets[1], 1, race_slot, "claim-b"),
        )
        successes = [item for item in race_results if item["success"] is True]
        failures = [item for item in race_results if item["success"] is False]
        if len(successes) != 1 or len(failures) != 1:
            raise E2EError(
                "expected exactly one winning and one losing claim in conflict phase"
            )

        async with self.client(wallets[0], 2, session) as client:
            invalid = await client.send_tx(
                conflict_contract,
                "claim",
                {"slot": "", "amount": -1},
                stamps=DEFAULT_TX_STAMPS,
                wait_for_tx=True,
            )
            invalid_result = normalize_receipt(invalid, label="invalid-claim")
            if invalid_result["success"] is not False:
                raise E2EError("invalid conflict tx unexpectedly succeeded")

            current_counter = await client.call(conflict_contract, "current", {})

        self.sample_event_tx_hash = successes[0]["tx_hash"]
        return {
            "conflict_contract": conflict_contract,
            "race_results": race_results,
            "winning_tx_hash": successes[0]["tx_hash"],
            "invalid_result": invalid_result,
            "current_counter": current_counter,
        }

    async def dex_phase(self) -> dict[str, Any]:
        payload = await self.run_localnet_workload(
            scenario="dex_mixed",
            dex_rounds=self.args.dex_rounds,
        )
        contracts = payload["scenario_summary"]["contracts"]
        self.contracts["dex_token_a"] = contracts["token_a"]
        self.contracts["dex_token_b"] = contracts["token_b"]
        self.contracts["dex_pairs"] = contracts["pairs"]
        self.contracts["dex_router"] = contracts["dex"]
        self.contracts["dex_pair_id"] = payload["scenario_summary"]["pair_id"]
        tx_count = (
            payload["scenario_summary"]["deployment_transactions"]
            + payload["scenario_summary"]["funding_transactions"]
            + payload["scenario_summary"]["approval_transactions"]
            + payload["scenario_summary"]["workload_transactions"]
        )
        payload["approx_tps"] = round(tx_count / float(payload["elapsed_seconds"]), 3)
        return payload

    async def simulator_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        dex_contract = self.contracts.get("dex_router")
        token_a = self.contracts.get("dex_token_a")
        token_b = self.contracts.get("dex_token_b")
        pair_id = self.contracts.get("dex_pair_id")
        founder = self.founder_wallet
        if not all([dex_contract, token_a, token_b, pair_id]):
            raise E2EError("DEX contracts are not available for simulator phase")

        async def one_simulation(index: int) -> dict[str, Any]:
            node_index = index % len(self.nodes)
            async with self.client(founder, node_index, session) as client:
                started = time.monotonic()
                result = await client.simulate(
                    dex_contract,
                    "swapExactTokenForToken",
                    {
                        "amountIn": 5.0 + index,
                        "amountOutMin": 1.0,
                        "path": [token_a, token_b],
                        "to": founder.public_key,
                        "deadline": {
                            "__time__": [
                                2099,
                                1,
                                1,
                                0,
                                0,
                                0,
                                0,
                            ]
                        },
                    },
                )
                elapsed = time.monotonic() - started
                return {
                    "node": self.nodes[node_index].moniker,
                    "elapsed_ms": round(elapsed * 1000, 2),
                    "status": result.get("status"),
                    "result": normalize_value(result.get("result")),
                }

        started = time.monotonic()
        responses = await asyncio.gather(
            *(one_simulation(index) for index in range(SIMULATOR_BURST_REQUESTS))
        )
        elapsed = time.monotonic() - started
        failures = [item for item in responses if item["status"] not in (None, 0)]
        if failures:
            raise E2EError(f"simulator phase had {len(failures)} failing simulations")
        return {
            "request_count": len(responses),
            "elapsed_seconds": round(elapsed, 3),
            "approx_qps": round(len(responses) / elapsed, 3),
            "sample": responses[:8],
        }

    async def retrieval_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        founder = self.founder_wallet
        service = self.service_node
        if service is None:
            raise E2EError("service node not available")
        async with self.client(founder, service.index, session) as client:
            current_height = await latest_height(session, service.rpc_url)
            bds_status = await wait_for_bds_indexed(
                client,
                target_height=current_height,
                timeout_seconds=30.0,
            )
            blocks = await client.list_blocks(limit=5, offset=0)
            events = await client.list_events(
                self.contracts["conflict"],
                "Claimed",
                limit=10,
            )
            state_history = await client.get_state_history(
                f"{self.contracts['conflict']}.counter",
                limit=10,
                offset=0,
            )
            tx_events = (
                await client.get_events_for_tx(self.sample_event_tx_hash)
                if self.sample_event_tx_hash
                else []
            )
            tx_state = (
                await client.get_state_for_tx(self.sample_event_tx_hash)
                if self.sample_event_tx_hash
                else []
            )
            current_state = await client.get_state(self.contracts["conflict"], "counter")
            current_events = await client.list_events(
                self.contracts["conflict"],
                "Claimed",
                limit=1,
            )
            after_event_id = None
            if current_events:
                after_event_id = current_events[0].id

            async def next_block():
                async for block in client.watch_blocks(
                    poll_interval_seconds=0.5,
                ):
                    return normalize_value(block.raw)
                return None

            async def next_event():
                async for item in client.watch_events(
                    self.contracts["conflict"],
                    "Claimed",
                    after_id=after_event_id,
                    limit=5,
                    poll_interval_seconds=0.5,
                ):
                    return normalize_value(item.raw)
                return None

            block_task = asyncio.create_task(next_block())
            event_task = asyncio.create_task(next_event())
            await asyncio.sleep(0.5)
            trigger_wallet = derive_wallet(self.seed, "retrieval-trigger")
            await self.fund_wallets(session, [trigger_wallet], amount=100)
            ws_message = await self.websocket_tx_event(
                session,
                node=service,
                trigger_tx_hash=None,
                trigger_tx_coro=self._send_retrieval_trigger(session, trigger_wallet),
            )
            watched_block = await asyncio.wait_for(block_task, timeout=20.0)
            watched_event = await asyncio.wait_for(event_task, timeout=20.0)

        return {
            "bds_status": bds_status,
            "blocks": [normalize_value(block.raw) for block in blocks],
            "events": [normalize_value(item.raw) for item in events],
            "state_history": [normalize_value(item.raw) for item in state_history],
            "tx_events": [normalize_value(item.raw) for item in tx_events],
            "tx_state": [normalize_value(item.raw) for item in tx_state],
            "current_state": normalize_value(current_state),
            "watch_block": watched_block,
            "watch_event": watched_event,
            "websocket_tx_event": ws_message,
        }

    async def determinism_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        founder = self.founder_wallet
        conflict_contract = self.contracts["conflict"]
        patch_contract = self.contracts["patch_target"]

        consensus = await compare_app_hash_window(
            session,
            self.nodes,
            window=self.args.app_hash_window,
        )
        if not consensus["ok"]:
            raise E2EError("app hash mismatch detected during determinism phase")

        counter_state = await query_state_from_all_nodes(
            session,
            self.nodes,
            contract=conflict_contract,
            variable="counter",
        )
        patch_mode_state = await query_state_from_all_nodes(
            session,
            self.nodes,
            contract=patch_contract,
            variable="mode",
        )

        unique_state_groups = {
            "conflict_counter": sorted({json.dumps(value, sort_keys=True) for value in counter_state.values()}),
            "patch_mode": sorted({json.dumps(value, sort_keys=True) for value in patch_mode_state.values()}),
        }

        if any(len(group) != 1 for group in unique_state_groups.values()):
            raise E2EError("state values diverged across validators")

        simulation_results = []
        dex_router = self.contracts.get("dex_router")
        token_a = self.contracts.get("dex_token_a")
        dex_pair_id = self.contracts.get("dex_pair_id")
        if dex_router and token_a and dex_pair_id is not None:
            deadline = {"__time__": [2099, 1, 1, 0, 0, 0, 0]}
            for node in self.nodes:
                async with self.client(founder, node.index, session) as client:
                    result = await client.simulate(
                        dex_router,
                        "swapExactTokenForToken",
                        {
                            "amountIn": 7.0,
                            "amountOutMin": 0.0,
                            "pair": dex_pair_id,
                            "src": token_a,
                            "to": founder.public_key,
                            "deadline": deadline,
                        },
                    )
                    simulation_results.append(
                        {
                            "node": node.moniker,
                            "status": result.get("status"),
                            "stamps_used": result.get("stamps_used"),
                            "result": normalize_value(result.get("result")),
                        }
                    )

            comparison_keys = {
                json.dumps(
                    {
                        "status": item["status"],
                        "stamps_used": item["stamps_used"],
                        "result": item["result"],
                    },
                    sort_keys=True,
                )
                for item in simulation_results
            }
            if len(comparison_keys) != 1:
                raise E2EError("simulation results diverged across validators")

        return {
            "consensus": consensus,
            "state_samples": {
                "conflict_counter": counter_state,
                "patch_mode": patch_mode_state,
            },
            "simulation_results": simulation_results,
        }

    async def websocket_tx_event(
        self,
        session: aiohttp.ClientSession,
        *,
        node: LocalnetNode,
        trigger_tx_hash: str | None,
        trigger_tx_coro=None,
    ) -> dict[str, Any]:
        ws_url = node.rpc_url.replace("http://", "ws://") + "/websocket"
        async with session.ws_connect(ws_url, timeout=WEBSOCKET_TIMEOUT_SECONDS) as ws:
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "subscribe",
                    "id": "1",
                    "params": {"query": "tm.event='Tx'"},
                }
            )
            if trigger_tx_coro is not None:
                trigger_tx_hash = await trigger_tx_coro
            if trigger_tx_hash is None:
                raise E2EError("websocket_tx_event requires a trigger tx hash")
            deadline = time.monotonic() + WEBSOCKET_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                message = await ws.receive(timeout=WEBSOCKET_TIMEOUT_SECONDS)
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                result = payload.get("result", {})
                data = result.get("data", {})
                value = data.get("value", {})
                tx_result = value.get("TxResult", {}).get("result", {})
                tx_hash = tx_result.get("hash")
                if tx_hash == trigger_tx_hash:
                    return payload
        raise E2EError("websocket did not emit the expected tx event")

    async def _send_retrieval_trigger(
        self,
        session: aiohttp.ClientSession,
        trigger_wallet: Wallet,
    ) -> str:
        async with self.client(trigger_wallet, 0, session) as trigger_client:
            trigger = await trigger_client.send_tx(
                self.contracts["conflict"],
                "claim",
                {"slot": f"retrieval-{short_hash(self.run_id)}", "amount": 1},
                stamps=DEFAULT_TX_STAMPS,
                wait_for_tx=True,
            )
            trigger_receipt = ensure_positive_submission(
                trigger,
                label="retrieval-trigger",
            )
            return trigger_receipt["tx_hash"]

    async def validator_governance_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        node0_wallet, node1_wallet, node2_wallet, node3_wallet = self.validator_wallets
        node3_key = self.nodes[3].account_public_key

        async with self.client(node0_wallet, 0, session) as node0, self.client(
            node1_wallet, 1, session
        ) as node1, self.client(node2_wallet, 2, session) as node2, self.client(
            node3_wallet, 3, session
        ) as node3:
            power_proposal = await node0.send_tx(
                "masternodes",
                "propose_vote",
                {
                    "type_of_vote": "set_member_power",
                    "arg": {"member": node3_key, "power": 15},
                },
                stamps=GOVERNANCE_TX_STAMPS,
                wait_for_tx=True,
            )
            power_receipt = ensure_positive_submission(
                power_proposal,
                label="set-power-propose",
            )
            power_proposal_id = await node0.get_state("masternodes", "total_votes")
            vote_1 = await node1.send_tx(
                "masternodes",
                "vote",
                {"proposal_id": power_proposal_id, "vote": "yes"},
                stamps=GOVERNANCE_TX_STAMPS,
                wait_for_tx=True,
            )
            vote_2 = await node2.send_tx(
                "masternodes",
                "vote",
                {"proposal_id": power_proposal_id, "vote": "yes"},
                stamps=GOVERNANCE_TX_STAMPS,
                wait_for_tx=True,
            )
            vote_3 = await node3.send_tx(
                "masternodes",
                "vote",
                {"proposal_id": power_proposal_id, "vote": "yes"},
                stamps=GOVERNANCE_TX_STAMPS,
                wait_for_tx=True,
            )
            for receipt, label in (
                (vote_1, "set-power-vote-1"),
                (vote_2, "set-power-vote-2"),
                (vote_3, "set-power-vote-3"),
            ):
                ensure_positive_submission(receipt, label=label)

            power_wait_height = await latest_height(session, self.nodes[0].rpc_url) + 2
            await wait_for_height(
                session,
                self.nodes[0].rpc_url,
                power_wait_height,
                timeout_seconds=20.0,
            )
            power_record = await node0.call(
                "masternodes",
                "get_validator",
                {"account": node3_key},
            )
            if power_record["power"] != 15:
                raise E2EError("validator power change did not apply")

            remove_proposal = await node0.send_tx(
                "masternodes",
                "propose_vote",
                {
                    "type_of_vote": "remove_member",
                    "arg": node3_key,
                },
                stamps=GOVERNANCE_TX_STAMPS,
                wait_for_tx=True,
            )
            remove_receipt = ensure_positive_submission(
                remove_proposal,
                label="remove-member-propose",
            )
            remove_proposal_id = await node0.get_state("masternodes", "total_votes")
            for client, label in (
                (node1, "remove-vote-1"),
                (node2, "remove-vote-2"),
                (node3, "remove-vote-3"),
            ):
                vote_submission = await client.send_tx(
                    "masternodes",
                    "vote",
                    {"proposal_id": remove_proposal_id, "vote": "yes"},
                    stamps=GOVERNANCE_TX_STAMPS,
                    wait_for_tx=True,
                )
                ensure_positive_submission(vote_submission, label=label)

            remove_wait_height = (
                int(
                    (
                        await fetch_json(
                            session,
                            f"{self.nodes[0].rpc_url}/status",
                            timeout=5.0,
                        )
                    )["result"]["sync_info"]["latest_block_height"]
                )
                + 2
            )
            await wait_for_height(
                session,
                self.nodes[0].rpc_url,
                remove_wait_height,
                timeout_seconds=30.0,
            )
            validators_after_remove = await fetch_json(
                session,
                f"{self.nodes[0].rpc_url}/validators",
                timeout=5.0,
            )
            if len(validators_after_remove["result"]["validators"]) != 3:
                raise E2EError("validator removal did not reduce the validator set to 3")

            registration_fee = await node0.get_state("masternodes", "registration_fee")
            approval_submission = await node3.send_tx(
                "currency",
                "approve",
                {"amount": registration_fee, "to": "masternodes"},
                stamps=DEFAULT_TX_STAMPS,
                wait_for_tx=True,
            )
            approval_receipt = ensure_positive_submission(
                approval_submission,
                label="re-register-approve-node3",
            )

            register_submission = await node3.send_tx(
                "masternodes",
                "register",
                {
                    "requested_validator_power": 12,
                    "moniker": "node-3-return",
                    "network_endpoint": "localnet://node-3",
                },
                stamps=GOVERNANCE_TX_STAMPS,
                wait_for_tx=True,
            )
            ensure_positive_submission(register_submission, label="re-register-node3")
            add_proposal = await node0.send_tx(
                "masternodes",
                "propose_vote",
                {"type_of_vote": "add_member", "arg": node3_key},
                stamps=GOVERNANCE_TX_STAMPS,
                wait_for_tx=True,
            )
            add_receipt = ensure_positive_submission(
                add_proposal,
                label="add-member-propose",
            )
            add_proposal_id = await node0.get_state("masternodes", "total_votes")
            for client, label in (
                (node1, "add-vote-1"),
                (node2, "add-vote-2"),
            ):
                vote_submission = await client.send_tx(
                    "masternodes",
                    "vote",
                    {"proposal_id": add_proposal_id, "vote": "yes"},
                    stamps=GOVERNANCE_TX_STAMPS,
                    wait_for_tx=True,
                )
                ensure_positive_submission(vote_submission, label=label)

            readd_wait_height = (
                int(
                    (
                        await fetch_json(
                            session,
                            f"{self.nodes[0].rpc_url}/status",
                            timeout=5.0,
                        )
                    )["result"]["sync_info"]["latest_block_height"]
                )
                + 2
            )
            await wait_for_height(
                session,
                self.nodes[0].rpc_url,
                readd_wait_height,
                timeout_seconds=30.0,
            )
            validators_after_add = await fetch_json(
                session,
                f"{self.nodes[0].rpc_url}/validators",
                timeout=5.0,
            )
            if len(validators_after_add["result"]["validators"]) != 4:
                raise E2EError("validator add-back did not restore the validator set to 4")
            validator_record = await node0.call(
                "masternodes",
                "get_validator",
                {"account": node3_key},
            )

        return {
            "power_change": power_receipt,
            "power_record": normalize_value(power_record),
            "remove_receipt": remove_receipt,
            "re_register_approval": approval_receipt,
            "add_receipt": add_receipt,
            "validators_after_remove": normalize_value(validators_after_remove),
            "validators_after_add": normalize_value(validators_after_add),
            "node3_validator_record": normalize_value(validator_record),
        }

    async def state_patch_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        service = self.service_node
        founder = self.founder_wallet
        current_height = int(
            (
                await fetch_json(
                    session,
                    f"{service.rpc_url}/status",
                    timeout=5.0,
                )
            )["result"]["sync_info"]["latest_block_height"]
        )
        activation_height = current_height + STATE_PATCH_DELAY_BLOCKS
        patch_id = f"localnet-e2e-{short_hash(self.run_id)}"
        bundle_payload = {
            "version": 1,
            "patch_id": patch_id,
            "activation_height": activation_height,
            "governance_contract": "governance",
            "summary": "Localnet E2E patch exercise",
            "uri": "local://localnet-e2e",
            "chain_id": self.network["chain_id"],
            "changes": [
                {
                    "key": f"{self.contracts['patch_target']}.mode",
                    "value": "patched",
                    "comment": "switch into patched mode",
                },
                {
                    "key": f"{self.contracts['patch_target']}.patch_count",
                    "value": 1,
                    "comment": "record that a patch applied",
                },
            ],
        }
        bundle_payload["bundle_hash"] = compute_patch_bundle_hash(bundle_payload)
        for node in self.nodes:
            patch_dir = STACK_DIR / ".localnet" / node.moniker / ".cometbft" / "config" / "state-patches"
            patch_dir.mkdir(parents=True, exist_ok=True)
            (patch_dir / f"{patch_id}.json").write_text(
                json.dumps(bundle_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        run_cmd(
            ["docker", "compose", "-f", "docker-compose-localnet.yml", "restart"],
            cwd=STACK_DIR,
        )
        await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=self.args.rpc_timeout_seconds,
        )

        node0_wallet, node1_wallet, node2_wallet, node3_wallet = self.validator_wallets
        async with self.client(node0_wallet, 0, session) as node0, self.client(
            node1_wallet, 1, session
        ) as node1, self.client(node2_wallet, 2, session) as node2, self.client(
            node3_wallet, 3, session
        ) as node3:
            proposal = await node0.send_tx(
                "governance",
                "propose_state_patch",
                {
                    "patch_id": patch_id,
                    "bundle_hash": bundle_payload["bundle_hash"],
                    "activation_height": activation_height,
                    "summary": bundle_payload["summary"],
                    "uri": bundle_payload["uri"],
                    "emergency": False,
                },
                stamps=GOVERNANCE_TX_STAMPS,
                wait_for_tx=True,
            )
            proposal_receipt = ensure_positive_submission(
                proposal,
                label="state-patch-propose",
            )
            proposal_id = await node0.get_state("governance", "proposal_count")
            for client, label in (
                (node1, "state-patch-vote-1"),
                (node2, "state-patch-vote-2"),
                (node3, "state-patch-vote-3"),
            ):
                submission = await client.send_tx(
                    "governance",
                    "vote",
                    {"proposal_id": proposal_id, "support": True},
                    stamps=GOVERNANCE_TX_STAMPS,
                    wait_for_tx=True,
                )
                ensure_positive_submission(submission, label=label)

        await wait_for_height(
            session,
            self.nodes[0].rpc_url,
            activation_height + 1,
            timeout_seconds=60.0,
        )
        async with self.client(founder, service.index, session) as client:
            indexed_status = await wait_for_bds_indexed(
                client,
                target_height=activation_height,
                timeout_seconds=30.0,
            )
            patch_status = await client.call("governance", "get_patch", {"patch_id": patch_id})
            contract_status = await client.call(
                self.contracts["patch_target"],
                "get_status",
                {},
            )
            local_bundles = await fetch_abci_query(
                session,
                service.rpc_url,
                "/state_patch_bundles",
            )
            scheduled = await fetch_abci_query(
                session,
                service.rpc_url,
                f"/scheduled_state_patches/{activation_height}",
            )
            indexed_state_patches = await fetch_abci_query(
                session,
                service.rpc_url,
                "/state_patches",
            )
            indexed_state_patches_for_block = await fetch_abci_query(
                session,
                service.rpc_url,
                f"/state_patches_for_block/{activation_height}",
            )

        return {
            "bundle": bundle_payload,
            "proposal_receipt": proposal_receipt,
            "governance_patch": normalize_value(patch_status),
            "patch_target_status": normalize_value(contract_status),
            "local_bundle_inventory": normalize_value(local_bundles),
            "scheduled_inventory": normalize_value(scheduled),
            "indexed_state_patches": normalize_value(indexed_state_patches),
            "indexed_state_patches_for_block": normalize_value(
                indexed_state_patches_for_block
            ),
            "indexed_status": indexed_status,
        }

    async def logging_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        initial_logs = {
            node.moniker: [str(path) for path in local_log_paths(node)]
            for node in self.nodes
        }
        update_logging_config(level="DEBUG", trace_logging=False, json_logging=False)
        run_cmd(["docker", "compose", "-f", "docker-compose-localnet.yml", "restart"], cwd=STACK_DIR)
        await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=self.args.rpc_timeout_seconds,
        )

        trigger_wallet = derive_wallet(self.seed, "logging-trigger")
        await self.fund_wallets(session, [trigger_wallet], amount=50)
        async with self.client(trigger_wallet, 0, session) as client:
            debug_submission = await client.send(
                amount=1,
                to_address=self.founder_wallet.public_key,
                stamps=DEFAULT_TRANSFER_STAMPS,
                wait_for_tx=True,
            )
            debug_receipt = ensure_positive_submission(debug_submission, label="debug-log-trigger")

        update_logging_config(level="TRACE", trace_logging=True, json_logging=False)
        run_cmd(["docker", "compose", "-f", "docker-compose-localnet.yml", "restart"], cwd=STACK_DIR)
        await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=self.args.rpc_timeout_seconds,
        )

        async with self.client(trigger_wallet, 0, session) as client:
            trace_submission = await client.send(
                amount=1,
                to_address=self.founder_wallet.public_key,
                stamps=DEFAULT_TRANSFER_STAMPS,
                wait_for_tx=True,
            )
            trace_receipt = ensure_positive_submission(trace_submission, label="trace-log-trigger")

        debug_lines: dict[str, list[str]] = {}
        trace_lines: dict[str, list[str]] = {}
        for node in self.nodes:
            logs = local_log_paths(node)
            if not logs:
                raise E2EError(f"no logs found for {node.moniker}")
            text = logs[-1].read_text(encoding="utf-8")
            debug_lines[node.moniker] = [
                line for line in text.splitlines() if "stage=check_tx" in line
            ][-3:]
            trace_lines[node.moniker] = [
                line for line in text.splitlines() if "stage=finalize_tx_result" in line
            ][-3:]
            if not debug_lines[node.moniker]:
                raise E2EError(f"DEBUG logs missing stage=check_tx for {node.moniker}")
            if not trace_lines[node.moniker]:
                raise E2EError(
                    f"TRACE logs missing stage=finalize_tx_result for {node.moniker}"
                )

        update_logging_config(level=self.args.log_level, trace_logging=False, json_logging=False)
        run_cmd(["docker", "compose", "-f", "docker-compose-localnet.yml", "restart"], cwd=STACK_DIR)
        await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=self.args.rpc_timeout_seconds,
        )

        return {
            "initial_logs": initial_logs,
            "debug_receipt": debug_receipt,
            "trace_receipt": trace_receipt,
            "debug_matches": debug_lines,
            "trace_matches": trace_lines,
        }

    async def shielded_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        if ShieldedNoteProver is None:
            return {
                "skipped": True,
                "reason": f"xian_zk unavailable: {XIAN_ZK_IMPORT_ERROR}",
            }

        founder = self.founder_wallet
        alice = derive_wallet(self.seed, "shielded-alice")
        bob = derive_wallet(self.seed, "shielded-bob")
        await self.fund_wallets(session, [alice, bob], amount=5_000)

        registry_name = "zk_registry"
        token_name = f"con_private_e2e_{short_hash(self.run_id)}"
        async with self.client(founder, 0, session) as client:
            registry_submission = await client.submit_contract(
                name=registry_name,
                code=read_text(
                    ROOT_DIR
                    / "xian-contracting"
                    / "src"
                    / "contracting"
                    / "contracts"
                    / "zk_registry.s.py"
                ),
                stamps=200_000,
                wait_for_tx=True,
            )
            ensure_positive_submission(registry_submission, label="deploy-zk-registry")
            token_submission = await client.submit_contract(
                name=token_name,
                code=read_text(
                    CONTRACTS_DIR
                    / "shielded-note-token"
                    / "src"
                    / "con_shielded_note_token.py"
                ),
                args={
                    "token_name": "Local Private USD",
                    "token_symbol": "lpUSD",
                    "operator_address": founder.public_key,
                    "root_window_size": 32,
                },
                stamps=400_000,
                wait_for_tx=True,
            )
            ensure_positive_submission(token_submission, label="deploy-shielded-token")
            prover = ShieldedNoteProver.build_insecure_dev_bundle()
            for action in ("deposit", "transfer", "withdraw"):
                vk = prover.bundle[action]
                register_submission = await client.send_tx(
                    registry_name,
                    "register_vk",
                    {
                        "vk_id": vk["vk_id"],
                        "vk_hex": vk["vk_hex"],
                        "circuit_name": vk["circuit_name"],
                        "version": vk["version"],
                    },
                    stamps=200_000,
                    wait_for_tx=True,
                )
                ensure_positive_submission(
                    register_submission,
                    label=f"register-vk-{action}",
                )
                bind_submission = await client.send_tx(
                    token_name,
                    "configure_vk",
                    {"action": action, "vk_id": vk["vk_id"]},
                    stamps=100_000,
                    wait_for_tx=True,
                )
                ensure_positive_submission(
                    bind_submission,
                    label=f"bind-vk-{action}",
                )
            mint_submission = await client.send_tx(
                token_name,
                "mint_public",
                {"amount": 100, "to": alice.public_key},
                stamps=50_000,
                wait_for_tx=True,
            )
            ensure_positive_submission(mint_submission, label="shielded-mint-public")
            asset_id = await client.call(token_name, "asset_id", {})
            zero_root = await client.call(token_name, "zero_shielded_root", {})

        alice_keys = ShieldedKeyBundle.generate()
        bob_keys = ShieldedKeyBundle.generate()
        alice_note_1 = ShieldedNote(
            owner_secret=alice_keys.owner_secret,
            amount=30,
            rho=101,
            blind=201,
        )
        alice_note_2 = ShieldedNote(
            owner_secret=alice_keys.owner_secret,
            amount=20,
            rho=102,
            blind=202,
        )
        bob_note = ShieldedNote(
            owner_secret=bob_keys.owner_secret,
            amount=20,
            rho=103,
            blind=203,
        )
        alice_change = ShieldedNote(
            owner_secret=alice_keys.owner_secret,
            amount=30,
            rho=104,
            blind=204,
        )

        async with self.client(alice, 1, session) as alice_client, self.client(
            founder, 0, session
        ) as founder_client:
            deposit = prover.prove_deposit(
                ShieldedDepositRequest(
                    asset_id=asset_id,
                    old_root=zero_root,
                    append_state=tree_state([]),
                    amount=50,
                    outputs=[alice_note_1.to_output(), alice_note_2.to_output()],
                )
            )
            deposit_submission = await alice_client.send_tx(
                token_name,
                "deposit_shielded",
                {
                    "amount": 50,
                    "old_root": deposit.old_root,
                    "output_commitments": deposit.output_commitments,
                    "proof_hex": deposit.proof_hex,
                    "output_payloads": [
                        alice_note_1.to_output().encrypt_for(
                            asset_id=asset_id,
                            viewing_public_key=alice_keys.viewing_public_key,
                        ),
                        alice_note_2.to_output().encrypt_for(
                            asset_id=asset_id,
                            viewing_public_key=alice_keys.viewing_public_key,
                        ),
                    ],
                },
                stamps=300_000,
                wait_for_tx=True,
            )
            ensure_positive_submission(
                deposit_submission,
                label="shielded-deposit",
            )

            records_after_deposit = await founder_client.call(
                token_name,
                "list_note_records",
                {"start": 0, "limit": 8},
            )
            recovered_alice = recover_encrypted_notes(
                asset_id=asset_id,
                commitments=[record["commitment"] for record in records_after_deposit],
                payloads=[record["payload"] for record in records_after_deposit],
                owner_secret=alice_keys.owner_secret,
                viewing_private_key=alice_keys.viewing_private_key,
            )
            discovered_after_deposit = scan_notes(
                asset_id=asset_id,
                commitments=deposit.output_commitments,
                notes=[alice_note_1, alice_note_2],
            )
            transfer = prover.prove_transfer(
                ShieldedTransferRequest(
                    asset_id=asset_id,
                    old_root=deposit.expected_new_root,
                    append_state=tree_state(deposit.output_commitments),
                    inputs=[match.to_input() for match in discovered_after_deposit],
                    outputs=[
                        ShieldedOutput.for_recipient(
                            bob_keys.recipient,
                            amount=bob_note.amount,
                            rho=bob_note.rho,
                            blind=bob_note.blind,
                        ),
                        alice_change.to_output(),
                    ],
                )
            )
            transfer_submission = await alice_client.send_tx(
                token_name,
                "transfer_shielded",
                {
                    "old_root": transfer.old_root,
                    "input_nullifiers": transfer.input_nullifiers,
                    "output_commitments": transfer.output_commitments,
                    "proof_hex": transfer.proof_hex,
                    "output_payloads": [
                        ShieldedOutput.for_recipient(
                            bob_keys.recipient,
                            amount=bob_note.amount,
                            rho=bob_note.rho,
                            blind=bob_note.blind,
                        ).encrypt_for(
                            asset_id=asset_id,
                            viewing_public_key=bob_keys.viewing_public_key,
                        ),
                        alice_change.to_output().encrypt_for(
                            asset_id=asset_id,
                            viewing_public_key=alice_keys.viewing_public_key,
                        ),
                    ],
                },
                stamps=400_000,
                wait_for_tx=True,
            )
            ensure_positive_submission(
                transfer_submission,
                label="shielded-transfer",
            )

            records_after_transfer = await founder_client.call(
                token_name,
                "list_note_records",
                {"start": 0, "limit": 12},
            )
            recovered_bob = recover_encrypted_notes(
                asset_id=asset_id,
                commitments=[record["commitment"] for record in records_after_transfer],
                payloads=[record["payload"] for record in records_after_transfer],
                owner_secret=bob_keys.owner_secret,
                viewing_private_key=bob_keys.viewing_private_key,
            )
            discovered_after_transfer = scan_notes(
                asset_id=asset_id,
                commitments=deposit.output_commitments + transfer.output_commitments,
                notes=[alice_change],
            )

        async with self.client(alice, 1, session) as alice_client, self.client(
            founder, 0, session
        ) as founder_client:
            withdraw = prover.prove_withdraw(
                ShieldedWithdrawRequest(
                    asset_id=asset_id,
                    old_root=transfer.expected_new_root,
                    append_state=tree_state(
                        deposit.output_commitments + transfer.output_commitments
                    ),
                    amount=10,
                    recipient=bob.public_key,
                    inputs=[discovered_after_transfer[0].to_input()],
                    outputs=[],
                )
            )
            withdraw_submission = await alice_client.send_tx(
                token_name,
                "withdraw_shielded",
                {
                    "amount": 10,
                    "to": bob.public_key,
                    "old_root": withdraw.old_root,
                    "input_nullifiers": withdraw.input_nullifiers,
                    "output_commitments": withdraw.output_commitments,
                    "proof_hex": withdraw.proof_hex,
                    "output_payloads": [],
                },
                stamps=300_000,
                wait_for_tx=True,
            )
            ensure_positive_submission(
                withdraw_submission,
                label="shielded-withdraw",
            )
            alice_public = await founder_client.call(
                token_name,
                "balance_of",
                {"account": alice.public_key},
            )
            bob_public = await founder_client.call(
                token_name,
                "balance_of",
                {"account": bob.public_key},
            )
            supply_state = await founder_client.call(token_name, "get_supply_state", {})

        return {
            "token": token_name,
            "registry": registry_name,
            "alice_public_balance": alice_public,
            "bob_public_balance": bob_public,
            "supply_state": normalize_value(supply_state),
            "alice_recovered_notes": len(recovered_alice),
            "bob_recovered_notes": len(recovered_bob),
        }

    async def finalize_summary(self) -> dict[str, Any]:
        return {
            "ok": all(phase.ok for phase in self.phase_results),
            "run_id": self.run_id,
            "output_dir": str(self.output_dir),
            "chain_id": None if self.network is None else self.network["chain_id"],
            "contracts": self.contracts,
            "phases": [
                {
                    "name": phase.name,
                    "ok": phase.ok,
                    "started_at": phase.started_at,
                    "ended_at": phase.ended_at,
                }
                for phase in self.phase_results
            ],
        }

    async def run(self) -> int:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, sock_connect=5, sock_read=25),
            connector=aiohttp.TCPConnector(limit=256, ttl_dns_cache=300),
        ) as session:
            await self.run_phase("00-bootstrap", lambda: self.bootstrap(session))
            await self.run_phase("01-health", lambda: self.health_phase(session))
            await self.run_phase("02-xian-py-smoke", lambda: self.xian_py_smoke(session))
            await self.run_phase("03-periodic-load", lambda: self.periodic_load(session))
            await self.run_phase("04-burst-load", self.burst_phase)
            await self.run_phase("05-conflict-invalid", lambda: self.conflict_phase(session))
            await self.run_phase("06-dex-mixed", self.dex_phase)
            await self.run_phase("07-simulator-load", lambda: self.simulator_phase(session))
            await self.run_phase("08-retrieval-surfaces", lambda: self.retrieval_phase(session))
            await self.run_phase("09-determinism", lambda: self.determinism_phase(session))
            await self.run_phase("10-validator-governance", lambda: self.validator_governance_phase(session))
            await self.run_phase("11-state-patch", lambda: self.state_patch_phase(session))
            await self.run_phase("12-logging", lambda: self.logging_phase(session))
            await self.run_phase("13-shielded-note-token", lambda: self.shielded_phase(session))

        summary = await self.finalize_summary()
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a layered 4-node localnet end-to-end test program",
    )
    parser.add_argument("--bootstrap", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--build", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--nodes", type=int, default=4)
    parser.add_argument("--topology", choices=("integrated", "fidelity"), default="integrated")
    parser.add_argument("--bds-node-index", type=int, default=0)
    parser.add_argument("--seed", default="xian-localnet-e2e-v1")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--rpc-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--state-sample-nodes", type=int, default=4)
    parser.add_argument("--app-hash-window", type=int, default=4)
    parser.add_argument("--receipt-workers", type=int, default=24)
    parser.add_argument("--periodic-rounds", type=int, default=8)
    parser.add_argument("--periodic-interval-seconds", type=float, default=0.35)
    parser.add_argument("--burst-counter-ops", type=int, default=260)
    parser.add_argument("--dex-rounds", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = E2ERunner(args)
    return asyncio.run(runner.run())


if __name__ == "__main__":
    raise SystemExit(main())
