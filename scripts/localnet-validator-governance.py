#!/usr/bin/env python3
"""Run a focused validator, delegation, governance, and patch program."""

from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
from localnet_common import fetch_json

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent
ROOT_DIR = STACK_DIR.parent
NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"
OUTPUT_ROOT = STACK_DIR / ".artifacts" / "localnet-validator-governance"
XIAN_ABCI_SRC = ROOT_DIR / "xian-abci" / "src"
XIAN_CONTRACTING_SRC = ROOT_DIR / "xian-contracting" / "src"

DEFAULT_TRANSFER_CHI = 2_000
DEFAULT_TX_CHI = 200_000
GOVERNANCE_TX_CHI = 2_000_000
DEFAULT_LOCALNET_NODES = 5
DEFAULT_GENESIS_NETWORK = "testnet"
ABCI_HEALTH_QUERY_PATH = "/get/currency.balances:__xian_localnet_governance_health_probe__"
STATE_PATCH_DELAY_BLOCKS = 8
STATE_PATCH_ACTIVATION_HEADROOM_BLOCKS = 8
LOCALNET_IMAGE_BY_TOPOLOGY = {
    "integrated": "xian-node-integrated:local",
    "fidelity": "xian-node-split:local",
}

sys.path.append(str(XIAN_ABCI_SRC))
sys.path.insert(0, str(XIAN_CONTRACTING_SRC))

import nacl.signing  # noqa: E402
from cometbft.types.v1 import canonical_pb2  # noqa: E402
from contracting.artifacts import build_contract_artifacts  # noqa: E402
from google.protobuf.timestamp_pb2 import Timestamp  # noqa: E402
from xian_py.config import RetryPolicy, SubmissionConfig, XianClientConfig  # noqa: E402
from xian_py.wallet import Wallet  # noqa: E402
from xian_py.xian_async import XianAsync  # noqa: E402


def build_deployment_artifacts(module_name: str, source: str) -> dict[str, Any]:
    return build_contract_artifacts(
        module_name=module_name,
        source=source,
        lint=True,
        vm_profile="xian_vm_v1",
    )


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
    bds_node: bool


class RunnerError(RuntimeError):
    pass


def encode_uvarint(value: int) -> bytes:
    if value < 0:
        raise RunnerError(f"varint value must be non-negative, got {value}")
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def parse_rfc3339_timestamp(value: str) -> tuple[datetime, int]:
    timestamp_value = value
    if value.endswith("Z"):
        timestamp_value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(timestamp_value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    nanos = 0
    if "." in value:
        fraction = value.split(".", 1)[1]
        fraction = fraction.rstrip("Z")
        if "+" in fraction:
            fraction = fraction.split("+", 1)[0]
        if "-" in fraction:
            fraction = fraction.split("-", 1)[0]
        nanos = int(fraction.ljust(9, "0")[:9])

    return parsed, nanos


def protobuf_timestamp(value: str) -> Timestamp:
    parsed, nanos = parse_rfc3339_timestamp(value)
    return Timestamp(seconds=int(parsed.timestamp()), nanos=nanos)


def block_id_sort_key(block_hash: str, parts_total: int, parts_hash: str) -> tuple[int, str, str]:
    return (parts_total, block_hash, parts_hash)


def normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if value.__class__.__name__ == "ContractingDecimal":
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): normalize_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_value(item) for item in value]
    return value


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


def coerce_int(value: Any) -> int:
    normalized = normalize_value(value)
    if isinstance(normalized, bool):
        raise RunnerError(f"expected int-like value, got {normalized!r}")
    if isinstance(normalized, int):
        return normalized
    if isinstance(normalized, float) and normalized.is_integer():
        return int(normalized)
    if isinstance(normalized, str):
        try:
            decimal_value = Decimal(normalized)
        except Exception as exc:  # noqa: BLE001
            raise RunnerError(f"expected int-like value, got {normalized!r}") from exc
        if decimal_value == decimal_value.to_integral_value():
            return int(decimal_value)
    raise RunnerError(f"expected int-like value, got {normalized!r}")


def coerce_decimal(value: Any) -> Decimal:
    normalized = normalize_value(value)
    if isinstance(normalized, bool):
        raise RunnerError(f"expected decimal-like value, got {normalized!r}")
    if isinstance(normalized, Decimal):
        return normalized
    if isinstance(normalized, int):
        return Decimal(normalized)
    if isinstance(normalized, float):
        return Decimal(str(normalized))
    if isinstance(normalized, str):
        try:
            return Decimal(normalized)
        except Exception as exc:  # noqa: BLE001
            raise RunnerError(f"expected decimal-like value, got {normalized!r}") from exc
    raise RunnerError(f"expected decimal-like value, got {normalized!r}")


def load_network() -> dict[str, Any]:
    if not NETWORK_PATH.exists():
        raise RunnerError(f"localnet metadata not found at {NETWORK_PATH}; bootstrap first")
    return json.loads(NETWORK_PATH.read_text(encoding="utf-8"))


def derive_wallet(seed: str, label: str) -> Wallet:
    digest = hashlib.sha256(f"{seed}:{label}".encode("utf-8")).hexdigest()
    return Wallet(private_key=digest)


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


@functools.lru_cache(maxsize=1)
def load_stack_env() -> dict[str, str]:
    shell_script = f"""
source ./scripts/stack-env.sh
export_stack_env
{sys.executable} - <<'PY'
import json
import os

print(
    json.dumps(
        {{
            key: value
            for key, value in os.environ.items()
            if key.startswith("XIAN_")
        }}
    )
)
PY
""".strip()
    result = subprocess.run(
        ["bash", "-lc", shell_script],
        cwd=STACK_DIR,
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    return json.loads(result.stdout)


def make_localnet_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    env.update(load_stack_env())
    env["XIAN_LOCALNET_GENESIS_NETWORK"] = args.genesis_network
    env["XIAN_LOCALNET_ENABLE_BDS"] = "0"
    env["XIAN_LOCALNET_PORT_OFFSET"] = str(args.port_offset)
    env["XIAN_LOCALNET_APP_LOG_LEVEL"] = args.log_level
    env["XIAN_LOCALNET_APP_LOG_JSON"] = "0"
    env["XIAN_LOCALNET_TRANSACTION_TRACE_LOGGING"] = "0"
    env["LOCALNET_NODES"] = str(args.nodes)
    env["XIAN_LOCALNET_TOPOLOGY"] = args.topology
    env["XIAN_LOCALNET_PARALLEL_EXECUTION_ENABLED"] = "0"
    env["XIAN_LOCALNET_PARALLEL_EXECUTION_WORKERS"] = "0"
    env["XIAN_LOCALNET_PARALLEL_EXECUTION_MIN_TRANSACTIONS"] = "8"
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


def image_available(tag: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", tag],
        cwd=STACK_DIR,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


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
        raise RunnerError(f"ABCI query failed for {path}: {abci_response.get('log')}")
    encoded_value = abci_response.get("value")
    if not encoded_value:
        return None
    decoded = base64.b64decode(encoded_value).decode("utf-8")
    try:
        return json.loads(decoded)
    except json.JSONDecodeError:
        return decoded


async def fetch_json_fresh(
    url: str,
    *,
    timeout: float,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    connector = aiohttp.TCPConnector(force_close=True)
    async with aiohttp.ClientSession(timeout=client_timeout, connector=connector) as session:
        async with session.get(url, params=params) as response:
            return await response.json()


async def fetch_abci_query_fresh(
    rpc_url: str,
    path: str,
    *,
    timeout: float,
) -> Any:
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    connector = aiohttp.TCPConnector(force_close=True)
    async with aiohttp.ClientSession(timeout=client_timeout, connector=connector) as session:
        return await fetch_abci_query(session, rpc_url, path, timeout=timeout)


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
                bds_node=bool(node.get("bds_enabled")),
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
                int(payload["result"]["sync_info"]["latest_block_height"]) for payload in statuses
            ]
            if all(height > 0 for height in heights):
                return statuses
        await asyncio.sleep(1.0)
    raise RunnerError("localnet nodes did not become ready in time")


async def latest_height(session: aiohttp.ClientSession, rpc_url: str) -> int:
    del session
    payload = await fetch_json_fresh(f"{rpc_url}/status", timeout=5.0)
    return int(payload["result"]["sync_info"]["latest_block_height"])


async def wait_for_height(
    session: aiohttp.ClientSession,
    rpc_url: str,
    target_height: int,
    *,
    timeout_seconds: float,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            height = await latest_height(session, rpc_url)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.5)
            continue
        if height >= target_height:
            return height
        await asyncio.sleep(0.5)
    raise RunnerError(
        f"node {rpc_url} did not reach height {target_height}; last_error={last_error}"
    )


async def wait_for_abci_query_responsive(
    session: aiohttp.ClientSession,
    rpc_url: str,
    *,
    timeout_seconds: float,
    probe_timeout: float = 2.0,
) -> None:
    del session
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            await asyncio.wait_for(
                fetch_abci_query_fresh(
                    rpc_url,
                    ABCI_HEALTH_QUERY_PATH,
                    timeout=probe_timeout,
                ),
                timeout=probe_timeout + 0.5,
            )
            return
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.5)
    raise RunnerError(f"node {rpc_url} did not answer ABCI queries; last={last_error}")


async def wait_for_container_state(
    container_name: str,
    *,
    expected_states: set[str],
    timeout_seconds: float,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_state: str | None = None
    while time.monotonic() < deadline:
        result = run_cmd(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                container_name,
            ],
            cwd=STACK_DIR,
        )
        state_status, _, health_status = result.stdout.strip().partition("|")
        candidates = [state_status.strip(), health_status.strip()]
        last_state = next((state for state in candidates if state), None)
        for candidate in candidates:
            if candidate in expected_states:
                return candidate
        await asyncio.sleep(1.0)

    raise RunnerError(
        f"container {container_name} did not reach state {sorted(expected_states)}; "
        f"last={last_state!r}"
    )


def normalize_receipt(submission, *, label: str) -> dict[str, Any]:
    execution = submission.receipt.execution if submission.receipt else None
    state = []
    events = []
    if isinstance(execution, dict):
        state = execution.get("state", []) or []
        events = execution.get("events", []) or []
    success = None if submission.receipt is None else submission.receipt.success
    message = submission.message if submission.receipt is None else submission.receipt.message
    if submission.receipt is None and submission.mode == "commit":
        success = bool(submission.accepted and submission.finalized)
        if success and message is None:
            message = "Transaction committed"
    return {
        "label": label,
        "submitted": submission.submitted,
        "accepted": submission.accepted,
        "finalized": submission.finalized,
        "success": success,
        "message": message,
        "tx_hash": submission.tx_hash,
        "nonce": submission.nonce,
        "chi_supplied": submission.chi_supplied,
        "chi_used": None if execution is None else execution.get("chi_used"),
        "state_write_count": len(state),
        "event_count": len(events),
        "events": events,
    }


def submission_error_context(submission) -> str:
    parts = []
    if submission.message:
        parts.append(f"message={submission.message!r}")
    response = submission.response or {}
    if response:
        parts.append("response=" + json.dumps(response, sort_keys=True, default=str)[:500])
    return "" if not parts else " (" + "; ".join(parts) + ")"


def ensure_positive_submission(submission, *, label: str) -> dict[str, Any]:
    if not submission.submitted:
        raise RunnerError(
            f"{label}: transaction was not submitted{submission_error_context(submission)}"
        )
    if submission.accepted is False:
        raise RunnerError(
            f"{label}: CheckTx rejected: {submission.message}{submission_error_context(submission)}"
        )
    if submission.receipt is None:
        if submission.mode == "commit" and submission.accepted is True and submission.finalized:
            return normalize_receipt(submission, label=label)
        raise RunnerError(f"{label}: receipt missing")
    if submission.receipt.success is not True:
        raise RunnerError(
            f"{label}: transaction failed during execution: {submission.receipt.message}"
        )
    return normalize_receipt(submission, label=label)


def ensure_failed_submission(
    submission,
    *,
    label: str,
    message_contains: str | None = None,
) -> dict[str, Any]:
    if not submission.submitted:
        raise RunnerError(
            f"{label}: transaction was not submitted{submission_error_context(submission)}"
        )
    if submission.receipt is None:
        raise RunnerError(f"{label}: receipt missing for expected failure")
    if submission.receipt.success is not False:
        raise RunnerError(f"{label}: expected transaction failure, got success")

    receipt = normalize_receipt(submission, label=label)
    if message_contains is not None:
        message = receipt["message"] or ""
        if message_contains not in message:
            raise RunnerError(
                f"{label}: expected failure containing {message_contains!r}, got {message!r}"
            )
    return receipt


def assert_equal(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise RunnerError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(value: Any, *, label: str) -> None:
    if not value:
        raise RunnerError(f"{label}: expected truthy value, got {value!r}")


class ValidatorGovernanceRunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.output_dir = OUTPUT_ROOT / self.run_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.network: dict[str, Any] | None = None
        self.nodes: list[LocalnetNode] = []
        self.founder_wallet: Wallet | None = None
        self.validator_wallets: list[Wallet] = []
        self.delegator_wallet: Wallet = derive_wallet(args.seed, "validator-delegator")
        self.probe_contract: str | None = None

    def client(
        self,
        wallet: Wallet,
        node_index: int,
        session: aiohttp.ClientSession,
    ) -> XianAsync:
        if self.network is None:
            raise RunnerError("network is not initialized")
        return XianAsync(
            node_url=self.nodes[node_index].rpc_url,
            chain_id=self.network["chain_id"],
            wallet=wallet,
            config=self.client_config(),
            session=session,
        )

    @functools.lru_cache(maxsize=1)
    def client_config(self) -> XianClientConfig:
        return XianClientConfig(
            retry=RetryPolicy(
                max_attempts=6,
                initial_delay_seconds=0.25,
                max_delay_seconds=4.0,
                backoff_multiplier=2.0,
                retry_transport_errors=True,
                retry_rpc_errors=True,
            ),
            submission=SubmissionConfig(
                timeout_seconds=self.args.rpc_timeout_seconds,
                poll_interval_seconds=0.5,
            ),
        )

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        path = self.output_dir / f"{name}.json"
        path.write_text(
            json.dumps(normalize_value(payload), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    async def bootstrap(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        env = make_localnet_env(self.args)
        outputs: dict[str, Any] = {
            "env": {
                "XIAN_LOCALNET_GENESIS_NETWORK": env["XIAN_LOCALNET_GENESIS_NETWORK"],
                "XIAN_LOCALNET_PORT_OFFSET": env["XIAN_LOCALNET_PORT_OFFSET"],
                "XIAN_LOCALNET_TOPOLOGY": env["XIAN_LOCALNET_TOPOLOGY"],
                "LOCALNET_NODES": env["LOCALNET_NODES"],
            }
        }

        if self.args.bootstrap:
            outputs["localnet_down"] = run_make("localnet-down", env=env).stdout
            outputs["localnet_init"] = run_make("localnet-init", env=env).stdout
            image_tag = LOCALNET_IMAGE_BY_TOPOLOGY[self.args.topology]
            if self.args.build or not image_available(image_tag):
                outputs["localnet_build"] = run_make("localnet-build", env=env).stdout
            outputs["localnet_up"] = run_make("localnet-up", env=env).stdout

        self.network = load_network()
        self.nodes = build_nodes(self.network)
        self.founder_wallet = Wallet(private_key=self.network["founder_key"])
        self.validator_wallets = [
            Wallet(private_key=node.account_private_key) for node in self.nodes
        ]
        if self.network.get("genesis_network") != self.args.genesis_network:
            raise RunnerError(
                "loaded localnet genesis network "
                f"{self.network.get('genesis_network')!r} does not match "
                f"requested {self.args.genesis_network!r}"
            )
        if len(self.nodes) != DEFAULT_LOCALNET_NODES:
            raise RunnerError(
                f"this runner expects exactly {DEFAULT_LOCALNET_NODES} validators, "
                f"got {len(self.nodes)}"
            )
        await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=self.args.rpc_timeout_seconds,
        )
        self.write_json("network", self.network)
        outputs["network"] = {
            "chain_id": self.network["chain_id"],
            "genesis_network": self.network.get("genesis_network"),
            "node_count": len(self.nodes),
        }
        return outputs

    async def restart_localnet(self, session: aiohttp.ClientSession) -> None:
        env = make_localnet_env(self.args)
        run_make("localnet-down", env=env)
        run_make("localnet-up", env=env)
        await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=self.args.rpc_timeout_seconds,
        )

    @staticmethod
    def node_container_names(node: LocalnetNode) -> list[str]:
        ordered = [node.abci_container, node.cometbft_container]
        unique: list[str] = []
        for name in ordered:
            if name and name not in unique:
                unique.append(name)
        return unique

    async def stop_node_runtime(self, node: LocalnetNode) -> dict[str, Any]:
        container_names = self.node_container_names(node)
        if not container_names:
            raise RunnerError(f"node {node.moniker} has no containers to stop")
        run_cmd(["docker", "stop", *container_names], cwd=STACK_DIR)
        states: dict[str, str] = {}
        for container_name in container_names:
            states[container_name] = await wait_for_container_state(
                container_name,
                expected_states={"exited"},
                timeout_seconds=30.0,
            )
        return {"containers": container_names, "states": states}

    async def start_node_runtime(
        self,
        session: aiohttp.ClientSession,
        node: LocalnetNode,
        *,
        target_height: int | None = None,
    ) -> dict[str, Any]:
        container_names = self.node_container_names(node)
        if not container_names:
            raise RunnerError(f"node {node.moniker} has no containers to start")
        run_cmd(["docker", "start", *container_names], cwd=STACK_DIR)
        states: dict[str, str] = {}
        for container_name in container_names:
            states[container_name] = await wait_for_container_state(
                container_name,
                expected_states={"running", "healthy"},
                timeout_seconds=45.0,
            )
        ready_height = await wait_for_height(
            session,
            node.rpc_url,
            max(target_height or 1, 1),
            timeout_seconds=90.0,
        )
        await wait_for_abci_query_responsive(
            session,
            node.rpc_url,
            timeout_seconds=90.0,
        )
        return {
            "containers": container_names,
            "states": states,
            "ready_height": ready_height,
            "abci_query_ready": True,
        }

    async def restart_node_runtime(
        self,
        session: aiohttp.ClientSession,
        node: LocalnetNode,
        *,
        target_height: int | None = None,
    ) -> dict[str, Any]:
        stop = await self.stop_node_runtime(node)
        start = await self.start_node_runtime(
            session,
            node,
            target_height=target_height,
        )
        return {
            "node": node.moniker,
            "stop": stop,
            "start": start,
        }

    async def recover_lagging_nodes(
        self,
        session: aiohttp.ClientSession,
        *,
        target_height: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        before: dict[str, Any] = {}
        lagging: list[tuple[LocalnetNode, str]] = []
        for node in self.nodes:
            try:
                height = await wait_for_height(
                    session,
                    node.rpc_url,
                    target_height,
                    timeout_seconds=timeout_seconds,
                )
                await wait_for_abci_query_responsive(
                    session,
                    node.rpc_url,
                    timeout_seconds=timeout_seconds,
                )
                before[node.moniker] = {
                    "height": height,
                    "ok": True,
                    "abci_query_ready": True,
                }
            except Exception as exc:  # noqa: BLE001
                before[node.moniker] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                lagging.append((node, f"{type(exc).__name__}: {exc}"))

        restarts = []
        for node, error in lagging:
            restart = await self.restart_node_runtime(
                session,
                node,
                target_height=target_height,
            )
            restart["reason"] = error
            restarts.append(restart)

        after: dict[str, Any] = {}
        if restarts:
            for node in self.nodes:
                height = await wait_for_height(
                    session,
                    node.rpc_url,
                    target_height,
                    timeout_seconds=timeout_seconds,
                )
                await wait_for_abci_query_responsive(
                    session,
                    node.rpc_url,
                    timeout_seconds=timeout_seconds,
                )
                after[node.moniker] = {
                    "height": height,
                    "ok": True,
                    "abci_query_ready": True,
                }

        return {
            "target_height": target_height,
            "before": before,
            "restarts": restarts,
            "after": after,
        }

    async def recover_current_height(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        reference_node = self.nodes[1] if len(self.nodes) > 1 else self.nodes[0]
        return await self.recover_lagging_nodes(
            session,
            target_height=await latest_height(session, reference_node.rpc_url),
            timeout_seconds=timeout_seconds,
        )

    async def fund_wallets(
        self,
        session: aiohttp.ClientSession,
        wallets: list[Wallet],
        *,
        amount: int,
    ) -> list[dict[str, Any]]:
        if self.founder_wallet is None:
            raise RunnerError("founder wallet is not initialized")
        receipts = []
        funding_node_index = 1 if len(self.nodes) > 1 else 0
        async with self.client(self.founder_wallet, funding_node_index, session) as client:
            for wallet in wallets:
                submission = await client.send(
                    amount=amount,
                    to_address=wallet.public_key,
                    chi=DEFAULT_TRANSFER_CHI,
                    wait_for_tx=True,
                )
                receipts.append(
                    ensure_positive_submission(
                        submission,
                        label=f"fund-{wallet.public_key[:12]}",
                    )
                )
        return receipts

    async def health(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        node_statuses = []
        for node in self.nodes:
            status = await fetch_json(session, f"{node.rpc_url}/status", timeout=5.0)
            validators = await fetch_json(session, f"{node.rpc_url}/validators", timeout=5.0)
            node_statuses.append(
                {
                    "moniker": node.moniker,
                    "height": int(status["result"]["sync_info"]["latest_block_height"]),
                    "validator_count": len(validators["result"]["validators"]),
                }
            )

        async with self.client(self.validator_wallets[0], 0, session) as node0:
            policy = await node0.call("masternodes", "get_policy_config", {})
            active_validators = await node0.call("masternodes", "get_active_validators", {})
            governance_members = await node0.call("governance", "get_members", {})

        assert_equal(
            len(active_validators),
            DEFAULT_LOCALNET_NODES,
            label="initial active validator count",
        )
        assert_equal(policy["selection_mode"], "manual", label="initial selection mode")
        assert_equal(
            len(governance_members),
            DEFAULT_LOCALNET_NODES,
            label="initial governance members",
        )

        return {
            "nodes": node_statuses,
            "policy": policy,
            "active_validators": active_validators,
            "governance_members": governance_members,
        }

    async def submit_tx(
        self,
        client: XianAsync,
        contract: str,
        function: str,
        kwargs: dict[str, Any],
        *,
        label: str,
        chi: int = DEFAULT_TX_CHI,
    ) -> dict[str, Any]:
        submission = await client.send_tx(
            contract,
            function,
            kwargs,
            chi=chi,
            wait_for_tx=True,
        )
        return ensure_positive_submission(submission, label=label)

    async def wait_for_governance_proposal_status(
        self,
        client: XianAsync,
        proposal_id: int,
        *,
        expected_status: str,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_proposal = None
        while time.monotonic() < deadline:
            proposal = await client.call(
                "governance",
                "get_proposal",
                {"proposal_id": proposal_id},
            )
            last_proposal = proposal
            if proposal["status"] == expected_status:
                return proposal
            await asyncio.sleep(0.5)
        raise RunnerError(
            f"governance proposal {proposal_id} did not reach {expected_status!r}; "
            f"last={last_proposal}"
        )

    async def wait_for_members_vote_status(
        self,
        client: XianAsync,
        proposal_id: int,
        *,
        expected_status: str,
        timeout_seconds: float = 15.0,
        fallback_clients: list[XianAsync] | None = None,
    ) -> dict[str, Any]:
        readers = [client, *(fallback_clients or [])]
        deadline = time.monotonic() + timeout_seconds
        last_vote = None
        while time.monotonic() < deadline:
            proposal = await self.read_members_vote(
                readers,
                proposal_id,
                timeout_seconds=min(5.0, max(deadline - time.monotonic(), 0.1)),
            )
            last_vote = proposal
            if proposal["status"] == expected_status:
                return proposal
            await asyncio.sleep(0.5)
        raise RunnerError(
            f"members vote {proposal_id} did not reach {expected_status!r}; last={last_vote}"
        )

    async def wait_for_members_vote_progress(
        self,
        client: XianAsync,
        proposal_id: int,
        *,
        previous_vote: dict[str, Any],
        timeout_seconds: float = 5.0,
        fallback_clients: list[XianAsync] | None = None,
    ) -> dict[str, Any]:
        readers = [client, *(fallback_clients or [])]
        deadline = time.monotonic() + timeout_seconds
        last_vote = previous_vote
        previous_status = previous_vote.get("status")
        previous_yes = coerce_int(previous_vote.get("yes", 0) or 0)
        previous_no = coerce_int(previous_vote.get("no", 0) or 0)
        previous_voters = tuple(previous_vote.get("voters") or [])

        while time.monotonic() < deadline:
            proposal = await self.read_members_vote(
                readers,
                proposal_id,
                timeout_seconds=min(5.0, max(deadline - time.monotonic(), 0.1)),
            )
            last_vote = proposal
            if (
                proposal.get("status") != previous_status
                or coerce_int(proposal.get("yes", 0) or 0) != previous_yes
                or coerce_int(proposal.get("no", 0) or 0) != previous_no
                or tuple(proposal.get("voters") or []) != previous_voters
            ):
                return proposal
            await asyncio.sleep(0.25)

        return last_vote

    async def read_members_vote(
        self,
        clients: list[XianAsync],
        proposal_id: int,
        *,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        return await self.read_contract_state(
            clients,
            "masternodes",
            "votes",
            proposal_id,
            timeout_seconds=timeout_seconds,
        )

    async def read_contract_state(
        self,
        clients: list[XianAsync],
        contract: str,
        variable: str,
        *keys: Any,
        timeout_seconds: float = 5.0,
    ) -> Any:
        last_error: str | None = None
        for client in clients:
            try:
                return await asyncio.wait_for(
                    client.get_state(contract, variable, *keys),
                    timeout=timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
        raise RunnerError(
            f"{contract}.{variable} could not be read from any node; last={last_error}"
        )

    async def approve_governance_contract_call(
        self,
        proposer: XianAsync,
        voters: list[tuple[str, XianAsync]],
        *,
        target_contract: str,
        target_function: str,
        kwargs: dict[str, Any],
        summary: str,
        label_prefix: str,
    ) -> dict[str, Any]:
        proposal_receipt = await self.submit_tx(
            proposer,
            "governance",
            "propose_contract_call",
            {
                "target_contract": target_contract,
                "target_function": target_function,
                "kwargs": kwargs,
                "summary": summary,
            },
            label=f"{label_prefix}-propose",
            chi=GOVERNANCE_TX_CHI,
        )
        proposal_id = coerce_int(await proposer.get_state("governance", "proposal_count"))
        proposal_pending = await proposer.call(
            "governance",
            "get_proposal",
            {"proposal_id": proposal_id},
        )
        assert_equal(
            proposal_pending["status"],
            "pending",
            label=f"{label_prefix} pending status",
        )

        vote_receipts = []
        proposal_final = None
        for index, (name, voter) in enumerate(voters, start=1):
            current_proposal = await proposer.call(
                "governance",
                "get_proposal",
                {"proposal_id": proposal_id},
            )
            if current_proposal["status"] == "executed":
                proposal_final = current_proposal
                break
            vote_receipts.append(
                await self.submit_tx(
                    voter,
                    "governance",
                    "vote",
                    {"proposal_id": proposal_id, "support": True},
                    label=f"{label_prefix}-vote-{index}-{name}",
                    chi=GOVERNANCE_TX_CHI,
                )
            )
            current_proposal = await proposer.call(
                "governance",
                "get_proposal",
                {"proposal_id": proposal_id},
            )
            if current_proposal["status"] == "executed":
                proposal_final = current_proposal
                break

        if proposal_final is None:
            proposal_final = await self.wait_for_governance_proposal_status(
                proposer,
                proposal_id,
                expected_status="executed",
            )
        return {
            "proposal_id": proposal_id,
            "proposal_receipt": proposal_receipt,
            "proposal_pending": proposal_pending,
            "vote_receipts": vote_receipts,
            "proposal_final": proposal_final,
        }

    async def approve_members_vote(
        self,
        proposer: XianAsync,
        voters: list[tuple[str, XianAsync]],
        *,
        type_of_vote: str,
        arg: Any,
        label_prefix: str,
    ) -> dict[str, Any]:
        status_readers = [voter for _name, voter in voters]
        proposal_receipt = await self.submit_tx(
            proposer,
            "masternodes",
            "propose_vote",
            {"type_of_vote": type_of_vote, "arg": arg},
            label=f"{label_prefix}-propose",
            chi=GOVERNANCE_TX_CHI,
        )
        proposal_id = coerce_int(
            await self.read_contract_state(
                [proposer, *status_readers],
                "masternodes",
                "total_votes",
            )
        )
        proposal_pending = await self.read_members_vote(
            [proposer, *status_readers],
            proposal_id,
        )
        assert_equal(
            proposal_pending["status"],
            "pending",
            label=f"{label_prefix} pending status",
        )

        vote_receipts = []
        proposal_final = None
        for index, (name, voter) in enumerate(voters, start=1):
            current_vote = await self.read_members_vote(
                [proposer, *status_readers],
                proposal_id,
            )
            if current_vote["status"] == "approved":
                proposal_final = current_vote
                break
            vote_receipts.append(
                await self.submit_tx(
                    voter,
                    "masternodes",
                    "vote",
                    {"proposal_id": proposal_id, "vote": "yes"},
                    label=f"{label_prefix}-vote-{index}-{name}",
                    chi=GOVERNANCE_TX_CHI,
                )
            )
            current_vote = await self.wait_for_members_vote_progress(
                proposer,
                proposal_id,
                previous_vote=current_vote,
                fallback_clients=status_readers,
            )
            if current_vote["status"] == "approved":
                proposal_final = current_vote
                break

        if proposal_final is None:
            proposal_final = await self.wait_for_members_vote_status(
                proposer,
                proposal_id,
                expected_status="approved",
                fallback_clients=status_readers,
            )
        return {
            "proposal_id": proposal_id,
            "proposal_receipt": proposal_receipt,
            "proposal_pending": proposal_pending,
            "vote_receipts": vote_receipts,
            "proposal_final": proposal_final,
        }

    async def wait_for_validator_count(
        self,
        session: aiohttp.ClientSession,
        *,
        expected_count: int,
        timeout_seconds: float = 45.0,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            snapshots = []
            for node in self.nodes:
                validators = await fetch_json(session, f"{node.rpc_url}/validators", timeout=5.0)
                entries = validators["result"]["validators"]
                snapshots.append(
                    {
                        "moniker": node.moniker,
                        "count": len(entries),
                        "powers": sorted(int(entry["voting_power"]) for entry in entries),
                    }
                )
            if all(snapshot["count"] == expected_count for snapshot in snapshots):
                return snapshots
            await asyncio.sleep(1.0)
        raise RunnerError(f"live validator count did not converge to {expected_count}")

    async def wait_for_live_validator_address(
        self,
        session: aiohttp.ClientSession,
        *,
        rpc_url: str,
        validator_address: str,
        expected_present: bool,
        timeout_seconds: float = 45.0,
        label: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_snapshot = None
        while time.monotonic() < deadline:
            payload = await fetch_json(
                session,
                f"{rpc_url}/validators",
                timeout=5.0,
                params={"per_page": "100"},
            )
            entries = payload["result"]["validators"]
            current_addresses = [entry["address"] for entry in entries]
            last_snapshot = {
                "block_height": payload["result"]["block_height"],
                "addresses": current_addresses,
                "count": len(entries),
            }
            if (validator_address in current_addresses) == expected_present:
                return last_snapshot
            await asyncio.sleep(1.0)
        raise RunnerError(
            f"{label}: validator address presence did not reach {expected_present}; "
            f"last={last_snapshot}"
        )

    async def wait_for_validator_record(
        self,
        client: XianAsync,
        account: str,
        *,
        predicate,
        timeout_seconds: float = 30.0,
        label: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_record = None
        while time.monotonic() < deadline:
            last_record = await client.call(
                "masternodes",
                "get_validator",
                {"account": account},
            )
            if predicate(last_record):
                return last_record
            await asyncio.sleep(0.5)
        raise RunnerError(f"{label}: validator predicate not reached; last={last_record}")

    async def wait_for_active_validators(
        self,
        client: XianAsync,
        *,
        expected_accounts: list[str],
        timeout_seconds: float = 30.0,
        label: str,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_seconds
        last_active = None
        while time.monotonic() < deadline:
            last_active = await client.call(
                "masternodes",
                "get_active_validators",
                {},
            )
            current_accounts = [entry["account"] for entry in last_active]
            if current_accounts == expected_accounts:
                return last_active
            await asyncio.sleep(0.5)
        raise RunnerError(
            f"{label}: active validator set did not converge to {expected_accounts!r}; "
            f"last={last_active}"
        )

    def priv_validator_key_path(self, node_index: int) -> Path:
        return (
            STACK_DIR
            / ".localnet"
            / f"node-{node_index}"
            / ".cometbft"
            / "config"
            / "priv_validator_key.json"
        )

    def load_priv_validator_key(self, node_index: int) -> dict[str, Any]:
        return json.loads(self.priv_validator_key_path(node_index).read_text(encoding="utf-8"))

    async def broadcast_duplicate_vote_evidence(
        self,
        session: aiohttp.ClientSession,
        *,
        reporter_node_index: int,
        target_node_index: int,
        height_lookback: int = 30,
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        reporter = self.nodes[reporter_node_index]
        target = self.nodes[target_node_index]
        target_priv_validator = self.load_priv_validator_key(target_node_index)
        target_consensus_address = target_priv_validator["address"]
        target_signing_key = nacl.signing.SigningKey(
            base64.b64decode(target_priv_validator["priv_key"]["value"])[:32]
        )

        chosen_height = None
        block_result = None
        validators_result = None
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and chosen_height is None:
            latest = await latest_height(session, reporter.rpc_url)
            for offset in range(0, height_lookback + 1):
                candidate_height = latest - offset
                if candidate_height <= 0:
                    break
                block_candidate = await fetch_json(
                    session,
                    f"{reporter.rpc_url}/block",
                    timeout=5.0,
                    params={"height": str(candidate_height)},
                )
                validators_candidate = await fetch_json(
                    session,
                    f"{reporter.rpc_url}/validators",
                    timeout=5.0,
                    params={"height": str(candidate_height), "per_page": "100"},
                )
                validator_entries = validators_candidate["result"]["validators"]
                if any(entry["address"] == target_consensus_address for entry in validator_entries):
                    chosen_height = candidate_height
                    block_result = block_candidate["result"]
                    validators_result = validators_candidate["result"]
                    break
            if chosen_height is None:
                await asyncio.sleep(1.0)

        if chosen_height is None or block_result is None or validators_result is None:
            raise RunnerError(
                f"could not find validator {target_consensus_address} in the last "
                f"{height_lookback} heights after waiting {timeout_seconds} seconds"
            )

        validator_entries = validators_result["validators"]
        target_validator_index = next(
            index
            for index, entry in enumerate(validator_entries)
            if entry["address"] == target_consensus_address
        )
        validator_power = int(validator_entries[target_validator_index]["voting_power"])
        total_voting_power = sum(int(entry["voting_power"]) for entry in validator_entries)
        block_id = block_result["block_id"]
        block_header = block_result["block"]["header"]
        evidence_timestamp = block_header["time"]
        chain_id = block_header["chain_id"]
        parts_total = int(block_id["parts"]["total"])
        real_block_hash = block_id["hash"]
        real_parts_hash = block_id["parts"]["hash"]
        conflicting_block_hash = "11" * 32
        conflicting_parts_hash = "22" * 32

        def make_vote(block_hash: str, parts_hash: str) -> dict[str, Any]:
            canonical_vote = canonical_pb2.CanonicalVote(
                type=2,
                height=chosen_height,
                round=0,
                block_id=canonical_pb2.CanonicalBlockID(
                    hash=bytes.fromhex(block_hash),
                    part_set_header=canonical_pb2.CanonicalPartSetHeader(
                        total=parts_total,
                        hash=bytes.fromhex(parts_hash),
                    ),
                ),
                chain_id=chain_id,
                timestamp=protobuf_timestamp(evidence_timestamp),
            )
            sign_doc = canonical_vote.SerializeToString()
            sign_bytes = encode_uvarint(len(sign_doc)) + sign_doc
            signature = target_signing_key.sign(sign_bytes).signature
            return {
                "type": 2,
                "height": str(chosen_height),
                "round": 0,
                "block_id": {
                    "hash": block_hash,
                    "parts": {"total": parts_total, "hash": parts_hash},
                },
                "timestamp": evidence_timestamp,
                "validator_address": target_consensus_address,
                "validator_index": target_validator_index,
                "signature": base64.b64encode(signature).decode("utf-8"),
                "extension": None,
                "extension_signature": None,
            }

        vote_real = make_vote(real_block_hash, real_parts_hash)
        vote_conflicting = make_vote(conflicting_block_hash, conflicting_parts_hash)
        if block_id_sort_key(
            vote_real["block_id"]["hash"],
            vote_real["block_id"]["parts"]["total"],
            vote_real["block_id"]["parts"]["hash"],
        ) < block_id_sort_key(
            vote_conflicting["block_id"]["hash"],
            vote_conflicting["block_id"]["parts"]["total"],
            vote_conflicting["block_id"]["parts"]["hash"],
        ):
            vote_a = vote_real
            vote_b = vote_conflicting
        else:
            vote_a = vote_conflicting
            vote_b = vote_real

        evidence = {
            "type": "tendermint/DuplicateVoteEvidence",
            "value": {
                "vote_a": vote_a,
                "vote_b": vote_b,
                "TotalVotingPower": str(total_voting_power),
                "ValidatorPower": str(validator_power),
                "Timestamp": evidence_timestamp,
            },
        }

        async with session.get(
            f"{reporter.rpc_url}/broadcast_evidence",
            params={"evidence": json.dumps(evidence, separators=(",", ":"))},
            timeout=10.0,
        ) as response:
            payload = await response.json()
            if response.status >= 400:
                raise RunnerError(f"broadcast_evidence failed: {payload}")

        return {
            "reporter_node": reporter.moniker,
            "target_node": target.moniker,
            "target_account": target.account_public_key,
            "target_consensus_address": target_consensus_address,
            "height": chosen_height,
            "chain_id": chain_id,
            "evidence_timestamp": evidence_timestamp,
            "validator_index": target_validator_index,
            "validator_power": validator_power,
            "total_voting_power": total_voting_power,
            "block_id": block_id,
            "conflicting_block_id": {
                "hash": conflicting_block_hash.upper(),
                "parts": {"total": parts_total, "hash": conflicting_parts_hash.upper()},
            },
            "evidence": evidence,
            "response": payload,
        }

    async def generic_governance_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        (
            node0_wallet,
            node1_wallet,
            node2_wallet,
            node3_wallet,
            node4_wallet,
        ) = self.validator_wallets
        funding = await self.fund_wallets(
            session,
            [
                node0_wallet,
                node1_wallet,
                node2_wallet,
                node3_wallet,
                node4_wallet,
            ],
            amount=2_000_000,
        )
        async with (
            self.client(node0_wallet, 0, session) as node0,
            self.client(node1_wallet, 1, session) as node1,
            self.client(node2_wallet, 2, session) as node2,
            self.client(node3_wallet, 3, session) as node3,
            self.client(node4_wallet, 4, session) as node4,
        ):
            probe_contract = f"con_governance_probe_{short_hash(self.run_id)}"
            probe_code = """
value = Variable()
mode = Variable()
patch_count = Variable()

@construct
def seed():
    value.set(0)
    mode.set("live")
    patch_count.set(0)

@export
def set_value(new_value: int):
    value.set(new_value)

@export
def get_value():
    return value.get()

@export
def get_status():
    return {
        "value": value.get(),
        "mode": mode.get(),
        "patch_count": patch_count.get(),
    }
""".strip()
            deploy_submission = await node0.submit_contract(
                name=probe_contract,
                deployment_artifacts=build_deployment_artifacts(
                    probe_contract,
                    probe_code,
                ),
                chi=GOVERNANCE_TX_CHI,
                wait_for_tx=True,
            )
            deploy_receipt = ensure_positive_submission(
                deploy_submission,
                label="governance-probe-deploy",
            )
            original_value = coerce_int(await node0.get_state(probe_contract, "value"))
            next_value = 42 if original_value != 42 else 43
            approval = await self.approve_governance_contract_call(
                node0,
                [
                    ("node1", node1),
                    ("node2", node2),
                    ("node3", node3),
                    ("node4", node4),
                ],
                target_contract=probe_contract,
                target_function="set_value",
                kwargs={"new_value": next_value},
                summary="validator-governance script probe contract update",
                label_prefix="governance-contract-call",
            )
            updated_value = coerce_int(await node0.get_state(probe_contract, "value"))
            assert_equal(updated_value, next_value, label="updated governance probe value")
            self.probe_contract = probe_contract

        return {
            "funding": funding,
            "probe_contract": probe_contract,
            "deploy": deploy_receipt,
            "original_value": original_value,
            "updated_value": updated_value,
            "approval": approval,
        }

    async def state_patch_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        if self.network is None:
            raise RunnerError("network is not initialized")
        if self.probe_contract is None:
            raise RunnerError("governance probe contract is not initialized")

        current_height = await latest_height(session, self.nodes[0].rpc_url)
        governance_min_patch_delay = await fetch_abci_query(
            session,
            self.nodes[0].rpc_url,
            "/get/governance.metadata:min_patch_delay_blocks",
        )
        if governance_min_patch_delay is None:
            governance_min_patch_delay = STATE_PATCH_DELAY_BLOCKS
        activation_height = current_height + max(
            int(governance_min_patch_delay) + STATE_PATCH_ACTIVATION_HEADROOM_BLOCKS,
            STATE_PATCH_DELAY_BLOCKS,
        )
        patch_id = f"localnet-validator-governance-{short_hash(self.run_id)}"
        bundle_payload = {
            "version": 1,
            "patch_id": patch_id,
            "activation_height": activation_height,
            "governance_contract": "governance",
            "summary": "Validator governance localnet state patch exercise",
            "uri": "local://localnet-validator-governance",
            "chain_id": self.network["chain_id"],
            "changes": [
                {
                    "key": f"{self.probe_contract}.mode",
                    "value": "patched",
                    "comment": "switch governance probe into patched mode",
                },
                {
                    "key": f"{self.probe_contract}.patch_count",
                    "value": 1,
                    "comment": "record a single applied patch",
                },
            ],
        }
        bundle_payload["bundle_hash"] = compute_patch_bundle_hash(bundle_payload)

        for node in self.nodes:
            patch_dir = (
                STACK_DIR / ".localnet" / node.moniker / ".cometbft" / "config" / "state-patches"
            )
            patch_dir.mkdir(parents=True, exist_ok=True)
            (patch_dir / f"{patch_id}.json").write_text(
                json.dumps(bundle_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        await self.restart_localnet(session)
        restart_height = await latest_height(session, self.nodes[0].rpc_url)
        await wait_for_height(
            session,
            self.nodes[0].rpc_url,
            restart_height + 1,
            timeout_seconds=30.0,
        )

        (
            node0_wallet,
            node1_wallet,
            node2_wallet,
            node3_wallet,
            _node4_wallet,
        ) = self.validator_wallets
        # Route state-patch votes through one healthy RPC while preserving signer identity.
        # A validator can briefly lag during patch activation; waiting on that same node's
        # RPC makes the harness hang even when the transaction finalizes on the network.
        async with (
            self.client(node0_wallet, 0, session) as node0,
            self.client(node1_wallet, 0, session) as node1,
            self.client(node2_wallet, 0, session) as node2,
            self.client(node3_wallet, 0, session) as node3,
        ):
            for client in (node0, node1, node2, node3):
                await client.refresh_nonce()
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
                chi=GOVERNANCE_TX_CHI,
                wait_for_tx=True,
            )
            proposal_receipt = ensure_positive_submission(
                proposal,
                label="state-patch-propose",
            )
            proposal_id = coerce_int(await node0.get_state("governance", "proposal_count"))
            vote_receipts = []
            for client, label in (
                (node1, "state-patch-vote-1"),
                (node2, "state-patch-vote-2"),
                (node3, "state-patch-vote-3"),
            ):
                submission = await client.send_tx(
                    "governance",
                    "vote",
                    {"proposal_id": proposal_id, "support": True},
                    chi=GOVERNANCE_TX_CHI,
                    wait_for_tx=True,
                )
                vote_receipts.append(ensure_positive_submission(submission, label=label))
            proposal_final = await self.wait_for_governance_proposal_status(
                node0,
                proposal_id,
                expected_status="approved",
            )

        current_height = await latest_height(session, self.nodes[0].rpc_url)
        activation_wait_timeout = max(
            120.0,
            float(max((activation_height + 1) - current_height, 1) * 8),
        )
        await wait_for_height(
            session,
            self.nodes[0].rpc_url,
            activation_height + 1,
            timeout_seconds=activation_wait_timeout,
        )
        node_recovery = await self.recover_lagging_nodes(
            session,
            target_height=activation_height + 1,
            timeout_seconds=15.0,
        )

        async with self.client(node0_wallet, 0, session) as node0:
            patch_status = await node0.call(
                "governance",
                "get_patch",
                {"patch_id": patch_id},
            )
            probe_status = await node0.call(
                self.probe_contract,
                "get_status",
                {},
            )

        local_bundles = await fetch_abci_query(
            session,
            self.nodes[0].rpc_url,
            "/state_patch_bundles",
        )
        scheduled_inventory = await fetch_abci_query(
            session,
            self.nodes[0].rpc_url,
            f"/scheduled_state_patches/{activation_height}",
        )

        assert_equal(
            probe_status["mode"],
            "patched",
            label="state patch updated probe mode",
        )
        assert_equal(
            probe_status["patch_count"],
            1,
            label="state patch updated probe patch_count",
        )

        return {
            "bundle": bundle_payload,
            "governance_min_patch_delay": governance_min_patch_delay,
            "activation_wait_timeout_seconds": activation_wait_timeout,
            "node_recovery": node_recovery,
            "proposal_receipt": proposal_receipt,
            "vote_receipts": vote_receipts,
            "proposal_final": proposal_final,
            "governance_patch": patch_status,
            "probe_status": probe_status,
            "local_bundle_inventory": normalize_value(local_bundles),
            "scheduled_inventory": normalize_value(scheduled_inventory),
        }

    async def manual_members_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        (
            node0_wallet,
            node1_wallet,
            node2_wallet,
            node3_wallet,
            node4_wallet,
        ) = self.validator_wallets
        node3_key = self.nodes[3].account_public_key

        await self.fund_wallets(
            session,
            [node1_wallet, node2_wallet, node3_wallet, node4_wallet],
            amount=500_000,
        )

        async with (
            self.client(node0_wallet, 0, session) as node0,
            self.client(node1_wallet, 1, session) as node1,
            self.client(node2_wallet, 2, session) as node2,
            self.client(node3_wallet, 3, session) as node3,
            self.client(node4_wallet, 4, session) as node4,
        ):
            set_power = await self.approve_members_vote(
                node0,
                [
                    ("node1", node1),
                    ("node2", node2),
                    ("node3", node3),
                    ("node4", node4),
                ],
                type_of_vote="set_member_power",
                arg={"member": node3_key, "power": 15},
                label_prefix="manual-set-member-power",
            )
            validator_after_power = await node0.call(
                "masternodes",
                "get_validator",
                {"account": node3_key},
            )
            assert_equal(
                validator_after_power["power"],
                15,
                label="node3 power after set_member_power",
            )

            live_height_target = await latest_height(session, self.nodes[0].rpc_url) + 2
            await wait_for_height(
                session,
                self.nodes[0].rpc_url,
                live_height_target,
                timeout_seconds=30.0,
            )
            live_after_power = await self.wait_for_validator_count(
                session,
                expected_count=5,
            )
            assert_true(
                any(15 in snapshot["powers"] for snapshot in live_after_power),
                label="live validator power updated",
            )

            remove_member = await self.approve_members_vote(
                node0,
                [
                    ("node1", node1),
                    ("node2", node2),
                    ("node3", node3),
                    ("node4", node4),
                ],
                type_of_vote="remove_member",
                arg=node3_key,
                label_prefix="manual-remove-member",
            )
            live_after_remove = await self.wait_for_validator_count(
                session,
                expected_count=4,
            )
            validator_after_remove = await node0.call(
                "masternodes",
                "get_validator",
                {"account": node3_key},
            )
            assert_equal(
                validator_after_remove["status"],
                "removed",
                label="node3 removed status",
            )

            registration_fee = coerce_int(await node0.get_state("masternodes", "registration_fee"))
            reapprove = await self.submit_tx(
                node3,
                "currency",
                "approve",
                {"amount": registration_fee, "to": "masternodes"},
                label="manual-reregister-approve",
            )
            register_receipt = await self.submit_tx(
                node3,
                "masternodes",
                "register",
                {
                    "requested_validator_power": 12,
                    "moniker": "node-3-return",
                    "network_endpoint": "localnet://node-3-return",
                },
                label="manual-reregister",
                chi=GOVERNANCE_TX_CHI,
            )
            update_registration_receipt = await self.submit_tx(
                node3,
                "masternodes",
                "update_registration",
                {
                    "requested_validator_power": 13,
                    "moniker": "node-3-return-updated",
                    "network_endpoint": "localnet://node-3-return-updated",
                },
                label="manual-update-registration",
                chi=GOVERNANCE_TX_CHI,
            )
            validator_pending = await node0.call(
                "masternodes",
                "get_validator",
                {"account": node3_key},
            )
            assert_equal(
                validator_pending["pending_registration"],
                True,
                label="node3 pending registration flag",
            )
            assert_equal(
                validator_pending["requested_power"],
                13,
                label="node3 updated requested power",
            )

            add_member = await self.approve_members_vote(
                node0,
                [("node1", node1), ("node2", node2), ("node4", node4)],
                type_of_vote="add_member",
                arg=node3_key,
                label_prefix="manual-add-member",
            )
            live_after_readd = await self.wait_for_validator_count(
                session,
                expected_count=5,
            )
            validator_after_readd = await node0.call(
                "masternodes",
                "get_validator",
                {"account": node3_key},
            )
            assert_equal(
                validator_after_readd["status"],
                "active",
                label="node3 re-added status",
            )
            assert_equal(
                validator_after_readd["power"],
                13,
                label="node3 power after re-add",
            )

        return {
            "set_member_power": set_power,
            "validator_after_power": validator_after_power,
            "live_after_power": live_after_power,
            "remove_member": remove_member,
            "live_after_remove": live_after_remove,
            "validator_after_remove": validator_after_remove,
            "reapprove": reapprove,
            "register": register_receipt,
            "update_registration": update_registration_receipt,
            "validator_pending": validator_pending,
            "add_member": add_member,
            "live_after_readd": live_after_readd,
            "validator_after_readd": validator_after_readd,
        }

    async def auto_delegation_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        (
            node0_wallet,
            node1_wallet,
            node2_wallet,
            node3_wallet,
            node4_wallet,
        ) = self.validator_wallets
        recovery_reference_node = self.nodes[1] if len(self.nodes) > 1 else self.nodes[0]
        pre_policy_recovery = await self.recover_lagging_nodes(
            session,
            target_height=await latest_height(session, recovery_reference_node.rpc_url),
            timeout_seconds=15.0,
        )
        await self.fund_wallets(
            session,
            [self.delegator_wallet],
            amount=1_000_000,
        )

        async with (
            self.client(node0_wallet, 0, session) as node0,
            self.client(node0_wallet, 1, session) as node0_policy,
            self.client(node1_wallet, 1, session) as node1,
            self.client(node2_wallet, 2, session) as node2,
            self.client(node3_wallet, 3, session) as node3,
            self.client(node4_wallet, 4, session) as node4,
            self.client(self.delegator_wallet, 0, session) as delegator,
        ):
            approvals = []
            for name, client in (
                ("node0", node0),
                ("node1", node1),
                ("node2", node2),
                ("node3", node3),
                ("node4", node4),
            ):
                approvals.append(
                    await self.submit_tx(
                        client,
                        "currency",
                        "approve",
                        {"amount": 2_000, "to": "masternodes"},
                        label=f"bond-approve-{name}",
                    )
                )

            bond_receipts = [
                await self.submit_tx(
                    node0,
                    "masternodes",
                    "bond_self",
                    {"amount": 400},
                    label="bond-self-node0",
                ),
                await self.submit_tx(
                    node1,
                    "masternodes",
                    "bond_self",
                    {"amount": 300},
                    label="bond-self-node1",
                ),
                await self.submit_tx(
                    node2,
                    "masternodes",
                    "bond_self",
                    {"amount": 200},
                    label="bond-self-node2",
                ),
                await self.submit_tx(
                    node3,
                    "masternodes",
                    "bond_self",
                    {"amount": 100},
                    label="bond-self-node3",
                ),
                await self.submit_tx(
                    node4,
                    "masternodes",
                    "bond_self",
                    {"amount": 50},
                    label="bond-self-node4",
                ),
            ]

            # The auto_top_n policy can temporarily stall node-0's ABCI app while
            # node-0 remains in the active validator set. Sign as node-0, but submit
            # through node-1 so the harness can reach the recovery step.
            policy_update = await self.approve_members_vote(
                node0_policy,
                [
                    ("node1", node1),
                    ("node2", node2),
                    ("node3", node3),
                    ("node4", node4),
                ],
                type_of_vote="update_policy",
                arg={
                    "selection_mode": "auto_top_n",
                    "max_validators": 3,
                    "power_mode": "equal",
                    "rebalance_interval": 1,
                    "activation_delay_epochs": 0,
                    "unbonding_period_days": 0,
                    "min_self_bond": 50,
                    "min_total_bond": 50,
                    "max_active_set_churn": 3,
                    "min_bond_margin_bps": 0,
                    "manual_override_enabled": True,
                },
                label_prefix="auto-update-policy",
            )
            policy_recovery = await self.recover_lagging_nodes(
                session,
                target_height=await latest_height(session, self.nodes[1].rpc_url),
                timeout_seconds=15.0,
            )
            await node0.refresh_nonce()
            live_after_policy = await self.wait_for_validator_count(
                session,
                expected_count=3,
            )
            active_after_policy = await node0.call(
                "masternodes",
                "get_active_validators",
                {},
            )
            active_after_policy_accounts = [entry["account"] for entry in active_after_policy]
            assert_equal(
                active_after_policy_accounts,
                [
                    self.nodes[0].account_public_key,
                    self.nodes[1].account_public_key,
                    self.nodes[2].account_public_key,
                ],
                label="active validators after auto_top_n policy",
            )

            delegator_approval = await self.submit_tx(
                delegator,
                "currency",
                "approve",
                {"amount": 500, "to": "masternodes"},
                label="delegator-approve",
            )
            delegate_receipt = await self.submit_tx(
                delegator,
                "masternodes",
                "delegate",
                {
                    "validator": self.nodes[3].account_public_key,
                    "amount": 250,
                    "reward_key": "delegator-reward-key",
                },
                label="delegate-to-node3",
                chi=GOVERNANCE_TX_CHI,
            )
            delegation = await node0.call(
                "masternodes",
                "get_delegation",
                {
                    "delegator": self.delegator_wallet.public_key,
                    "validator": self.nodes[3].account_public_key,
                },
            )
            reward_distribution_before_rebalance = await node0.call(
                "masternodes",
                "get_reward_distribution_info",
                {"validator": self.nodes[3].account_public_key},
            )
            assert_equal(
                coerce_int(delegation["amount"]),
                250,
                label="delegation amount after delegate",
            )
            assert_true(
                self.delegator_wallet.public_key
                in await node0.call(
                    "masternodes",
                    "get_delegators",
                    {"validator": self.nodes[3].account_public_key},
                ),
                label="delegator listed for node3",
            )

            rebalance_receipt = await self.submit_tx(
                node0,
                "masternodes",
                "rebalance",
                {},
                label="auto-rebalance-after-delegation",
                chi=GOVERNANCE_TX_CHI,
            )
            active_after_rebalance = await node0.call(
                "masternodes",
                "get_active_validators",
                {},
            )
            active_after_rebalance_accounts = [entry["account"] for entry in active_after_rebalance]
            assert_equal(
                active_after_rebalance_accounts,
                [
                    self.nodes[0].account_public_key,
                    self.nodes[3].account_public_key,
                    self.nodes[1].account_public_key,
                ],
                label="active validators after delegation rebalance",
            )

            jail_vote = await self.approve_members_vote(
                node0,
                [("node3", node3), ("node1", node1)],
                type_of_vote="jail_member",
                arg={
                    "member": self.nodes[1].account_public_key,
                    "reason": "script-jail-check",
                },
                label_prefix="auto-jail-member",
            )
            active_after_jail = await node0.call(
                "masternodes",
                "get_active_validators",
                {},
            )
            active_after_jail_accounts = [entry["account"] for entry in active_after_jail]
            assert_equal(
                active_after_jail_accounts,
                [
                    self.nodes[0].account_public_key,
                    self.nodes[3].account_public_key,
                    self.nodes[2].account_public_key,
                ],
                label="active validators after jail",
            )
            jailed_validator = await node0.call(
                "masternodes",
                "get_validator",
                {"account": self.nodes[1].account_public_key},
            )
            assert_equal(
                jailed_validator["jailed"],
                True,
                label="node1 jailed flag",
            )

            unjail_vote = await self.approve_members_vote(
                node0,
                [("node3", node3), ("node2", node2)],
                type_of_vote="unjail_member",
                arg=self.nodes[1].account_public_key,
                label_prefix="auto-unjail-member",
            )
            active_after_unjail = await node0.call(
                "masternodes",
                "get_active_validators",
                {},
            )
            active_after_unjail_accounts = [entry["account"] for entry in active_after_unjail]
            assert_equal(
                active_after_unjail_accounts,
                [
                    self.nodes[0].account_public_key,
                    self.nodes[3].account_public_key,
                    self.nodes[1].account_public_key,
                ],
                label="active validators after unjail",
            )

            dao_balance_before_slash = await node0.get_state(
                "currency",
                "balances",
                "dao",
            )
            slash_vote = await self.approve_members_vote(
                node0,
                [("node3", node3), ("node1", node1)],
                type_of_vote="slash_member",
                arg={
                    "member": self.nodes[1].account_public_key,
                    "slash_bps": 1000,
                    "reason": "script-slash-check",
                },
                label_prefix="auto-slash-member",
            )
            dao_balance_after_slash = await node0.get_state(
                "currency",
                "balances",
                "dao",
            )
            validator_after_slash = await node0.call(
                "masternodes",
                "get_validator",
                {"account": self.nodes[1].account_public_key},
            )
            assert_equal(
                coerce_int(validator_after_slash["total_slashed"]),
                30,
                label="node1 total slashed",
            )
            assert_equal(
                coerce_int(validator_after_slash["total_bond"]),
                270,
                label="node1 total bond after slash",
            )
            assert_equal(
                coerce_decimal(dao_balance_after_slash) - coerce_decimal(dao_balance_before_slash),
                Decimal("30"),
                label="dao balance delta after slash",
            )

            undelegate_receipt = await self.submit_tx(
                delegator,
                "masternodes",
                "undelegate",
                {"validator": self.nodes[3].account_public_key, "amount": 100},
                label="undelegate-from-node3",
                chi=GOVERNANCE_TX_CHI,
            )
            pending_unbond_ids = await node0.call(
                "masternodes",
                "get_pending_unbond_ids",
                {"owner": self.delegator_wallet.public_key},
            )
            assert_true(
                len(pending_unbond_ids) > 0,
                label="delegator pending unbond ids",
            )
            pending_unbond_id = pending_unbond_ids[-1]
            pending_unbond = await node0.call(
                "masternodes",
                "get_pending_unbond",
                {"unbond_id": pending_unbond_id},
            )
            delegator_balance_after_undelegate = coerce_decimal(
                await node0.get_state(
                    "currency",
                    "balances",
                    self.delegator_wallet.public_key,
                )
            )
            assert_equal(
                coerce_int(pending_unbond["amount"]),
                100,
                label="pending unbond amount",
            )

            claim_unbond_receipt = await self.submit_tx(
                delegator,
                "masternodes",
                "claim_unbond",
                {"unbond_id": pending_unbond_id},
                label="claim-pending-unbond",
                chi=GOVERNANCE_TX_CHI,
            )
            pending_unbond_after_claim = await node0.call(
                "masternodes",
                "get_pending_unbond",
                {"unbond_id": pending_unbond_id},
            )
            delegator_balance_after_claim = coerce_decimal(
                await node0.get_state(
                    "currency",
                    "balances",
                    self.delegator_wallet.public_key,
                )
            )
            assert_equal(
                pending_unbond_after_claim["claimed"],
                True,
                label="pending unbond claimed flag",
            )
            assert_true(
                delegator_balance_after_claim > delegator_balance_after_undelegate,
                label="delegator balance increased after claim",
            )

            reward_distribution_after_claim = await node0.call(
                "masternodes",
                "get_reward_distribution_info",
                {"validator": self.nodes[3].account_public_key},
            )
            assert_equal(
                coerce_int(reward_distribution_after_claim["total_delegated"]),
                150,
                label="node3 total delegated after undelegate",
            )

        return {
            "approvals": approvals,
            "bond_self": bond_receipts,
            "pre_policy_recovery": pre_policy_recovery,
            "policy_update": policy_update,
            "policy_recovery": policy_recovery,
            "live_after_policy": live_after_policy,
            "active_after_policy": active_after_policy,
            "delegator_approval": delegator_approval,
            "delegate": delegate_receipt,
            "delegation": delegation,
            "reward_distribution_before_rebalance": reward_distribution_before_rebalance,
            "rebalance_after_delegate": rebalance_receipt,
            "active_after_rebalance": active_after_rebalance,
            "jail_vote": jail_vote,
            "active_after_jail": active_after_jail,
            "jailed_validator": jailed_validator,
            "unjail_vote": unjail_vote,
            "active_after_unjail": active_after_unjail,
            "slash_vote": slash_vote,
            "validator_after_slash": validator_after_slash,
            "dao_balance_before_slash": dao_balance_before_slash,
            "dao_balance_after_slash": dao_balance_after_slash,
            "undelegate": undelegate_receipt,
            "pending_unbond_ids": pending_unbond_ids,
            "pending_unbond": pending_unbond,
            "claim_unbond": claim_unbond_receipt,
            "pending_unbond_after_claim": pending_unbond_after_claim,
            "reward_distribution_after_claim": reward_distribution_after_claim,
        }

    async def hybrid_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        node0_wallet = self.validator_wallets[0]
        node1_wallet = self.validator_wallets[1]
        node2_wallet = self.validator_wallets[2]
        node3_wallet = self.validator_wallets[3]
        node1_key = self.nodes[1].account_public_key
        phase_recovery = await self.recover_current_height(
            session,
            timeout_seconds=15.0,
        )

        async with (
            self.client(node0_wallet, 0, session) as node0,
            self.client(node1_wallet, 1, session) as node1,
            self.client(node2_wallet, 2, session) as node2,
            self.client(node3_wallet, 3, session) as node3,
        ):
            clients_by_account = {
                self.nodes[0].account_public_key: ("node0", node0),
                self.nodes[1].account_public_key: ("node1", node1),
                self.nodes[2].account_public_key: ("node2", node2),
                self.nodes[3].account_public_key: ("node3", node3),
            }

            def voters_for_active_set(
                active_validators: list[dict[str, Any]],
            ) -> list[tuple[str, XianAsync]]:
                voters = []
                for validator in active_validators:
                    account = validator["account"]
                    if account == self.nodes[0].account_public_key:
                        continue
                    if account in clients_by_account:
                        voters.append(clients_by_account[account])
                return voters

            remove_candidate = await self.approve_members_vote(
                node0,
                [("node3", node3), ("node1", node1)],
                type_of_vote="remove_member",
                arg=node1_key,
                label_prefix="hybrid-remove-candidate",
            )
            validator_after_remove = await node0.call(
                "masternodes",
                "get_validator",
                {"account": node1_key},
            )
            assert_equal(
                validator_after_remove["status"],
                "removed",
                label="node1 removed before hybrid re-register",
            )
            active_after_remove = await node0.call(
                "masternodes",
                "get_active_validators",
                {},
            )

            switch_to_hybrid = await self.approve_members_vote(
                node0,
                voters_for_active_set(active_after_remove),
                type_of_vote="update_policy",
                arg={"selection_mode": "hybrid"},
                label_prefix="hybrid-update-policy",
            )
            policy_after_switch = await node0.call(
                "masternodes",
                "get_policy_config",
                {},
            )
            assert_equal(
                policy_after_switch["selection_mode"],
                "hybrid",
                label="hybrid selection mode",
            )

            registration_fee = coerce_int(await node0.get_state("masternodes", "registration_fee"))
            approve_registration = await self.submit_tx(
                node1,
                "currency",
                "approve",
                {"amount": registration_fee, "to": "masternodes"},
                label="hybrid-register-approve",
            )
            register_receipt = await self.submit_tx(
                node1,
                "masternodes",
                "register",
                {
                    "requested_validator_power": 11,
                    "moniker": "node-1-hybrid",
                    "network_endpoint": "localnet://node-1-hybrid",
                },
                label="hybrid-register",
                chi=GOVERNANCE_TX_CHI,
            )
            approve_bond = await self.submit_tx(
                node1,
                "currency",
                "approve",
                {"amount": 400, "to": "masternodes"},
                label="hybrid-bond-approve",
            )
            bond_receipt = await self.submit_tx(
                node1,
                "masternodes",
                "bond_self",
                {"amount": 350},
                label="hybrid-bond-self",
            )

            rebalance_pending = await self.submit_tx(
                node0,
                "masternodes",
                "rebalance",
                {},
                label="hybrid-rebalance-before-approval",
                chi=GOVERNANCE_TX_CHI,
            )
            active_before_approval = await node0.call(
                "masternodes",
                "get_active_validators",
                {},
            )
            active_before_approval_accounts = [entry["account"] for entry in active_before_approval]
            assert_true(
                node1_key not in active_before_approval_accounts,
                label="node1 blocked before hybrid approval",
            )

            active_before_approval_voters = voters_for_active_set(active_before_approval)
            add_member = await self.approve_members_vote(
                node0,
                active_before_approval_voters,
                type_of_vote="add_member",
                arg=node1_key,
                label_prefix="hybrid-add-member",
            )
            active_after_approval = await node0.call(
                "masternodes",
                "get_active_validators",
                {},
            )
            active_after_approval_accounts = [entry["account"] for entry in active_after_approval]
            assert_true(
                node1_key in active_after_approval_accounts,
                label="node1 active after hybrid approval",
            )

            live_after_hybrid = await self.wait_for_validator_count(
                session,
                expected_count=3,
            )

        return {
            "phase_recovery": phase_recovery,
            "remove_candidate": remove_candidate,
            "validator_after_remove": validator_after_remove,
            "active_after_remove": active_after_remove,
            "approve_registration": approve_registration,
            "register": register_receipt,
            "approve_bond": approve_bond,
            "bond_self": bond_receipt,
            "switch_to_hybrid": switch_to_hybrid,
            "policy_after_switch": policy_after_switch,
            "rebalance_before_approval": rebalance_pending,
            "active_before_approval": active_before_approval,
            "add_member": add_member,
            "active_after_approval": active_after_approval,
            "live_after_hybrid": live_after_hybrid,
        }

    async def evidence_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        node0_wallet = self.validator_wallets[0]
        node1_account = self.nodes[1].account_public_key
        node2_account = self.nodes[2].account_public_key
        node3_account = self.nodes[3].account_public_key
        node1_consensus_address = self.load_priv_validator_key(1)["address"]
        node2_consensus_address = self.load_priv_validator_key(2)["address"]
        phase_recovery = await self.recover_current_height(
            session,
            timeout_seconds=15.0,
        )

        async with self.client(node0_wallet, 0, session) as node0:
            policy = await node0.call("masternodes", "get_policy_config", {})
            validator_before = await node0.call(
                "masternodes",
                "get_validator",
                {"account": node1_account},
            )
            active_before = await node0.call(
                "masternodes",
                "get_active_validators",
                {},
            )
            dao_balance_before = await node0.get_state("currency", "balances", "dao")
            live_target_present_before = await self.wait_for_live_validator_address(
                session,
                rpc_url=self.nodes[0].rpc_url,
                validator_address=node1_consensus_address,
                expected_present=True,
                timeout_seconds=45.0,
                label="hybrid validator set inclusion before evidence",
            )
            evidence_broadcast = await self.broadcast_duplicate_vote_evidence(
                session,
                reporter_node_index=0,
                target_node_index=1,
            )

            current_height = await latest_height(session, self.nodes[0].rpc_url)
            await wait_for_height(
                session,
                self.nodes[0].rpc_url,
                current_height + 2,
                timeout_seconds=45.0,
            )
            validator_after = await self.wait_for_validator_record(
                node0,
                node1_account,
                predicate=lambda record: record["jailed"] is True,
                timeout_seconds=30.0,
                label="evidence duplicate-vote penalty applied",
            )
            active_after = await self.wait_for_active_validators(
                node0,
                expected_accounts=[
                    self.nodes[0].account_public_key,
                    node3_account,
                    node2_account,
                ],
                timeout_seconds=30.0,
                label="evidence active-set rebalance",
            )
            live_after = await self.wait_for_validator_count(
                session,
                expected_count=3,
            )
            live_target_absent_after = await self.wait_for_live_validator_address(
                session,
                rpc_url=self.nodes[0].rpc_url,
                validator_address=node1_consensus_address,
                expected_present=False,
                timeout_seconds=30.0,
                label="slashed validator removed from live validator set",
            )
            live_replacement_present = await self.wait_for_live_validator_address(
                session,
                rpc_url=self.nodes[0].rpc_url,
                validator_address=node2_consensus_address,
                expected_present=True,
                timeout_seconds=30.0,
                label="replacement validator entered live validator set",
            )
            dao_balance_after = await node0.get_state("currency", "balances", "dao")

        expected_slash = (
            coerce_decimal(validator_before["total_bond"])
            * coerce_decimal(policy["duplicate_vote_slash_bps"])
            / Decimal("10000")
        )
        assert_equal(
            coerce_decimal(validator_after["total_slashed"])
            - coerce_decimal(validator_before["total_slashed"]),
            expected_slash,
            label="duplicate-vote slash delta",
        )
        assert_equal(
            coerce_decimal(validator_before["total_bond"])
            - coerce_decimal(validator_after["total_bond"]),
            expected_slash,
            label="duplicate-vote bond delta",
        )
        assert_equal(
            validator_after["jail_reason"],
            "duplicate_vote",
            label="duplicate-vote jail reason",
        )
        assert_equal(
            coerce_decimal(dao_balance_after) - coerce_decimal(dao_balance_before),
            expected_slash,
            label="duplicate-vote dao balance delta",
        )

        return {
            "phase_recovery": phase_recovery,
            "policy": policy,
            "validator_before": validator_before,
            "active_before": active_before,
            "live_target_present_before": live_target_present_before,
            "dao_balance_before": dao_balance_before,
            "evidence_broadcast": evidence_broadcast,
            "validator_after": validator_after,
            "active_after": active_after,
            "live_after": live_after,
            "live_target_absent_after": live_target_absent_after,
            "live_replacement_present": live_replacement_present,
            "dao_balance_after": dao_balance_after,
            "expected_slash": expected_slash,
        }

    async def leave_announcement_phase(
        self,
        session: aiohttp.ClientSession,
    ) -> dict[str, Any]:
        node0_wallet = self.validator_wallets[0]
        node3_wallet = self.validator_wallets[3]
        node3_account = self.nodes[3].account_public_key
        phase_recovery = await self.recover_current_height(
            session,
            timeout_seconds=15.0,
        )

        async with (
            self.client(node0_wallet, 0, session) as node0,
            self.client(node3_wallet, 3, session) as node3,
        ):
            validator_before = await node0.call(
                "masternodes",
                "get_validator",
                {"account": node3_account},
            )
            active_before = await node0.call(
                "masternodes",
                "get_active_validators",
                {},
            )
            announce_leave_receipt = await self.submit_tx(
                node3,
                "masternodes",
                "announce_leave",
                {},
                label="announce-leave-node3",
                chi=GOVERNANCE_TX_CHI,
            )
            validator_after_announce = await self.wait_for_validator_record(
                node0,
                node3_account,
                predicate=lambda record: (
                    record["status"] == "leaving"
                    and record["pending_leave_at"] not in (False, None)
                ),
                timeout_seconds=15.0,
                label="announce_leave recorded",
            )
            immediate_leave_submission = await node3.send_tx(
                "masternodes",
                "leave",
                {},
                chi=GOVERNANCE_TX_CHI,
                wait_for_tx=True,
            )
            immediate_leave_failure = ensure_failed_submission(
                immediate_leave_submission,
                label="leave-before-delay",
                message_contains="Leave announcement period not over.",
            )
            rebalance_receipt = await self.submit_tx(
                node0,
                "masternodes",
                "rebalance",
                {},
                label="rebalance-after-announce-leave",
                chi=GOVERNANCE_TX_CHI,
            )
            active_after_rebalance = await self.wait_for_active_validators(
                node0,
                expected_accounts=[
                    self.nodes[0].account_public_key,
                    self.nodes[2].account_public_key,
                    self.nodes[4].account_public_key,
                ],
                timeout_seconds=30.0,
                label="active set after announce_leave rebalance",
            )
            live_after_rebalance = await self.wait_for_validator_count(
                session,
                expected_count=3,
            )
            validator_after_rebalance = await node0.call(
                "masternodes",
                "get_validator",
                {"account": node3_account},
            )

        assert_equal(
            validator_after_rebalance["status"],
            "leaving",
            label="validator status after announce_leave rebalance",
        )
        assert_equal(
            validator_after_rebalance["active"],
            False,
            label="validator active flag after announce_leave rebalance",
        )
        assert_true(
            validator_after_rebalance["pending_leave_at"] not in (False, None),
            label="validator pending leave timestamp after rebalance",
        )

        return {
            "phase_recovery": phase_recovery,
            "validator_before": validator_before,
            "active_before": active_before,
            "announce_leave": announce_leave_receipt,
            "validator_after_announce": validator_after_announce,
            "leave_before_delay_failure": immediate_leave_failure,
            "rebalance_after_announce": rebalance_receipt,
            "active_after_rebalance": active_after_rebalance,
            "live_after_rebalance": live_after_rebalance,
            "validator_after_rebalance": validator_after_rebalance,
        }

    async def run(self) -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            summary = {
                "run_id": self.run_id,
                "started_at": datetime.now(UTC).isoformat(),
            }
            summary["bootstrap"] = await self.bootstrap(session)
            self.write_json("00-bootstrap", summary["bootstrap"])
            summary["health"] = await self.health(session)
            self.write_json("01-health", summary["health"])
            summary["generic_governance"] = await self.generic_governance_phase(session)
            self.write_json("02-generic-governance", summary["generic_governance"])
            summary["state_patch"] = await self.state_patch_phase(session)
            self.write_json("03-state-patch", summary["state_patch"])
            summary["manual_members"] = await self.manual_members_phase(session)
            self.write_json("04-manual-members", summary["manual_members"])
            summary["auto_delegation"] = await self.auto_delegation_phase(session)
            self.write_json("05-auto-delegation", summary["auto_delegation"])
            summary["hybrid"] = await self.hybrid_phase(session)
            self.write_json("06-hybrid", summary["hybrid"])
            summary["evidence"] = await self.evidence_phase(session)
            self.write_json("07-evidence", summary["evidence"])
            summary["leave_announcement"] = await self.leave_announcement_phase(session)
            self.write_json("08-leave-announcement", summary["leave_announcement"])
            summary["ended_at"] = datetime.now(UTC).isoformat()
            summary["coverage_notes"] = [
                "Covered: generic governance contract calls and proposal voting.",
                "Covered: bundle-backed governance state-patch approval, scheduling, "
                "activation, and on-disk patch inventory.",
                "Covered: manual validator votes, removal, registration update, and re-addition.",
                "Covered: self-bonding, delegation, reward-distribution getters, "
                "auto_top_n rebalancing, jail, unjail, slash, undelegate, claim_unbond, "
                "and hybrid approval gating.",
                "Covered: real CometBFT duplicate-vote evidence broadcast and ABCI-driven "
                "slashing/jailing with active-set replacement.",
                "Covered: announce_leave, enforced delay on immediate leave, and "
                "validator-set rebalance while leave is pending, including standby "
                "validator promotion.",
                "Not covered: delayed leave completion after the full 7-day waiting period "
                "and real light-client-attack evidence.",
            ]
            self.write_json("summary", summary)
            return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a focused validator/governance exercise against a disposable 5-validator localnet."
        )
    )
    parser.add_argument("--seed", default="xian-localnet-testnet-governance-v1")
    parser.add_argument("--nodes", type=int, default=DEFAULT_LOCALNET_NODES)
    parser.add_argument("--port-offset", type=int, default=1000)
    parser.add_argument(
        "--topology",
        choices=sorted(LOCALNET_IMAGE_BY_TOPOLOGY),
        default="integrated",
    )
    parser.add_argument(
        "--genesis-network",
        default=DEFAULT_GENESIS_NETWORK,
        help="genesis bundle name used to seed localnet genesis",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--rpc-timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--bootstrap",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Recreate and start the localnet before running the exercise.",
    )
    parser.add_argument(
        "--build",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Force a localnet image build before startup.",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    runner = ValidatorGovernanceRunner(args)
    summary = await runner.run()
    print(json.dumps(normalize_value(summary), indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
