#!/usr/bin/env python3
"""Run a layered 5-validator testnet-shaped localnet e2e program."""

from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
from governance_vote_helpers import cast_votes_until_status, wait_for_status
from localnet_vm_rollout import collect_localnet_vm_rollout_report
from shielded_relayer_backend import (
    DEFAULT_SHIELDED_RELAYER_HOST,
    DEFAULT_SHIELDED_RELAYER_PORT,
    shielded_relayer_endpoints,
    start_shielded_relayer_runtime,
    stop_shielded_relayer_runtime,
)
from state_convergence_helpers import wait_for_uniform_state

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent
ROOT_DIR = STACK_DIR.parent
NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"
WORKLOADS_DIR = STACK_DIR / "workloads"
OUTPUT_ROOT = STACK_DIR / ".artifacts" / "localnet-e2e"
CONTRACTS_DIR = ROOT_DIR / "xian-contracts" / "contracts"
XIAN_ZK_PYTHON_DIR = (
    ROOT_DIR / "xian-contracting" / "packages" / "xian-zk" / "python"
)
XIAN_CONTRACTING_SRC = ROOT_DIR / "xian-contracting" / "src"
XIAN_ABCI_SRC = ROOT_DIR / "xian-abci" / "src"
ORCHESTRATION_TEMPLATE_MODULE = "__ORCH_TEMPLATE__"
RUST_TRACER_MODE = "native_instruction_v1"
DEFAULT_LOCALNET_NODES = 5
DEFAULT_GENESIS_NETWORK = "testnet"
DEFAULT_TX_CHI = 15_000
DEFAULT_TRANSFER_CHI = 2_000
GOVERNANCE_TX_CHI = 200_000
STATE_PATCH_DELAY_BLOCKS = 8
STATE_PATCH_ACTIVATION_HEADROOM_BLOCKS = 8
SIMULATOR_BURST_REQUESTS = 128
DEFAULT_SIMULATOR_BURST_CONCURRENCY = 32
WEBSOCKET_TIMEOUT_SECONDS = 20.0
LOCALNET_POSTGRES_SERVICE = "localnet-postgres"
LOCALNET_POSTGRES_CONTAINER = "xian-localnet-postgres"
SECONDARY_BDS_POSTGRES_IMAGE = "postgres:17"
SECONDARY_BDS_POSTGRES_USER = "xian"
SECONDARY_BDS_POSTGRES_PASSWORD = "xian"
SECONDARY_BDS_POSTGRES_DATABASE = "xian"
SECONDARY_BDS_POSTGRES_PORT_BASE = 55432
SECONDARY_BDS_QUERY_TIMEOUT_SECONDS = 180.0
CONTRACT_ORCHESTRATION_TX_CHI = {
    "deploy_contract": 180_000,
    "deploy_family": 100_000,
    "dynamic_call": 50_000,
}
SHIELDED_TX_CHI = {
    "deposit": 8_000_000,
    "transfer": 10_000_000,
    "withdraw": 8_000_000,
}
CURRENT_UV_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"

sys.path.insert(0, str(XIAN_CONTRACTING_SRC))
sys.path.insert(0, str(XIAN_ZK_PYTHON_DIR))
sys.path.insert(0, str(XIAN_ABCI_SRC))

from contracting.compilation.artifacts import build_contract_artifacts  # noqa: E402
from xian_py.config import RetryPolicy, XianClientConfig  # noqa: E402
import xian_py.transaction as tr  # noqa: E402
from xian_py.wallet import Wallet  # noqa: E402
from xian_py.xian_async import XianAsync  # noqa: E402
from xian_py.exception import SimulationError, TransportError, TxTimeoutError  # noqa: E402
from xian_py.shielded_relayer import ShieldedRelayerAsyncClient  # noqa: E402

try:  # noqa: SIM105
    from xian_zk import (  # noqa: E402
        ShieldedDepositRequest,
        ShieldedKeyBundle,
        ShieldedNote,
        ShieldedNoteProver,
        ShieldedRelayTransferProver,
        ShieldedRelayTransferWallet,
        note_records_from_transactions,
        ShieldedOutput,
        ShieldedTransferRequest,
        ShieldedWallet,
        ShieldedWithdrawRequest,
        output_payload_hashes,
        recover_encrypted_notes,
        scan_notes,
        shielded_relay_registry_manifest,
        shielded_registry_manifest,
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


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=128)
def build_deployment_artifacts(module_name: str, source: str) -> dict[str, Any]:
    return build_contract_artifacts(module_name=module_name, source=source)


@functools.lru_cache(maxsize=8)
def render_orchestration_factory_source() -> str:
    factory_template = read_text(WORKLOADS_DIR / "e2e" / "orchestration_factory.py")
    child_source = read_text(WORKLOADS_DIR / "e2e" / "orchestration_child.py")
    child_artifacts = build_contract_artifacts(
        module_name=ORCHESTRATION_TEMPLATE_MODULE,
        source=child_source,
    )
    replacements = {
        "__ORCH_CHILD_SOURCE_JSON__": json.dumps(child_artifacts["source"]),
        "__ORCH_CHILD_VM_IR_TEMPLATE_JSON__": json.dumps(
            child_artifacts["vm_ir_json"]
        ),
        "__ORCH_CHILD_ARTIFACT_FORMAT_JSON__": json.dumps(
            child_artifacts["format"]
        ),
        "__ORCH_CHILD_VM_PROFILE_JSON__": json.dumps(
            child_artifacts["vm_profile"]
        ),
        "__ORCH_CHILD_SOURCE_SHA256_JSON__": json.dumps(
            child_artifacts["hashes"]["source_sha256"]
        ),
    }
    rendered = factory_template
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    unresolved = sorted(
        token
        for token in set(re.findall(r"__ORCH_[A-Z0-9_]+__", rendered))
        if token != ORCHESTRATION_TEMPLATE_MODULE
    )
    if unresolved:
        raise E2EError(
            "unresolved orchestration factory placeholders: "
            + ", ".join(unresolved)
        )
    return rendered


def load_network() -> dict[str, Any]:
    if not NETWORK_PATH.exists():
        raise E2EError(
            f"localnet metadata not found at {NETWORK_PATH}; bootstrap first"
        )
    return json.loads(NETWORK_PATH.read_text(encoding="utf-8"))


def derive_wallet(seed: str, label: str) -> Wallet:
    digest = hashlib.sha256(f"{seed}:{label}".encode("utf-8")).hexdigest()
    return Wallet(private_key=digest)


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
    env["XIAN_LOCALNET_TRACER_MODE"] = RUST_TRACER_MODE
    env["XIAN_LOCALNET_EXECUTION_MODE"] = args.execution_mode
    env["XIAN_LOCALNET_EXECUTION_BYTECODE_VERSION"] = (
        args.execution_bytecode_version
    )
    env["XIAN_LOCALNET_EXECUTION_GAS_SCHEDULE"] = args.execution_gas_schedule
    env["XIAN_LOCALNET_EXECUTION_AUTHORITY"] = args.execution_authority
    env["XIAN_LOCALNET_EXECUTION_SHADOW_TRACER_MODE"] = (
        args.execution_shadow_tracer_mode
    )
    env["XIAN_LOCALNET_GENESIS_NETWORK"] = args.genesis_network
    env["XIAN_LOCALNET_ENABLE_BDS"] = "1"
    env["XIAN_LOCALNET_BDS_NODE_INDEX"] = str(args.bds_node_index)
    env["XIAN_LOCALNET_PORT_OFFSET"] = str(args.port_offset)
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
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            check=True,
            capture_output=capture_output,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise E2EError(format_subprocess_error(exc)) from exc


def format_subprocess_error(
    exc: subprocess.CalledProcessError,
    *,
    max_lines: int = 80,
) -> str:
    command = exc.cmd
    if isinstance(command, (list, tuple)):
        command_str = " ".join(str(part) for part in command)
    else:
        command_str = str(command)

    def tail(text: str | None) -> str:
        if not text:
            return ""
        lines = text.strip().splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return "\n".join(
            ["..."] + lines[-max_lines:]
        )

    lines = [f"command failed with exit code {exc.returncode}: {command_str}"]
    stdout_tail = tail(exc.stdout)
    stderr_tail = tail(exc.stderr)
    if stdout_tail:
        lines.extend(["", "stdout (tail):", stdout_tail])
    if stderr_tail:
        lines.extend(["", "stderr (tail):", stderr_tail])
    return "\n".join(lines)


def run_make(target: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run_cmd(["make", target], env=env)


def run_localnet_compose(
    *compose_args: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return run_cmd(
        ["docker", "compose", "-f", "docker-compose-localnet.yml", *compose_args],
        cwd=STACK_DIR,
        env=env,
    )


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


def construct_token_permit_message(
    *,
    token_contract: str,
    owner: str,
    spender: str,
    value: int | float,
    deadline: str,
    authorizer_contract: str,
    chain_id: str,
) -> str:
    return (
        f"{token_contract}:{owner}:{spender}:{value}:{deadline}:"
        f"{authorizer_contract}:{chain_id}"
    )


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
        try:
            payload = await fetch_json(session, f"{rpc_url}/status", timeout=5.0)
            height = int(payload["result"]["sync_info"]["latest_block_height"])
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.5)
            continue
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


async def latest_heights(
    session: aiohttp.ClientSession,
    nodes: list[LocalnetNode],
) -> dict[str, int]:
    heights = await asyncio.gather(
        *(latest_height(session, node.rpc_url) for node in nodes)
    )
    return {
        node.moniker: height for node, height in zip(nodes, heights, strict=True)
    }


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


async def wait_for_bds_backlog(
    client: XianAsync,
    *,
    target_height: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            status = await client.get_bds_status()
        except Exception as exc:  # noqa: BLE001
            last_status = {"error": str(exc)}
            await asyncio.sleep(0.5)
            continue

        raw = normalize_value(status.raw)
        last_status = raw
        indexed_height = status.indexed_height
        if indexed_height is not None and indexed_height < target_height:
            return raw
        if status.spool_pending_count > 0:
            return raw
        if raw.get("last_enqueue_error"):
            return raw
        if str(raw.get("db_status")) not in {"ok", "None"}:
            return raw
        await asyncio.sleep(0.5)

    raise E2EError(
        f"BDS did not show backlog or degradation before timeout; last={last_status}"
    )


async def wait_for_bds_recovered(
    client: XianAsync,
    *,
    target_height: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            status = await client.get_bds_status()
        except Exception as exc:  # noqa: BLE001
            last_status = {"error": str(exc)}
            await asyncio.sleep(0.5)
            continue

        raw = normalize_value(status.raw)
        last_status = raw
        indexed_height = status.indexed_height
        recovered = (
            indexed_height is not None
            and indexed_height >= target_height
            and status.spool_pending_count == 0
            and str(raw.get("db_status")) == "ok"
            and not raw.get("last_enqueue_error")
        )
        if recovered:
            return raw
        await asyncio.sleep(0.5)

    raise E2EError(
        f"BDS did not recover to height {target_height}; last={last_status}"
    )


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

    raise E2EError(
        f"container {container_name} did not reach state {sorted(expected_states)}; "
        f"last={last_state!r}"
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
    values = await asyncio.gather(
        *(fetch_abci_query(session, node.rpc_url, path) for node in nodes)
    )
    return {
        node.moniker: value for node, value in zip(nodes, values, strict=True)
    }


async def perf_status_from_all_nodes(
    session: aiohttp.ClientSession,
    nodes: list[LocalnetNode],
) -> dict[str, dict[str, Any]]:
    payloads = await asyncio.gather(
        *(fetch_abci_query(session, node.rpc_url, "/perf_status") for node in nodes)
    )
    return {
        node.moniker: payload for node, payload in zip(nodes, payloads, strict=True)
    }


def recent_blocks_in_window(
    perf_status: dict[str, Any],
    *,
    min_height: int | None,
    max_height: int | None,
) -> list[dict[str, Any]]:
    recent_blocks = perf_status.get("recent_blocks") or []
    blocks = []
    for block in recent_blocks:
        try:
            height = int(block["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if min_height is not None and height < min_height:
            continue
        if max_height is not None and height > max_height:
            continue
        blocks.append(block)
    return blocks


async def wait_for_uniform_node_state(
    session: aiohttp.ClientSession,
    nodes: list[LocalnetNode],
    *,
    contract: str,
    variable: str,
    label: str,
    expected: Any,
    keys: list[str] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, str]:
    try:
        return await wait_for_uniform_state(
            fetch_values=lambda: query_state_from_all_nodes(
                session,
                nodes,
                contract=contract,
                variable=variable,
                keys=keys,
            ),
            fetch_heights=lambda: latest_heights(session, nodes),
            label=label,
            normalize_value=normalize_value,
            expected=expected,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=0.25,
        )
    except RuntimeError as exc:
        raise E2EError(str(exc)) from exc


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
        if submission.mode == "commit" and submission.accepted is True and submission.finalized:
            return normalize_receipt(submission, label=label)
        raise E2EError(f"{label}: receipt missing")
    if submission.receipt.success is not True:
        raise E2EError(
            f"{label}: transaction failed during execution: "
            f"{submission.receipt.message}"
        )
    return normalize_receipt(submission, label=label)


def ensure_failed_submission(
    submission,
    *,
    label: str,
    expected_message_fragment: str | None = None,
) -> dict[str, Any]:
    receipt = normalize_receipt(submission, label=label)
    if receipt["accepted"] is not False and receipt["success"] is not False:
        raise E2EError(f"{label}: transaction unexpectedly succeeded")
    if expected_message_fragment is not None and expected_message_fragment not in str(
        receipt.get("message")
    ):
        raise E2EError(
            f"{label}: expected failure containing {expected_message_fragment!r}, "
            f"got {receipt.get('message')!r}"
        )
    return receipt


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
        (STACK_DIR / ".localnet").glob("node-*/.cometbft/config/xian.toml")
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


def decode_websocket_tx_execution(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        tx_result = (
            payload.get("result", {})
            .get("data", {})
            .get("value", {})
            .get("TxResult", {})
            .get("result", {})
        )
        encoded = tx_result.get("data")
        if not encoded or not isinstance(encoded, str):
            return None
        return json.loads(base64.b64decode(encoded).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


class E2ERunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        if args.resume_dir is not None:
            self.output_dir = Path(args.resume_dir).expanduser().resolve()
            self.run_id = self.output_dir.name
        else:
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
        self.vm_rollout_report: dict[str, Any] | None = None

    @property
    def execution_mode(self) -> str:
        if self.network is not None:
            execution = self.network.get("execution", {})
            mode = execution.get("mode")
            if isinstance(mode, str) and mode:
                return mode
        return str(self.args.execution_mode or "")

    def contract_submission_kwargs(
        self,
        *,
        name: str,
        code: str,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"code": code}
        if self.execution_mode == "xian_vm_v1":
            kwargs["deployment_artifacts"] = build_deployment_artifacts(name, code)
        return kwargs

    @functools.lru_cache(maxsize=1)
    def load_test_client_config(self) -> XianClientConfig:
        return XianClientConfig(
            retry=RetryPolicy(
                max_attempts=6,
                initial_delay_seconds=0.25,
                max_delay_seconds=4.0,
                backoff_multiplier=2.0,
                retry_transport_errors=True,
                retry_rpc_errors=True,
            )
        )

    @staticmethod
    def phase_names() -> list[str]:
        return [
            "00-bootstrap",
            "01-health",
            "02-xian-py-smoke",
            "03-contract-orchestration",
            "04-periodic-load",
            "05-burst-load",
            "06-conflict-invalid",
            "07-dex-mixed",
            "08-simulator-load",
            "09-bds-catchup",
            "10-retrieval-surfaces",
            "11-determinism",
            "12-validator-governance",
            "13-state-patch",
            "14-logging",
            "15-shielded-note-token",
            "16-parallel-execution",
        ]

    def _load_resume_json(self, phase_name: str) -> dict[str, Any]:
        path = self.output_dir / json_file_name(phase_name)
        if not path.exists():
            raise E2EError(f"resume phase artifact not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def load_resume_context(self) -> None:
        network_path = self.output_dir / "network.json"
        if not network_path.exists():
            raise E2EError(
                f"resume directory does not contain network.json: {network_path}"
            )
        self.network = json.loads(network_path.read_text(encoding="utf-8"))
        self.nodes = build_nodes(self.network)
        self.service_node = next(
            (node for node in self.nodes if node.service_node),
            self.nodes[self.args.bds_node_index],
        )
        self.founder_wallet = Wallet(private_key=self.network["founder_key"])
        self.validator_wallets = [
            Wallet(private_key=node.account_private_key) for node in self.nodes
        ]
        if len(self.nodes) != DEFAULT_LOCALNET_NODES:
            raise E2EError(
                f"resume context has {len(self.nodes)} validators; expected "
                f"{DEFAULT_LOCALNET_NODES}"
            )
        if self.network.get("genesis_network") != DEFAULT_GENESIS_NETWORK:
            raise E2EError(
                "resume context genesis network "
                f"{self.network.get('genesis_network')!r} does not match "
                f"{DEFAULT_GENESIS_NETWORK!r}"
            )
        prior_phase_names = self.phase_names()[
            : self.phase_names().index(self.args.start_phase)
        ]
        self.phase_results = []
        for phase_name in prior_phase_names:
            path = self.output_dir / json_file_name(phase_name)
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.phase_results.append(
                PhaseResult(
                    name=payload["name"],
                    ok=bool(payload["ok"]),
                    started_at=payload["started_at"],
                    ended_at=payload["ended_at"],
                    details=payload["details"],
                )
            )

        completed_phase_names = set(prior_phase_names)

        if "02-xian-py-smoke" in completed_phase_names:
            smoke = self._load_resume_json("02-xian-py-smoke")
            for deployment in smoke["details"]["deployments"]:
                label = deployment.get("label", "")
                if label.startswith("deploy con_e2e_conflict_"):
                    self.contracts["conflict"] = label.removeprefix("deploy ")
                elif label.startswith("deploy con_e2e_patch_"):
                    self.contracts["patch_target"] = label.removeprefix("deploy ")

        if "03-contract-orchestration" in completed_phase_names:
            orchestration = self._load_resume_json("03-contract-orchestration")
            self.contracts.update(orchestration["details"]["contracts"])

        if "06-conflict-invalid" in completed_phase_names:
            conflict = self._load_resume_json("06-conflict-invalid")
            self.contracts["conflict"] = conflict["details"]["conflict_contract"]
            self.sample_event_tx_hash = conflict["details"]["winning_tx_hash"]

        if "07-dex-mixed" in completed_phase_names:
            dex = self._load_resume_json("07-dex-mixed")
            contracts = dex["details"]["scenario_summary"]["contracts"]
            self.contracts["dex_token_a"] = contracts["token_a"]
            self.contracts["dex_token_b"] = contracts["token_b"]
            self.contracts["dex_pairs"] = contracts["pairs"]
            self.contracts["dex_router"] = contracts["dex"]
            self.contracts["dex_pair_id"] = dex["details"]["scenario_summary"]["pair_id"]

        if "09-bds-catchup" in completed_phase_names:
            catchup = self._load_resume_json("09-bds-catchup")
            tx_hashes = catchup["details"].get("catchup_tx_hashes") or []
            if tx_hashes:
                self.sample_event_tx_hash = tx_hashes[-1]

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
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
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
        try:
            return await wait_for_status(
                lambda: client.call(
                    "governance",
                    "get_proposal",
                    {"proposal_id": proposal_id},
                ),
                expected_status=expected_status,
                label=f"governance proposal {proposal_id}",
                timeout_seconds=timeout_seconds,
            )
        except RuntimeError as exc:
            raise E2EError(str(exc)) from exc

    async def wait_for_members_vote_status(
        self,
        client: XianAsync,
        proposal_id: int,
        *,
        expected_status: str,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        try:
            return await wait_for_status(
                lambda: client.get_state("masternodes", "votes", proposal_id),
                expected_status=expected_status,
                label=f"members vote {proposal_id}",
                timeout_seconds=timeout_seconds,
            )
        except RuntimeError as exc:
            raise E2EError(str(exc)) from exc

    async def approve_governance_proposal(
        self,
        proposer: XianAsync,
        voters: list[tuple[str, XianAsync]],
        *,
        proposal_function: str,
        proposal_kwargs: dict[str, Any],
        expected_final_status: str,
        label_prefix: str,
    ) -> dict[str, Any]:
        proposal_receipt = await self.submit_tx(
            proposer,
            "governance",
            proposal_function,
            proposal_kwargs,
            label=f"{label_prefix}-propose",
            chi=GOVERNANCE_TX_CHI,
        )
        proposal_id = int(await proposer.get_state("governance", "proposal_count"))
        proposal_pending = await proposer.call(
            "governance",
            "get_proposal",
            {"proposal_id": proposal_id},
        )
        if proposal_pending["status"] != "pending":
            raise E2EError(
                f"{label_prefix} expected pending proposal, got "
                f"{proposal_pending['status']!r}"
            )

        vote_senders = [
            functools.partial(
                self.submit_tx,
                voter,
                "governance",
                "vote",
                {"proposal_id": proposal_id, "support": True},
                label=f"{label_prefix}-vote-{index}-{name}",
                chi=GOVERNANCE_TX_CHI,
            )
            for index, (name, voter) in enumerate(voters, start=1)
        ]
        vote_receipts, proposal_final = await cast_votes_until_status(
            vote_senders,
            fetch_status=lambda: proposer.call(
                "governance",
                "get_proposal",
                {"proposal_id": proposal_id},
            ),
            completed_statuses={expected_final_status},
        )
        if proposal_final is None:
            proposal_final = await self.wait_for_governance_proposal_status(
                proposer,
                proposal_id,
                expected_status=expected_final_status,
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
        proposal_receipt = await self.submit_tx(
            proposer,
            "masternodes",
            "propose_vote",
            {"type_of_vote": type_of_vote, "arg": arg},
            label=f"{label_prefix}-propose",
            chi=GOVERNANCE_TX_CHI,
        )
        proposal_id = int(await proposer.get_state("masternodes", "total_votes"))
        proposal_pending = await proposer.get_state("masternodes", "votes", proposal_id)
        if proposal_pending["status"] != "pending":
            raise E2EError(
                f"{label_prefix} expected pending vote, got "
                f"{proposal_pending['status']!r}"
            )

        vote_senders = [
            functools.partial(
                self.submit_tx,
                voter,
                "masternodes",
                "vote",
                {"proposal_id": proposal_id, "vote": "yes"},
                label=f"{label_prefix}-vote-{index}-{name}",
                chi=GOVERNANCE_TX_CHI,
            )
            for index, (name, voter) in enumerate(voters, start=1)
        ]
        vote_receipts, proposal_final = await cast_votes_until_status(
            vote_senders,
            fetch_status=lambda: proposer.get_state("masternodes", "votes", proposal_id),
            completed_statuses={"approved"},
        )
        if proposal_final is None:
            proposal_final = await self.wait_for_members_vote_status(
                proposer,
                proposal_id,
                expected_status="approved",
            )

        return {
            "proposal_id": proposal_id,
            "proposal_receipt": proposal_receipt,
            "proposal_pending": proposal_pending,
            "vote_receipts": vote_receipts,
            "proposal_final": proposal_final,
        }

    def restart_localnet(self) -> None:
        env = make_localnet_env(self.args)
        run_make("localnet-down", env=env)
        run_make("localnet-up", env=env)

    async def restart_localnet_and_wait_ready(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout_seconds: float | None = None,
        require_additional_block: bool = True,
    ) -> list[dict[str, Any]]:
        self.restart_localnet()
        statuses = await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=(
                self.args.rpc_timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            ),
        )
        if require_additional_block:
            target_height = (
                max(
                    int(status["result"]["sync_info"]["latest_block_height"])
                    for status in statuses
                )
                + 1
            )
            await asyncio.gather(
                *(
                    wait_for_height(
                        session,
                        node.rpc_url,
                        target_height,
                        timeout_seconds=30.0,
                    )
                    for node in self.nodes
                )
            )
        return statuses

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
            raise E2EError(f"node {node.moniker} has no containers to stop")
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
            raise E2EError(f"node {node.moniker} has no containers to start")
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
        return {
            "containers": container_names,
            "states": states,
            "ready_height": ready_height,
        }

    def secondary_bds_container_name(self) -> str:
        return f"xian-localnet-postgres-secondary-{short_hash(self.run_id)}"

    def secondary_bds_host_port(self) -> int:
        return SECONDARY_BDS_POSTGRES_PORT_BASE + int(self.args.port_offset)

    def secondary_bds_home_dir(self) -> Path:
        return self.output_dir / "secondary-bds-home"

    def secondary_bds_data_dir(self) -> Path:
        return self.output_dir / "secondary-bds-postgres"

    def prepare_secondary_bds_home(self, source_node: LocalnetNode) -> Path:
        source_home = STACK_DIR / ".localnet" / source_node.moniker / ".cometbft"
        if not source_home.exists():
            raise E2EError(f"service node home missing at {source_home}")

        secondary_home = self.secondary_bds_home_dir()
        if secondary_home.exists():
            shutil.rmtree(secondary_home)

        target_comet_home = secondary_home / ".cometbft"
        shutil.copytree(source_home / "config", target_comet_home / "config")
        (target_comet_home / "xian").mkdir(parents=True, exist_ok=True)

        config_path = target_comet_home / "config" / "config.toml"
        config_text = config_path.read_text(encoding="utf-8")
        config_text, replacements = re.subn(
            r'^laddr = "tcp://0\.0\.0\.0:26657"$',
            f'laddr = "tcp://127.0.0.1:{source_node.rpc_port}"',
            config_text,
            count=1,
            flags=re.MULTILINE,
        )
        if replacements != 1:
            raise E2EError(
                f"could not rewrite rpc laddr in {config_path}"
            )
        config_path.write_text(config_text, encoding="utf-8")
        return secondary_home

    async def start_secondary_bds_postgres(self) -> dict[str, Any]:
        container_name = self.secondary_bds_container_name()
        host_port = self.secondary_bds_host_port()
        data_dir = self.secondary_bds_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            ["docker", "rm", "-f", container_name],
            cwd=STACK_DIR,
            capture_output=True,
            text=True,
        )
        run_cmd(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container_name,
                "--health-cmd",
                (
                    "pg_isready "
                    f"-U {SECONDARY_BDS_POSTGRES_USER} "
                    f"-d {SECONDARY_BDS_POSTGRES_DATABASE}"
                ),
                "--health-interval",
                "1s",
                "--health-timeout",
                "5s",
                "--health-retries",
                "45",
                "--publish",
                f"{host_port}:5432",
                "--volume",
                f"{data_dir}:/var/lib/postgresql/data",
                "--env",
                f"POSTGRES_USER={SECONDARY_BDS_POSTGRES_USER}",
                "--env",
                f"POSTGRES_PASSWORD={SECONDARY_BDS_POSTGRES_PASSWORD}",
                "--env",
                f"POSTGRES_DB={SECONDARY_BDS_POSTGRES_DATABASE}",
                SECONDARY_BDS_POSTGRES_IMAGE,
            ],
            cwd=STACK_DIR,
        )
        state = await wait_for_container_state(
            container_name,
            expected_states={"healthy"},
            timeout_seconds=45.0,
        )
        return {
            "container_name": container_name,
            "host_port": host_port,
            "state": state,
        }

    async def stop_secondary_bds_postgres(self) -> str:
        container_name = self.secondary_bds_container_name()
        run_cmd(["docker", "stop", container_name], cwd=STACK_DIR)
        return await wait_for_container_state(
            container_name,
            expected_states={"exited"},
            timeout_seconds=30.0,
        )

    async def restart_secondary_bds_postgres(self) -> str:
        container_name = self.secondary_bds_container_name()
        run_cmd(["docker", "start", container_name], cwd=STACK_DIR)
        return await wait_for_container_state(
            container_name,
            expected_states={"healthy"},
            timeout_seconds=45.0,
        )

    def cleanup_secondary_bds_postgres(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.secondary_bds_container_name()],
            cwd=STACK_DIR,
            capture_output=True,
            text=True,
        )

    def run_secondary_bds_reindex(
        self,
        *,
        source_node: LocalnetNode,
        reset: bool,
    ) -> dict[str, Any]:
        secondary_home = self.prepare_secondary_bds_home(source_node)
        host_port = self.secondary_bds_host_port()
        env = os.environ.copy()
        env["HOME"] = str(secondary_home)
        env["XIAN_BDS_DSN"] = (
            "postgresql://"
            f"{SECONDARY_BDS_POSTGRES_USER}:"
            f"{SECONDARY_BDS_POSTGRES_PASSWORD}@"
            f"127.0.0.1:{host_port}/"
            f"{SECONDARY_BDS_POSTGRES_DATABASE}"
        )
        env["XIAN_BDS_HOST"] = "127.0.0.1"
        env["XIAN_BDS_PORT"] = str(host_port)
        env["XIAN_BDS_DATABASE"] = SECONDARY_BDS_POSTGRES_DATABASE
        env["XIAN_BDS_USER"] = SECONDARY_BDS_POSTGRES_USER
        env["XIAN_BDS_PASSWORD"] = SECONDARY_BDS_POSTGRES_PASSWORD
        env["XIAN_BDS_APPLICATION_NAME"] = "xian-bds-secondary"
        env["XIAN_BDS_SPOOL_DIR"] = str(
            secondary_home / ".cometbft" / "xian" / "secondary-bds-spool"
        )

        cmd = [
            "uv",
            "run",
            "--project",
            str(ROOT_DIR / "xian-abci"),
            "--python",
            CURRENT_UV_PYTHON,
            "xian-bds-reindex",
            "--rpc-url",
            source_node.rpc_url,
        ]
        if reset:
            cmd.append("--reset")

        result = run_cmd(cmd, cwd=STACK_DIR, env=env)
        return {
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "secondary_home": str(secondary_home),
            "host_port": host_port,
        }

    def query_secondary_bds_scalar(self, sql: str) -> str:
        deadline = time.monotonic() + SECONDARY_BDS_QUERY_TIMEOUT_SECONDS
        while True:
            try:
                result = run_cmd(
                    [
                        "docker",
                        "exec",
                        self.secondary_bds_container_name(),
                        "psql",
                        "-U",
                        SECONDARY_BDS_POSTGRES_USER,
                        "-d",
                        SECONDARY_BDS_POSTGRES_DATABASE,
                        "-Atqc",
                        sql,
                    ],
                    cwd=STACK_DIR,
                )
                return result.stdout.strip()
            except E2EError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1.0)

    async def bootstrap(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        env = make_localnet_env(self.args)
        outputs: dict[str, Any] = {
            "env": {
                "XIAN_LOCALNET_GENESIS_NETWORK": env["XIAN_LOCALNET_GENESIS_NETWORK"],
                "XIAN_LOCALNET_TRACER_MODE": env["XIAN_LOCALNET_TRACER_MODE"],
                "XIAN_LOCALNET_ENABLE_BDS": env["XIAN_LOCALNET_ENABLE_BDS"],
                "XIAN_LOCALNET_BDS_NODE_INDEX": env["XIAN_LOCALNET_BDS_NODE_INDEX"],
                "XIAN_LOCALNET_PORT_OFFSET": env["XIAN_LOCALNET_PORT_OFFSET"],
                "XIAN_LOCALNET_APP_LOG_LEVEL": env["XIAN_LOCALNET_APP_LOG_LEVEL"],
                "XIAN_LOCALNET_TOPOLOGY": env["XIAN_LOCALNET_TOPOLOGY"],
                "LOCALNET_NODES": env["LOCALNET_NODES"],
            }
        }
        if self.args.bootstrap:
            if (STACK_DIR / "docker-compose-localnet.yml").exists():
                outputs["localnet_down"] = run_make("localnet-down", env=env).stdout
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
        if self.args.nodes != DEFAULT_LOCALNET_NODES:
            raise E2EError(
                f"this e2e harness expects exactly {DEFAULT_LOCALNET_NODES} validators"
            )
        if self.args.genesis_network != DEFAULT_GENESIS_NETWORK:
            raise E2EError(
                f"this e2e harness expects genesis_network={DEFAULT_GENESIS_NETWORK!r}"
            )
        if len(self.nodes) != DEFAULT_LOCALNET_NODES:
            raise E2EError(
                f"loaded localnet has {len(self.nodes)} validators; expected "
                f"{DEFAULT_LOCALNET_NODES}"
            )
        if self.network.get("genesis_network") != self.args.genesis_network:
            raise E2EError(
                "loaded localnet genesis network "
                f"{self.network.get('genesis_network')!r} does not match "
                f"requested {self.args.genesis_network!r}"
            )
        await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=self.args.rpc_timeout_seconds,
        )
        outputs["network"] = {
            "chain_id": self.network["chain_id"],
            "genesis_network": self.network.get("genesis_network"),
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
        *,
        config: XianClientConfig | None = None,
    ) -> XianAsync:
        if wallet is None:
            raise E2EError("wallet is required")
        return XianAsync(
            node_url=self.nodes[node_index].rpc_url,
            chain_id=self.network["chain_id"],
            wallet=wallet,
            config=config,
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
                    chi=DEFAULT_TRANSFER_CHI,
                    mode="commit",
                    wait_for_tx=True,
                )
                receipts.append(
                    ensure_positive_submission(
                        submission,
                        label=f"fund {wallet.public_key[:12]}",
                    )
                )
        for wallet in wallets:
            expected_balance = await fetch_abci_query(
                session,
                self.nodes[0].rpc_url,
                f"/get/currency.balances:{wallet.public_key}",
            )
            await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract="currency",
                variable="balances",
                keys=[wallet.public_key],
                expected=expected_balance,
                label=f"funded balance {wallet.public_key[:12]}",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )
        return receipts

    async def xian_py_smoke(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        founder = self.founder_wallet
        wallets = [derive_wallet(self.seed, f"e2e-wallet-{index}") for index in range(4)]
        funding = await self.fund_wallets(session, wallets, amount=5_000)

        conflict_contract = f"con_e2e_conflict_{short_hash(self.run_id)}"
        patch_contract = f"con_e2e_patch_{short_hash(self.run_id + 'patch')}"
        allocation_contract = (
            f"con_e2e_alloc_{short_hash(self.run_id + 'alloc')}"
        )
        self.contracts["conflict"] = conflict_contract
        self.contracts["patch_target"] = patch_contract
        self.contracts["allocation_guards"] = allocation_contract

        async with self.client(founder, 0, session) as client:
            conflict_submission = await client.submit_contract(
                name=conflict_contract,
                **self.contract_submission_kwargs(
                    name=conflict_contract,
                    code=read_text(WORKLOADS_DIR / "e2e" / "conflict_guard.py"),
                ),
                chi=120_000,
                wait_for_tx=True,
            )
            patch_submission = await client.submit_contract(
                name=patch_contract,
                **self.contract_submission_kwargs(
                    name=patch_contract,
                    code=read_text(WORKLOADS_DIR / "e2e" / "patch_target.py"),
                ),
                chi=90_000,
                wait_for_tx=True,
            )
            allocation_submission = await client.submit_contract(
                name=allocation_contract,
                **self.contract_submission_kwargs(
                    name=allocation_contract,
                    code=read_text(
                        WORKLOADS_DIR / "e2e" / "allocation_guards.py"
                    ),
                ),
                chi=120_000,
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
            allocation_receipt = ensure_positive_submission(
                allocation_submission,
                label=f"deploy {allocation_contract}",
            )
            self.sample_tx_hash = conflict_receipt["tx_hash"]

            balance = await client.get_balance(founder.public_key)
            simulated = await client.simulate("currency", "balance_of", {"address": founder.public_key})
            counter_state = await client.get_state(conflict_contract, "counter")
            patch_status = await client.call(patch_contract, "get_status", {})

            allocation_small = ensure_positive_submission(
                await client.send_tx(
                    allocation_contract,
                    "small_bytes",
                    {"size": 16},
                    chi=DEFAULT_TX_CHI,
                    wait_for_tx=True,
                ),
                label="allocation-small-bytes",
            )
            stable_status = await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract=allocation_contract,
                variable="last_status",
                expected="bytes:16",
                label="allocation guard stable status",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )

            allocation_limit_receipts = []
            for function_name, kwargs in (
                ("explode_range", {"size": 131_073}),
                ("explode_bytes", {"size": 131_073}),
                ("explode_bytearray", {"size": 131_073}),
                ("explode_string_repeat", {"count": 65_537}),
                ("explode_list_repeat", {"count": 65_537}),
            ):
                failed = ensure_failed_submission(
                    await client.send_tx(
                        allocation_contract,
                        function_name,
                        kwargs,
                        chi=DEFAULT_TX_CHI,
                        wait_for_tx=True,
                    ),
                    label=f"allocation-{function_name}",
                    expected_message_fragment="maximum allowed allocation size",
                )
                allocation_limit_receipts.append(failed)

            stable_after_failures = await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract=allocation_contract,
                variable="last_status",
                expected="bytes:16",
                label="allocation guard status after failures",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )

        return {
            "wallets": [wallet.public_key for wallet in wallets],
            "funding": funding,
            "deployments": [
                conflict_receipt,
                patch_receipt,
                allocation_receipt,
            ],
            "founder_balance": normalize_value(balance),
            "simulate_balance": normalize_value(simulated),
            "conflict_counter_state": counter_state,
            "patch_status": patch_status,
            "allocation_guards": {
                "small_success": allocation_small,
                "stable_status": stable_status,
                "failure_receipts": allocation_limit_receipts,
                "stable_status_after_failures": stable_after_failures,
            },
        }

    async def contract_orchestration_phase(
        self,
        session: aiohttp.ClientSession,
    ) -> dict[str, Any]:
        operator = derive_wallet(self.seed, "contract-orchestration-operator")
        spender = derive_wallet(self.seed, "contract-orchestration-spender")
        permit_spender = derive_wallet(
            self.seed, "contract-orchestration-permit-spender"
        )
        invalid_permit_spender = derive_wallet(
            self.seed, "contract-orchestration-invalid-permit-spender"
        )
        recipient = derive_wallet(self.seed, "contract-orchestration-recipient")
        permit_recipient = derive_wallet(
            self.seed, "contract-orchestration-permit-recipient"
        )
        duplicate_sender = derive_wallet(
            self.seed, "contract-orchestration-duplicate-sender"
        )
        duplicate_recipient = derive_wallet(
            self.seed, "contract-orchestration-duplicate-recipient"
        )
        await self.fund_wallets(
            session,
            [
                operator,
                spender,
                permit_spender,
                invalid_permit_spender,
                duplicate_sender,
            ],
            amount=20_000,
        )

        suffix = short_hash(f"{self.run_id}:orchestration")
        factory_name = f"con_orch_factory_{suffix}"
        router_name = f"con_orch_router_{suffix}"
        mid_name = f"con_orch_mid_{suffix}"
        root_name = f"con_orch_root_{suffix}"
        family_prefix = f"con_orch_family_{suffix}"
        failed_prefix = f"con_orch_fail_{suffix}"
        alpha_name = family_prefix + "_alpha"
        beta_name = family_prefix + "_beta"
        failed_good_name = failed_prefix + "_good"
        failed_bad_name = failed_prefix + "_bad"
        bad_artifact_name = f"con_orch_bad_artifact_{suffix}"

        factory_code = render_orchestration_factory_source()
        router_code = read_text(WORKLOADS_DIR / "e2e" / "orchestration_router.py")
        mid_code = read_text(WORKLOADS_DIR / "e2e" / "orchestration_mid.py")
        root_code = read_text(WORKLOADS_DIR / "e2e" / "orchestration_root.py")

        self.contracts.update(
            {
                "orchestration_factory": factory_name,
                "orchestration_router": router_name,
                "orchestration_mid": mid_name,
                "orchestration_root": root_name,
                "orchestrated_alpha": alpha_name,
                "orchestrated_beta": beta_name,
            }
        )

        async with self.client(operator, 1, session) as client:
            deployments = []
            for name, code in (
                (factory_name, factory_code),
                (router_name, router_code),
                (mid_name, mid_code),
                (root_name, root_code),
            ):
                existing_contract = await client.get_contract(name)
                if existing_contract is not None:
                    deployments.append(
                        {
                            "accepted": True,
                            "reused": True,
                            "contract": name,
                        }
                    )
                    continue
                submission = await client.submit_contract(
                    name=name,
                    **self.contract_submission_kwargs(name=name, code=code),
                    chi=CONTRACT_ORCHESTRATION_TX_CHI["deploy_contract"],
                    mode="commit",
                )
                deployments.append(
                    ensure_positive_submission(
                        submission,
                        label=f"deploy {name}",
                    )
                )

            alpha_existing = await client.get_contract(alpha_name)
            beta_existing = await client.get_contract(beta_name)
            if alpha_existing is not None and beta_existing is not None:
                family_receipt = {
                    "accepted": True,
                    "reused": True,
                    "label": "orchestration-deploy-family",
                }
            else:
                family_submission = await client.send_tx(
                    factory_name,
                    "deploy_family",
                    {"prefix": family_prefix},
                    chi=CONTRACT_ORCHESTRATION_TX_CHI["deploy_family"],
                    mode="commit",
                )
                family_receipt = ensure_positive_submission(
                    family_submission,
                    label="orchestration-deploy-family",
                )
            family_info = await client.call(
                factory_name,
                "get_last_family",
                {"prefix": family_prefix},
            )
            alpha_construct = await client.call(alpha_name, "get_construct_meta", {})
            beta_construct = await client.call(beta_name, "get_construct_meta", {})
            alpha_source = await client.get_contract(alpha_name)
            beta_source = await client.get_contract(beta_name)
            alpha_developer = await client.get_state(alpha_name, "__developer__")
            alpha_deployer = await client.get_state(alpha_name, "__deployer__")
            alpha_initiator = await client.get_state(alpha_name, "__initiator__")
            if alpha_source is None or beta_source is None:
                raise E2EError("factory-deployed child contracts were not persisted")
            if family_info["first"] != alpha_name or family_info["second"] != beta_name:
                raise E2EError("factory returned unexpected child contract names")
            if alpha_construct[0] != factory_name or beta_construct[0] != factory_name:
                raise E2EError("child constructor caller did not resolve to the factory contract")
            if alpha_construct[1] != operator.public_key:
                raise E2EError("child constructor signer drifted from the external caller")
            if alpha_construct[2] != alpha_name:
                raise E2EError("child constructor submission_name did not match the child contract")
            if (
                alpha_developer != factory_name
                or alpha_deployer != factory_name
                or alpha_initiator != operator.public_key
            ):
                raise E2EError("factory child deployment metadata is incorrect")

            touch_preview = await client.call(
                router_name,
                "dynamic_touch",
                {
                    "target_contract": alpha_name,
                    "function_name": "touch",
                    "account": operator.public_key,
                    "amount": 3,
                },
            )
            ping_preview = await client.call(
                router_name,
                "dynamic_ping_module",
                {
                    "target_contract": beta_name,
                    "label": "module-ping",
                },
            )

            if self.execution_mode == "xian_vm_v1":
                tampered_source = read_text(WORKLOADS_DIR / "e2e" / "patch_target.py")
                tampered_artifacts = build_deployment_artifacts(
                    bad_artifact_name,
                    tampered_source,
                )
                tampered_ir = json.loads(tampered_artifacts["vm_ir_json"])
                tampered_ir["module_name"] = f"{bad_artifact_name}_tampered"
                tampered_artifacts["vm_ir_json"] = json.dumps(
                    tampered_ir,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                tampered_artifacts["hashes"]["vm_ir_sha256"] = hashlib.sha256(
                    tampered_artifacts["vm_ir_json"].encode("utf-8")
                ).hexdigest()
                artifact_failure_receipt = ensure_failed_submission(
                    await client.submit_contract(
                        name=bad_artifact_name,
                        deployment_artifacts=tampered_artifacts,
                        chi=CONTRACT_ORCHESTRATION_TX_CHI["deploy_contract"],
                        wait_for_tx=True,
                    ),
                    label="orchestration-invalid-artifacts",
                )
                bad_artifact_source = await client.get_contract(bad_artifact_name)
                bad_artifact_absent = bad_artifact_source is None
                if not bad_artifact_absent:
                    raise E2EError(
                        "tampered deployment_artifacts unexpectedly persisted a contract"
                    )
            else:
                artifact_failure_receipt = {
                    "skipped": True,
                    "reason": "tampered deployment_artifacts are only enforced in xian_vm_v1",
                }
                bad_artifact_absent = True

        async with self.client(operator, 2, session) as client:
            touch_submission = await client.send_tx(
                router_name,
                "dynamic_touch",
                {
                    "target_contract": alpha_name,
                    "function_name": "touch",
                    "account": operator.public_key,
                    "amount": 3,
                },
                chi=CONTRACT_ORCHESTRATION_TX_CHI["dynamic_call"],
                mode="commit",
            )
            touch_receipt = ensure_positive_submission(
                touch_submission,
                label="orchestration-dynamic-touch",
            )
            alpha_touch_total = await client.call(alpha_name, "get_touch_total", {})

        async with self.client(operator, 3, session) as client:
            chain_preview = await client.call(
                root_name,
                "start",
                {
                    "router_contract": router_name,
                    "mid_contract": mid_name,
                    "leaf_contract": alpha_name,
                    "function_name": "describe",
                    "account": operator.public_key,
                    "amount": 7,
                },
            )

        expected_root_entry = f"{root_name}.start"
        router_before = chain_preview["nested"]["router_before"]
        router_after = chain_preview["nested"]["router_after"]
        mid_before = chain_preview["nested"]["nested"]["mid_before"]
        mid_after = chain_preview["nested"]["nested"]["mid_after"]
        leaf_ctx = chain_preview["nested"]["nested"]["leaf"]
        if chain_preview["root_ctx"]["caller"] != operator.public_key:
            raise E2EError("root caller drifted from the external signer")
        if router_before["caller"] != root_name or router_after["caller"] != root_name:
            raise E2EError("router caller drifted from the root contract")
        if mid_before["caller"] != router_name or mid_after["caller"] != router_name:
            raise E2EError("mid caller drifted from the router contract")
        if leaf_ctx["caller"] != mid_name:
            raise E2EError("leaf caller drifted from the mid contract")
        if leaf_ctx["signer"] != operator.public_key:
            raise E2EError("leaf signer drifted from the external signer")
        if leaf_ctx["entry"] != expected_root_entry:
            raise E2EError("leaf entry context did not preserve the root entrypoint")
        if alpha_touch_total != 3:
            raise E2EError("dynamic touch did not persist the expected leaf state")

        direct_allowance = 321
        direct_spend = 123
        permit_allowance = 222
        permit_spend = 111
        permit_deadline = (
            datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=10)
        ).strftime("%Y-%m-%d %H:%M:%S")
        permit_msg = construct_token_permit_message(
            token_contract="currency",
            owner=operator.public_key,
            spender=permit_spender.public_key,
            value=permit_allowance,
            deadline=permit_deadline,
            authorizer_contract="permit_authorizer",
            chain_id=self.network["chain_id"],
        )
        permit_signature = operator.sign_msg(permit_msg)

        async with self.client(operator, 0, session) as owner_client, self.client(
            spender, 1, session
        ) as spender_client, self.client(
            permit_spender, 2, session
        ) as permit_spender_client, self.client(
            invalid_permit_spender, 3, session
        ) as invalid_permit_client:
            direct_approve_receipt = ensure_positive_submission(
                await owner_client.send_tx(
                    "currency",
                    "approve",
                    {"amount": direct_allowance, "to": spender.public_key},
                    chi=DEFAULT_TX_CHI,
                    wait_for_tx=True,
                ),
                label="currency-direct-approve",
            )
            direct_allowance_state = await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract="currency",
                variable="approvals",
                keys=[operator.public_key, spender.public_key],
                expected=direct_allowance,
                label="direct allowance state",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )
            direct_transfer_receipt = ensure_positive_submission(
                await spender_client.send_tx(
                    "currency",
                    "transfer_from",
                    {
                        "amount": direct_spend,
                        "to": recipient.public_key,
                        "main_account": operator.public_key,
                    },
                    chi=DEFAULT_TRANSFER_CHI,
                    wait_for_tx=True,
                ),
                label="currency-direct-transfer-from",
            )
            direct_remaining_allowance = await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract="currency",
                variable="approvals",
                keys=[operator.public_key, spender.public_key],
                expected=direct_allowance - direct_spend,
                label="direct remaining allowance",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )
            direct_recipient_balance = await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract="currency",
                variable="balances",
                keys=[recipient.public_key],
                expected=direct_spend,
                label="direct transfer recipient balance",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )

            permit_receipt = ensure_positive_submission(
                await permit_spender_client.send_tx(
                    "permit_authorizer",
                    "permit",
                    {
                        "token_contract": "currency",
                        "owner": operator.public_key,
                        "spender": permit_spender.public_key,
                        "value": permit_allowance,
                        "deadline": permit_deadline,
                        "signature": permit_signature,
                    },
                    chi=DEFAULT_TX_CHI,
                    wait_for_tx=True,
                ),
                label="currency-permit-approve",
            )
            permit_allowance_state = await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract="currency",
                variable="approvals",
                keys=[operator.public_key, permit_spender.public_key],
                expected=permit_allowance,
                label="permit allowance state",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )
            permit_transfer_receipt = ensure_positive_submission(
                await permit_spender_client.send_tx(
                    "currency",
                    "transfer_from",
                    {
                        "amount": permit_spend,
                        "to": permit_recipient.public_key,
                        "main_account": operator.public_key,
                    },
                    chi=DEFAULT_TRANSFER_CHI,
                    wait_for_tx=True,
                ),
                label="currency-permit-transfer-from",
            )
            permit_remaining_allowance = await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract="currency",
                variable="approvals",
                keys=[operator.public_key, permit_spender.public_key],
                expected=permit_allowance - permit_spend,
                label="permit remaining allowance",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )
            permit_recipient_balance = await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract="currency",
                variable="balances",
                keys=[permit_recipient.public_key],
                expected=permit_spend,
                label="permit transfer recipient balance",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )

            expired_deadline = (
                datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
            ).strftime("%Y-%m-%d %H:%M:%S")
            expired_signature = operator.sign_msg(
                construct_token_permit_message(
                    token_contract="currency",
                    owner=operator.public_key,
                    spender=invalid_permit_spender.public_key,
                    value=77,
                    deadline=expired_deadline,
                    authorizer_contract="permit_authorizer",
                    chain_id=self.network["chain_id"],
                )
            )
            expired_permit_receipt = ensure_failed_submission(
                await invalid_permit_client.send_tx(
                    "permit_authorizer",
                    "permit",
                    {
                        "token_contract": "currency",
                        "owner": operator.public_key,
                        "spender": invalid_permit_spender.public_key,
                        "value": 77,
                        "deadline": expired_deadline,
                        "signature": expired_signature,
                    },
                    chi=DEFAULT_TX_CHI,
                    wait_for_tx=True,
                ),
                label="currency-permit-expired",
                expected_message_fragment="Permit has expired.",
            )
            expired_allowance_state = await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract="currency",
                variable="approvals",
                keys=[operator.public_key, invalid_permit_spender.public_key],
                expected=None,
                label="expired permit allowance remains zero",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )

            invalid_signature_deadline = (
                datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=10)
            ).strftime("%Y-%m-%d %H:%M:%S")
            wrong_signature = operator.sign_msg(
                construct_token_permit_message(
                    token_contract="currency",
                    owner=operator.public_key,
                    spender=permit_spender.public_key,
                    value=88,
                    deadline=invalid_signature_deadline,
                    authorizer_contract="permit_authorizer",
                    chain_id=self.network["chain_id"],
                )
            )
            invalid_signature_receipt = ensure_failed_submission(
                await invalid_permit_client.send_tx(
                    "permit_authorizer",
                    "permit",
                    {
                        "token_contract": "currency",
                        "owner": operator.public_key,
                        "spender": invalid_permit_spender.public_key,
                        "value": 88,
                        "deadline": invalid_signature_deadline,
                        "signature": wrong_signature,
                    },
                    chi=DEFAULT_TX_CHI,
                    wait_for_tx=True,
                ),
                label="currency-permit-invalid-signature",
                expected_message_fragment="Invalid signature.",
            )
            invalid_signature_allowance_state = await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract="currency",
                variable="approvals",
                keys=[operator.public_key, invalid_permit_spender.public_key],
                expected=None,
                label="invalid signature allowance remains zero",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )

            replay_permit_receipt = ensure_failed_submission(
                await permit_spender_client.send_tx(
                    "permit_authorizer",
                    "permit",
                    {
                        "token_contract": "currency",
                        "owner": operator.public_key,
                        "spender": permit_spender.public_key,
                        "value": permit_allowance,
                        "deadline": permit_deadline,
                        "signature": permit_signature,
                    },
                    chi=DEFAULT_TX_CHI,
                    wait_for_tx=True,
                ),
                label="currency-permit-replay",
                expected_message_fragment="Permit can only be used once.",
            )
            replay_allowance_state = await wait_for_uniform_node_state(
                session,
                self.nodes,
                contract="currency",
                variable="approvals",
                keys=[operator.public_key, permit_spender.public_key],
                expected=permit_allowance - permit_spend,
                label="replayed permit allowance unchanged",
                timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
            )

        duplicate_sender_balance_before = await fetch_abci_query(
            session,
            self.nodes[0].rpc_url,
            f"/get/currency.balances:{duplicate_sender.public_key}",
        )
        duplicate_recipient_balance_before = await fetch_abci_query(
            session,
            self.nodes[0].rpc_url,
            f"/get/currency.balances:{duplicate_recipient.public_key}",
        )
        duplicate_amount = 17
        duplicate_nonce_before = await tr.get_nonce_async(
            self.nodes[0].rpc_url,
            duplicate_sender.public_key,
            session=session,
        )
        duplicate_payload = {
            "chain_id": self.network["chain_id"],
            "contract": "currency",
            "function": "transfer",
            "kwargs": {"amount": duplicate_amount, "to": duplicate_recipient.public_key},
            "nonce": duplicate_nonce_before,
            "sender": duplicate_sender.public_key,
            "chi_supplied": DEFAULT_TRANSFER_CHI,
        }
        duplicate_tx = tr.create_tx(duplicate_payload, duplicate_sender)
        duplicate_tx_hash = hashlib.sha256(
            json.dumps(duplicate_tx).encode("utf-8")
        ).hexdigest().upper()

        def summarize_duplicate_broadcast(
            response: dict[str, Any],
            *,
            node: LocalnetNode,
        ) -> dict[str, Any]:
            result = response.get("result")
            error = response.get("error")
            tx_hash = None
            code = None
            log = None
            if isinstance(result, dict):
                tx_hash = result.get("hash")
                code = result.get("code")
                log = result.get("log")
            if log is None and isinstance(error, dict):
                log = error.get("data") or error.get("message")
            duplicate_marker = isinstance(log, str) and (
                "tx already exists in cache" in log.lower()
                or "tx already exists in mempool" in log.lower()
            )
            return {
                "node": node.moniker,
                "hash": tx_hash,
                "code": code,
                "log": log,
                "duplicate_marker": duplicate_marker,
                "raw": response,
            }

        duplicate_first_response = summarize_duplicate_broadcast(
            await tr.broadcast_tx_wait_async(
                self.nodes[0].rpc_url,
                duplicate_tx,
                session=session,
            ),
            node=self.nodes[0],
        )
        await asyncio.sleep(0.35)
        duplicate_second_response = summarize_duplicate_broadcast(
            await tr.broadcast_tx_wait_async(
                self.nodes[1].rpc_url,
                duplicate_tx,
                session=session,
            ),
            node=self.nodes[1],
        )
        async with self.client(duplicate_sender, 4, session) as duplicate_client:
            duplicate_receipt = normalize_value(
                (
                    await duplicate_client.wait_for_tx(
                        duplicate_tx_hash,
                        timeout_seconds=15.0,
                        poll_interval_seconds=0.25,
                    )
                ).raw
            )
        if duplicate_receipt.get("success") is not True:
            raise E2EError("duplicate submission tx did not finalize successfully")

        duplicate_recipient_expected = Decimal(
            str(duplicate_recipient_balance_before or 0)
        ) + Decimal(str(duplicate_amount))
        duplicate_recipient_balance_after = await wait_for_uniform_node_state(
            session,
            self.nodes,
            contract="currency",
            variable="balances",
            keys=[duplicate_recipient.public_key],
            expected=duplicate_recipient_expected,
            label="duplicate recipient balance changed once",
            timeout_seconds=min(self.args.rpc_timeout_seconds, 30.0),
        )
        duplicate_sender_balances = await query_state_from_all_nodes(
            session,
            self.nodes,
            contract="currency",
            variable="balances",
            keys=[duplicate_sender.public_key],
        )
        sender_balance_variants = {
            json.dumps(normalize_value(value), sort_keys=True)
            for value in duplicate_sender_balances.values()
        }
        if len(sender_balance_variants) != 1:
            raise E2EError("duplicate sender balance diverged across validators")
        duplicate_sender_balance_after = next(iter(duplicate_sender_balances.values()))
        if Decimal(str(duplicate_sender_balance_after)) >= Decimal(
            str(duplicate_sender_balance_before or 0)
        ):
            raise E2EError(
                "duplicate submission did not reduce the sender balance at all"
            )
        duplicate_nonce_after = await tr.get_nonce_async(
            self.nodes[2].rpc_url,
            duplicate_sender.public_key,
            session=session,
        )
        if duplicate_nonce_after != duplicate_nonce_before + 1:
            raise E2EError(
                "duplicate submission advanced the sender nonce by more than one"
            )

        async with self.client(operator, 4, session) as client:
            private_submission = await client.send_tx(
                router_name,
                "private_probe",
                {
                    "target_contract": alpha_name,
                    "function_name": "internal_secret",
                },
                chi=CONTRACT_ORCHESTRATION_TX_CHI["dynamic_call"],
                mode="commit",
            )
            private_receipt = normalize_receipt(
                private_submission,
                label="orchestration-private-probe",
            )
            if (
                private_receipt["accepted"] is not False
                and private_receipt["success"] is not False
            ):
                raise E2EError("private dynamic probe unexpectedly succeeded")

            failed_submission = await client.send_tx(
                factory_name,
                "deploy_family_with_failure",
                {"prefix": failed_prefix},
                chi=CONTRACT_ORCHESTRATION_TX_CHI["deploy_family"],
                mode="commit",
            )
            failed_receipt = normalize_receipt(
                failed_submission,
                label="orchestration-failed-family",
            )
            if (
                failed_receipt["accepted"] is not False
                and failed_receipt["success"] is not False
            ):
                raise E2EError("factory batch failure probe unexpectedly succeeded")
            failed_good_source = await client.get_contract_code(failed_good_name)
            failed_bad_source = await client.get_contract_code(failed_bad_name)
            if failed_good_source is not None or failed_bad_source is not None:
                raise E2EError("failed batch deployment left child contracts behind")

        return {
            "contracts": normalize_value(self.contracts),
            "deployments": deployments,
            "family_receipt": family_receipt,
            "family_info": normalize_value(family_info),
            "alpha_construct_meta": normalize_value(alpha_construct),
            "beta_construct_meta": normalize_value(beta_construct),
            "alpha_deployment_meta": {
                "developer": alpha_developer,
                "deployer": alpha_deployer,
                "initiator": alpha_initiator,
            },
            "touch_preview": normalize_value(touch_preview),
            "touch_receipt": touch_receipt,
            "alpha_touch_total": alpha_touch_total,
            "ping_preview": normalize_value(ping_preview),
            "private_receipt": private_receipt,
            "failed_receipt": failed_receipt,
            "failed_contracts_absent": {
                failed_good_name: failed_good_source is None,
                failed_bad_name: failed_bad_source is None,
            },
            "artifact_validation_path": {
                "invalid_artifact_receipt": artifact_failure_receipt,
                "tampered_contract_absent": bad_artifact_absent,
            },
            "chain_preview": normalize_value(chain_preview),
            "currency_allowance_path": {
                "direct_approve_receipt": direct_approve_receipt,
                "direct_allowance_state": direct_allowance_state,
                "direct_transfer_receipt": direct_transfer_receipt,
                "direct_remaining_allowance": direct_remaining_allowance,
                "direct_recipient_balance": direct_recipient_balance,
                "permit_receipt": permit_receipt,
                "permit_allowance_state": permit_allowance_state,
                "permit_transfer_receipt": permit_transfer_receipt,
                "permit_remaining_allowance": permit_remaining_allowance,
                "permit_recipient_balance": permit_recipient_balance,
                "expired_permit_receipt": expired_permit_receipt,
                "expired_allowance_state": expired_allowance_state,
                "invalid_signature_receipt": invalid_signature_receipt,
                "invalid_signature_allowance_state": invalid_signature_allowance_state,
                "replay_permit_receipt": replay_permit_receipt,
                "replay_allowance_state": replay_allowance_state,
            },
            "duplicate_submission_path": {
                "tx_hash": duplicate_tx_hash,
                "nonce_before": duplicate_nonce_before,
                "nonce_after": duplicate_nonce_after,
                "first_broadcast": normalize_value(duplicate_first_response),
                "second_broadcast": normalize_value(duplicate_second_response),
                "receipt": duplicate_receipt,
                "sender_balance_after": duplicate_sender_balance_after,
                "recipient_balance_after": duplicate_recipient_balance_after,
            },
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
                        chi=DEFAULT_TRANSFER_CHI,
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
        seed_label: str | None = None,
        counter_ops: int | None = None,
        dex_rounds: int | None = None,
    ) -> dict[str, Any]:
        workload_seed = self.seed if seed_label is None else f"{self.seed}:{seed_label}"
        cmd = [
            "uv",
            "run",
            "--project",
            str(ROOT_DIR / "xian-py"),
            "--python",
            CURRENT_UV_PYTHON,
            "python3",
            str(SCRIPT_DIR / "localnet-workload.py"),
            "--scenario",
            scenario,
            "--seed",
            workload_seed,
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

    async def wait_for_parallel_metadata_match(
        self,
        session: aiohttp.ClientSession,
        *,
        label: str,
        min_height: int | None,
        max_height: int | None,
        predicate,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_window: dict[str, list[dict[str, Any]]] = {}

        while time.monotonic() < deadline:
            statuses = await perf_status_from_all_nodes(session, self.nodes)
            matches: dict[str, dict[str, Any]] = {}
            last_window = {}

            for node in self.nodes:
                node_status = statuses[node.moniker]
                window = recent_blocks_in_window(
                    node_status,
                    min_height=min_height,
                    max_height=max_height,
                )
                last_window[node.moniker] = [
                    {
                        "height": block.get("height"),
                        "metadata": normalize_value(block.get("metadata", {})),
                    }
                    for block in window
                ]
                matched_block = next(
                    (
                        block
                        for block in window
                        if predicate(block.get("metadata") or {})
                    ),
                    None,
                )
                if matched_block is not None:
                    matches[node.moniker] = {
                        "height": int(matched_block["height"]),
                        "metadata": normalize_value(
                            matched_block.get("metadata", {})
                        ),
                    }

            if len(matches) == len(self.nodes):
                return {
                    "label": label,
                    "min_height": min_height,
                    "max_height": max_height,
                    "matches": matches,
                }

            await asyncio.sleep(0.5)

        raise E2EError(
            f"{label}: did not observe expected parallel metadata in perf window; "
            f"last={normalize_value(last_window)}"
        )

    async def burst_phase(self) -> dict[str, Any]:
        payload = await self.run_localnet_workload(
            scenario="counter_basic",
            seed_label=f"{self.run_id}:burst",
            counter_ops=self.args.burst_counter_ops,
        )
        tx_count = payload["scenario_summary"]["successful_transactions"]
        elapsed = float(payload["elapsed_seconds"])
        payload["approx_tps"] = round(tx_count / elapsed, 3)
        return payload

    async def conflict_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        conflict_contract = self.contracts["conflict"]
        wallets = [derive_wallet(self.seed, f"conflict-wallet-{index}") for index in range(2)]
        await self.fund_wallets(session, wallets, amount=5_000)

        async def claim_once(wallet: Wallet, node_index: int, slot: str, label: str):
            async with self.client(wallet, node_index, session) as client:
                submission = await client.send_tx(
                    conflict_contract,
                    "claim",
                    {"slot": slot, "amount": 1},
                    chi=DEFAULT_TX_CHI,
                    wait_for_tx=False,
                )
                receipt = normalize_receipt(submission, label=label)
                if submission.accepted and submission.tx_hash:
                    try:
                        finalized = await client.wait_for_tx(
                            submission.tx_hash,
                            timeout_seconds=8.0,
                            poll_interval_seconds=0.25,
                        )
                    except TxTimeoutError as exc:
                        receipt["timed_out"] = True
                        receipt["message"] = str(exc)
                        return receipt
                    execution = finalized.execution or {}
                    receipt["finalized"] = True
                    receipt["success"] = finalized.success
                    receipt["message"] = finalized.message
                    receipt["chi_used"] = execution.get("chi_used")
                    receipt["events"] = execution.get("events", []) or []
                    receipt["event_count"] = len(receipt["events"])
                    receipt["state_write_count"] = len(
                        execution.get("state", []) or []
                    )
                return receipt

        race_slot = f"race-{short_hash(self.run_id)}"
        race_results = await asyncio.gather(
            claim_once(wallets[0], 0, race_slot, "claim-a"),
            claim_once(wallets[1], 1, race_slot, "claim-b"),
        )
        successes = [item for item in race_results if item["success"] is True]
        non_applied = [item for item in race_results if item["success"] is not True]
        if len(successes) != 1 or len(non_applied) != 1:
            raise E2EError(
                "expected exactly one applied and one non-applied claim in conflict phase"
            )

        async with self.client(wallets[0], 2, session) as client:
            invalid = await client.send_tx(
                conflict_contract,
                "claim",
                {"slot": "", "amount": -1},
                chi=DEFAULT_TX_CHI,
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
            seed_label=f"{self.run_id}:dex",
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
        pair_id = self.contracts.get("dex_pair_id")
        if not all([dex_contract, token_a, pair_id]):
            raise E2EError("DEX contracts are not available for simulator phase")

        dex_workload_seed = f"{self.seed}:{self.run_id}:dex"
        sim_wallet = derive_wallet(dex_workload_seed, "dex-trader-0")
        deadline = {
            "__time__": [
                2099,
                1,
                1,
                0,
                0,
                0,
                0,
            ]
        }
        semaphore = asyncio.Semaphore(self.args.simulator_burst_concurrency)
        load_test_config = self.load_test_client_config()

        async def one_simulation(index: int) -> dict[str, Any]:
            node_index = index % len(self.nodes)
            async with semaphore:
                async with self.client(
                    sim_wallet,
                    node_index,
                    session,
                    config=load_test_config,
                ) as client:
                    started = time.monotonic()
                    result = await client.simulate(
                        dex_contract,
                        "swapExactTokenForToken",
                        {
                            "amountIn": 5.0 + index,
                            "amountOutMin": 1.0,
                            "pair": pair_id,
                            "src": token_a,
                            "to": sim_wallet.public_key,
                            "deadline": deadline,
                        },
                    )
                    elapsed = time.monotonic() - started
                    return {
                        "node": self.nodes[node_index].moniker,
                        "elapsed_ms": round(elapsed * 1000, 2),
                        "status": result.get("status"),
                        "result": normalize_value(result.get("result")),
                    }

        baseline = await asyncio.gather(
            *(one_simulation(index) for index in range(len(self.nodes)))
        )
        baseline_failures = [
            item for item in baseline if item["status"] not in (None, 0)
        ]
        if baseline_failures:
            raise E2EError("baseline simulator checks failed before burst load")

        started = time.monotonic()
        raw_responses = await asyncio.gather(
            *(one_simulation(index) for index in range(SIMULATOR_BURST_REQUESTS)),
            return_exceptions=True,
        )
        elapsed = time.monotonic() - started
        responses = []
        transport_failures = []
        for index, item in enumerate(raw_responses):
            if isinstance(item, Exception):
                transport_failures.append(
                    {
                        "request": index,
                        "node": self.nodes[index % len(self.nodes)].moniker,
                        "error_type": item.__class__.__name__,
                        "error": str(item),
                    }
                )
                continue
            responses.append(item)
        if transport_failures:
            raise E2EError(
                "simulator burst encountered transport failures after retries: "
                + json.dumps(transport_failures[:5], sort_keys=True)
            )
        failures = [
            item for item in responses if item["status"] not in (None, 0)
        ]
        successes = [
            item for item in responses if item["status"] in (None, 0)
        ]
        allowed_failure_markers = (
            "Simulation capacity exceeded on this node; retry later",
            "Simulation timed out on this node after",
        )
        unexpected_failures = [
            item
            for item in failures
            if not any(
                marker in str(item.get("result", ""))
                for marker in allowed_failure_markers
            )
        ]
        if unexpected_failures:
            raise E2EError(
                f"simulator phase had {len(unexpected_failures)} unexpected failures"
            )
        if not successes:
            raise E2EError("simulator phase had no successful simulations under load")

        recovery = await one_simulation(SIMULATOR_BURST_REQUESTS + 1)
        if recovery["status"] not in (None, 0):
            raise E2EError("simulator phase did not recover after burst load")
        return {
            "request_count": len(responses),
            "elapsed_seconds": round(elapsed, 3),
            "approx_qps": round(len(responses) / elapsed, 3),
            "success_count": len(successes),
            "failure_count": len(failures),
            "transport_failure_count": len(transport_failures),
            "baseline_sample": baseline[:4],
            "failure_sample": failures[:8],
            "success_sample": successes[:8],
            "recovery": recovery,
        }

    async def bds_catchup_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        service = self.service_node
        if service is None:
            raise E2EError("service node not available for BDS catch-up phase")

        async def post_graphql(
            *,
            json_body: dict[str, Any] | None = None,
            raw_body: str | None = None,
        ) -> dict[str, Any]:
            request_kwargs: dict[str, Any] = {"timeout": 5.0}
            if json_body is not None:
                request_kwargs["json"] = json_body
            else:
                request_kwargs["data"] = raw_body or ""
                request_kwargs["headers"] = {
                    "Content-Type": "application/json",
                }
            async with session.post(
                f"{service.rpc_url}/graphql",
                **request_kwargs,
            ) as response:
                body = await response.text()
            return {
                "status": response.status,
                "body": body[:200],
            }

        async def bds_query_pressure(
            client: XianAsync,
            *,
            rounds: int,
        ) -> dict[str, Any]:
            stats: dict[str, dict[str, Any]] = {}

            async def record(name: str, coro_factory) -> None:
                bucket = stats.setdefault(
                    name,
                    {"success": 0, "failure": 0, "samples": []},
                )
                try:
                    result = await coro_factory()
                except Exception as exc:  # noqa: BLE001
                    bucket["failure"] += 1
                    if len(bucket["samples"]) < 3:
                        bucket["samples"].append(
                            {"ok": False, "error": str(exc)}
                        )
                    return

                bucket["success"] += 1
                if len(bucket["samples"]) < 3:
                    if isinstance(result, list):
                        sample_result = [
                            normalize_value(getattr(item, "raw", item))
                            for item in result[:3]
                        ]
                    else:
                        sample_result = normalize_value(
                            getattr(result, "raw", result)
                        )
                    bucket["samples"].append(
                        {"ok": True, "result": sample_result}
                    )

            invalid_tx_hash = "0" * 64
            for _ in range(rounds):
                await asyncio.gather(
                    record(
                        "list_blocks",
                        lambda: client.list_blocks(limit=3, offset=0),
                    ),
                    record(
                        "list_events",
                        lambda: client.list_events(
                            self.contracts["conflict"],
                            "Claimed",
                            limit=3,
                        ),
                    ),
                    record(
                        "state_history",
                        lambda: client.get_state_history(
                            f"{self.contracts['conflict']}.counter",
                            limit=3,
                            offset=0,
                        ),
                    ),
                    record(
                        "events_for_unknown_tx",
                        lambda: client.get_events_for_tx(invalid_tx_hash),
                    ),
                    record(
                        "state_for_unknown_tx",
                        lambda: client.get_state_for_tx(invalid_tx_hash),
                    ),
                    record(
                        "malformed_graphql_query",
                        lambda: post_graphql(
                            json_body={"query": "query { broken("}
                        ),
                    ),
                    record(
                        "invalid_graphql_json",
                        lambda: post_graphql(raw_body="{not-json"),
                    ),
                )
                await asyncio.sleep(0.2)

            return stats

        env = make_localnet_env(self.args)
        current_height = await latest_height(session, service.rpc_url)
        catchup_wallets = [
            derive_wallet(self.seed, f"bds-catchup-{index}")
            for index in range(len(self.nodes))
        ]
        await self.fund_wallets(session, catchup_wallets, amount=5_000)

        secondary_bds: dict[str, Any] | None = None
        try:
            async with self.client(self.founder_wallet, service.index, session) as client:
                baseline_status = await wait_for_bds_indexed(
                    client,
                    target_height=current_height,
                    timeout_seconds=30.0,
                )

            run_localnet_compose("stop", LOCALNET_POSTGRES_SERVICE, env=env)
            stopped_state = await wait_for_container_state(
                LOCALNET_POSTGRES_CONTAINER,
                expected_states={"exited"},
                timeout_seconds=30.0,
            )

            catchup_records = []
            for index, wallet in enumerate(catchup_wallets * 2):
                node_index = index % len(self.nodes)
                slot = f"bds-catchup-{short_hash(f'{self.run_id}:{index}')}"
                async with self.client(wallet, node_index, session) as client:
                    submission = await client.send_tx(
                        self.contracts["conflict"],
                        "claim",
                        {"slot": slot, "amount": 1},
                        chi=DEFAULT_TX_CHI,
                        wait_for_tx=True,
                    )
                    receipt = ensure_positive_submission(
                        submission,
                        label=f"bds-catchup-claim-{index}",
                    )
                    receipt["slot"] = slot
                    catchup_records.append(receipt)

            catchup_height = await latest_height(session, service.rpc_url)

            async with self.client(self.founder_wallet, service.index, session) as client:
                outage_pressure_task = asyncio.create_task(
                    bds_query_pressure(client, rounds=12)
                )
                lagged_status = await wait_for_bds_backlog(
                    client,
                    target_height=catchup_height,
                    timeout_seconds=30.0,
                )
                outage_pressure = await outage_pressure_task
                for name in ("list_blocks", "list_events", "state_history"):
                    if outage_pressure.get(name, {}).get("failure", 0) == 0:
                        raise E2EError(
                            f"BDS outage pressure did not surface a failure for {name}"
                        )

            run_localnet_compose("start", LOCALNET_POSTGRES_SERVICE, env=env)
            healthy_state = await wait_for_container_state(
                LOCALNET_POSTGRES_CONTAINER,
                expected_states={"healthy"},
                timeout_seconds=45.0,
            )

            async with self.client(self.founder_wallet, service.index, session) as client:
                recovery_pressure_task = asyncio.create_task(
                    bds_query_pressure(client, rounds=40)
                )
                recovered_status = await wait_for_bds_recovered(
                    client,
                    target_height=catchup_height,
                    timeout_seconds=120.0,
                )
                recovery_pressure = await recovery_pressure_task
                for name in ("list_blocks", "list_events", "state_history"):
                    if recovery_pressure.get(name, {}).get("success", 0) == 0:
                        raise E2EError(
                            f"BDS recovery pressure never recovered successful {name} reads"
                        )
                indexed_txs = []
                for record in catchup_records[-3:]:
                    indexed_tx = await client.get_indexed_tx(record["tx_hash"])
                    if indexed_tx is None:
                        raise E2EError("BDS catch-up did not index a lagged transaction")
                    indexed_txs.append(normalize_value(indexed_tx.raw))
                indexed_events = await client.get_events_for_tx(catchup_records[-1]["tx_hash"])
                indexed_state = await client.get_state_for_tx(catchup_records[-1]["tx_hash"])
                if not indexed_events or not indexed_state:
                    raise E2EError("BDS catch-up did not restore indexed tx details")

            secondary_start = await self.start_secondary_bds_postgres()
            initial_secondary_sync = self.run_secondary_bds_reindex(
                source_node=service,
                reset=True,
            )
            secondary_initial_height = int(
                self.query_secondary_bds_scalar(
                    "SELECT COALESCE(MAX(height), 0) FROM blocks;"
                )
                or "0"
            )
            if secondary_initial_height < catchup_height:
                raise E2EError(
                    "secondary BDS initial sync did not reach the current chain height"
                )

            secondary_outage_state = await self.stop_secondary_bds_postgres()

            secondary_catchup_records = []
            for index, wallet in enumerate(catchup_wallets[:3]):
                node_index = (index + 1) % len(self.nodes)
                slot = f"secondary-bds-{short_hash(f'{self.run_id}:{index}')}"
                async with self.client(wallet, node_index, session) as client:
                    submission = await client.send_tx(
                        self.contracts["conflict"],
                        "claim",
                        {"slot": slot, "amount": 1},
                        chi=DEFAULT_TX_CHI,
                        wait_for_tx=True,
                    )
                    receipt = ensure_positive_submission(
                        submission,
                        label=f"secondary-bds-claim-{index}",
                    )
                    receipt["slot"] = slot
                    secondary_catchup_records.append(receipt)

            secondary_target_height = await latest_height(session, service.rpc_url)
            secondary_restarted_state = await self.restart_secondary_bds_postgres()
            secondary_resume_sync = self.run_secondary_bds_reindex(
                source_node=service,
                reset=False,
            )
            secondary_resumed_height = int(
                self.query_secondary_bds_scalar(
                    "SELECT COALESCE(MAX(height), 0) FROM blocks;"
                )
                or "0"
            )
            if secondary_resumed_height < secondary_target_height:
                raise E2EError(
                    "secondary BDS resume sync did not catch up to the latest height"
                )
            latest_secondary_tx_hash = secondary_catchup_records[-1]["tx_hash"]
            secondary_tx_count = int(
                self.query_secondary_bds_scalar(
                    "SELECT COUNT(*) FROM transactions "
                    f"WHERE hash = '{latest_secondary_tx_hash}';"
                )
                or "0"
            )
            if secondary_tx_count == 0:
                raise E2EError(
                    "secondary BDS did not index the lagged transaction after restart"
                )

            secondary_bds = {
                "start": secondary_start,
                "initial_sync": initial_secondary_sync,
                "initial_indexed_height": secondary_initial_height,
                "stopped_state": secondary_outage_state,
                "resume_sync": secondary_resume_sync,
                "restarted_state": secondary_restarted_state,
                "resumed_indexed_height": secondary_resumed_height,
                "target_height": secondary_target_height,
                "lagged_tx_hashes": [
                    record["tx_hash"] for record in secondary_catchup_records
                ],
                "lagged_receipts": secondary_catchup_records,
                "lagged_tx_count": secondary_tx_count,
            }

            self.sample_event_tx_hash = catchup_records[-1]["tx_hash"]
            return {
                "postgres_stopped_state": stopped_state,
                "postgres_recovered_state": healthy_state,
                "baseline_status": baseline_status,
                "lagged_status": lagged_status,
                "recovered_status": recovered_status,
                "outage_query_pressure": normalize_value(outage_pressure),
                "recovery_query_pressure": normalize_value(recovery_pressure),
                "catchup_height": catchup_height,
                "catchup_tx_hashes": [
                    record["tx_hash"] for record in catchup_records
                ],
                "catchup_receipts": catchup_records,
                "indexed_tx_sample": indexed_txs,
                "indexed_events_for_last_tx": normalize_value(
                    [item.raw for item in indexed_events]
                ),
                "indexed_state_for_last_tx": normalize_value(
                    [item.raw for item in indexed_state]
                ),
                "secondary_bds": normalize_value(secondary_bds),
            }
        finally:
            self.cleanup_secondary_bds_postgres()

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
            keys_page_one = await fetch_abci_query(
                session,
                service.rpc_url,
                f"/keys/{self.contracts['conflict']}.claims/limit=2",
            )
            if not isinstance(keys_page_one, dict):
                raise E2EError("/keys pagination did not return an object")
            if len(keys_page_one.get("items", [])) != 2:
                raise E2EError("/keys first page did not respect the requested limit")
            next_after = None
            next_after = keys_page_one.get("next_after")
            keys_page_two = (
                await fetch_abci_query(
                    session,
                    service.rpc_url,
                    f"/keys/{self.contracts['conflict']}.claims/limit=2/after={next_after}",
                )
                if next_after
                else None
            )
            if keys_page_one.get("has_more") and not next_after:
                raise E2EError("/keys response reported more results without a cursor")
            if next_after and (
                not isinstance(keys_page_two, dict)
                or len(keys_page_two.get("items", [])) == 0
            ):
                raise E2EError("/keys pagination did not return the follow-up page")
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
            await self.fund_wallets(session, [trigger_wallet], amount=5_000)
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
            "keys_page_one": normalize_value(keys_page_one),
            "keys_page_two": normalize_value(keys_page_two),
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

        async def wait_for_deterministic_sample(
            *,
            contract: str,
            variable: str,
            label: str,
            keys: list[str] | None = None,
        ) -> dict[str, Any]:
            try:
                await wait_for_uniform_state(
                    fetch_values=lambda: query_state_from_all_nodes(
                        session,
                        self.nodes,
                        contract=contract,
                        variable=variable,
                        keys=keys,
                    ),
                    fetch_heights=lambda: latest_heights(session, self.nodes),
                    label=label,
                    normalize_value=normalize_value,
                    timeout_seconds=20.0,
                    poll_interval_seconds=0.5,
                )
            except RuntimeError as exc:
                raise E2EError(str(exc)) from exc

            return await query_state_from_all_nodes(
                session,
                self.nodes,
                contract=contract,
                variable=variable,
                keys=keys,
            )

        counter_state = await wait_for_deterministic_sample(
            contract=conflict_contract,
            variable="counter",
            label="conflict counter",
        )
        patch_mode_state = await wait_for_deterministic_sample(
            contract=patch_contract,
            variable="mode",
            label="patch target mode",
        )
        orchestration_touch_state = None
        orchestrated_alpha = self.contracts.get("orchestrated_alpha")
        if orchestrated_alpha:
            orchestration_touch_state = await wait_for_deterministic_sample(
                contract=orchestrated_alpha,
                variable="touch_total",
                label="orchestrated alpha touch total",
            )

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
                            "chi_used": result.get("chi_used"),
                            "result": normalize_value(result.get("result")),
                        }
                    )

            comparison_keys = {
                json.dumps(
                    {
                        "status": item["status"],
                        "chi_used": item["chi_used"],
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
                "orchestrated_alpha_touch_total": orchestration_touch_state,
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
            expected_slot = None
            if trigger_tx_coro is not None:
                trigger_payload = await trigger_tx_coro
                if isinstance(trigger_payload, dict):
                    trigger_tx_hash = trigger_payload.get("tx_hash")
                    expected_slot = trigger_payload.get("slot")
                else:
                    trigger_tx_hash = trigger_payload
            if trigger_tx_hash is None:
                raise E2EError("websocket_tx_event requires a trigger tx hash")
            expected_tx_hash = trigger_tx_hash.lower()
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
                if isinstance(tx_hash, str) and tx_hash.lower() == expected_tx_hash:
                    return payload
                execution = decode_websocket_tx_execution(payload)
                if execution is None:
                    continue
                execution_hash = execution.get("hash")
                if (
                    isinstance(execution_hash, str)
                    and execution_hash.lower() == expected_tx_hash
                ):
                    return payload
                if expected_slot is None:
                    continue
                events = execution.get("events") or []
                for event in events:
                    data_indexed = event.get("data_indexed") or {}
                    if data_indexed.get("slot") == expected_slot:
                        return payload
        raise E2EError("websocket did not emit the expected tx event")

    async def _send_retrieval_trigger(
        self,
        session: aiohttp.ClientSession,
        trigger_wallet: Wallet,
    ) -> dict[str, str]:
        slot = f"retrieval-{short_hash(f'{self.run_id}:{time.time_ns()}')}"
        async with self.client(trigger_wallet, 0, session) as trigger_client:
            trigger = await trigger_client.send_tx(
                self.contracts["conflict"],
                "claim",
                {"slot": slot, "amount": 1},
                chi=DEFAULT_TX_CHI,
                wait_for_tx=True,
            )
            trigger_receipt = ensure_positive_submission(
                trigger,
                label="retrieval-trigger",
            )
            return {"tx_hash": trigger_receipt["tx_hash"], "slot": slot}

    async def validator_governance_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        (
            node0_wallet,
            node1_wallet,
            node2_wallet,
            node3_wallet,
            node4_wallet,
        ) = self.validator_wallets
        node3_key = self.nodes[3].account_public_key
        node4 = self.nodes[4]
        await self.fund_wallets(
            session,
            [node1_wallet, node2_wallet, node3_wallet, node4_wallet],
            amount=500_000,
        )

        async with self.client(node0_wallet, 0, session) as node0, self.client(
            node1_wallet, 1, session
        ) as node1, self.client(node2_wallet, 2, session) as node2, self.client(
            node3_wallet, 3, session
        ) as node3:
            node4_stop = await self.stop_node_runtime(node4)
            power_vote = await self.approve_members_vote(
                node0,
                [
                    ("node1", node1),
                    ("node2", node2),
                    ("node3", node3),
                ],
                type_of_vote="set_member_power",
                arg={"member": node3_key, "power": 15},
                label_prefix="set-power",
            )
            power_receipt = power_vote["proposal_receipt"]

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
            node4_restart = await self.start_node_runtime(
                session,
                node4,
                target_height=power_wait_height,
            )
            async with self.client(node4_wallet, 4, session) as restarted_node4:
                node4_power_record = await restarted_node4.call(
                    "masternodes",
                    "get_validator",
                    {"account": node3_key},
                )
            if node4_power_record["power"] != 15:
                raise E2EError(
                    "restarted validator did not converge to the updated member power"
                )

            async with self.client(node4_wallet, 4, session) as node4_client:
                remove_vote = await self.approve_members_vote(
                    node0,
                    [
                        ("node1", node1),
                        ("node2", node2),
                        ("node3", node3),
                        ("node4", node4_client),
                    ],
                    type_of_vote="remove_member",
                    arg=node3_key,
                    label_prefix="remove-member",
                )
                remove_receipt = remove_vote["proposal_receipt"]

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
                if len(validators_after_remove["result"]["validators"]) != 4:
                    raise E2EError("validator removal did not reduce the validator set to 4")

                registration_fee = await node0.get_state("masternodes", "registration_fee")
                approval_submission = await node3.send_tx(
                    "currency",
                    "approve",
                    {"amount": registration_fee, "to": "masternodes"},
                    chi=DEFAULT_TX_CHI,
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
                    chi=GOVERNANCE_TX_CHI,
                    wait_for_tx=True,
                )
                ensure_positive_submission(register_submission, label="re-register-node3")
                add_vote = await self.approve_members_vote(
                    node0,
                    [
                        ("node1", node1),
                        ("node2", node2),
                        ("node4", node4_client),
                    ],
                    type_of_vote="add_member",
                    arg=node3_key,
                    label_prefix="add-member",
                )
                add_receipt = add_vote["proposal_receipt"]

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
                if len(validators_after_add["result"]["validators"]) != 5:
                    raise E2EError("validator add-back did not restore the validator set to 5")
                validator_record = await node0.call(
                    "masternodes",
                    "get_validator",
                    {"account": node3_key},
                )

        return {
            "node4_outage_during_power_vote": {
                "stop": normalize_value(node4_stop),
                "restart": normalize_value(node4_restart),
                "node4_power_record": normalize_value(node4_power_record),
            },
            "power_change": power_receipt,
            "power_vote": normalize_value(power_vote),
            "power_record": normalize_value(power_record),
            "remove_receipt": remove_receipt,
            "remove_vote": normalize_value(remove_vote),
            "re_register_approval": approval_receipt,
            "add_receipt": add_receipt,
            "add_vote": normalize_value(add_vote),
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
        governance_min_patch_delay = await fetch_abci_query(
            session,
            service.rpc_url,
            "/get/governance.metadata:min_patch_delay_blocks",
        )
        if governance_min_patch_delay is None:
            governance_min_patch_delay = STATE_PATCH_DELAY_BLOCKS
        activation_height = current_height + max(
            int(governance_min_patch_delay) + STATE_PATCH_ACTIVATION_HEADROOM_BLOCKS,
            STATE_PATCH_DELAY_BLOCKS,
        )
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

        existing_patch: dict[str, Any] | None = None
        async with self.client(founder, service.index, session) as client:
            try:
                existing_patch = await client.call(
                    "governance",
                    "get_patch",
                    {"patch_id": patch_id},
                )
            except SimulationError:
                existing_patch = None

        if existing_patch is not None:
            activation_height = int(existing_patch["activation_height"])
            bundle_payload["activation_height"] = activation_height
            bundle_payload["bundle_hash"] = compute_patch_bundle_hash(bundle_payload)
            if bundle_payload["bundle_hash"] != existing_patch["bundle_hash"]:
                raise E2EError(
                    "existing state patch bundle hash does not match the harness payload"
                )

        for node in self.nodes:
            patch_dir = STACK_DIR / ".localnet" / node.moniker / ".cometbft" / "config" / "state-patches"
            patch_dir.mkdir(parents=True, exist_ok=True)
            (patch_dir / f"{patch_id}.json").write_text(
                json.dumps(bundle_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        await self.restart_localnet_and_wait_ready(session)

        (
            node0_wallet,
            node1_wallet,
            node2_wallet,
            node3_wallet,
            node4_wallet,
        ) = self.validator_wallets
        node4 = self.nodes[4]
        proposal_receipt: dict[str, Any] | None = None
        if existing_patch is None:
            async with self.client(node0_wallet, 0, session) as node0, self.client(
                node1_wallet, 1, session
            ) as node1, self.client(node2_wallet, 2, session) as node2, self.client(
                node3_wallet, 3, session
            ) as node3, self.client(node4_wallet, 4, session) as node4_client:
                for client in (node0, node1, node2, node3, node4_client):
                    await client.refresh_nonce()
                proposal_vote = await self.approve_governance_proposal(
                    node0,
                    [
                        ("node1", node1),
                        ("node2", node2),
                        ("node3", node3),
                        ("node4", node4_client),
                    ],
                    proposal_function="propose_state_patch",
                    proposal_kwargs={
                        "patch_id": patch_id,
                        "bundle_hash": bundle_payload["bundle_hash"],
                        "activation_height": activation_height,
                        "summary": bundle_payload["summary"],
                        "uri": bundle_payload["uri"],
                        "emergency": False,
                    },
                    expected_final_status="approved",
                    label_prefix="state-patch",
                )
                proposal_receipt = proposal_vote["proposal_receipt"]
        else:
            proposal_vote = None

        node4_stop = await self.stop_node_runtime(node4)

        current_height = int(
            (
                await fetch_json(
                    session,
                    f"{self.nodes[0].rpc_url}/status",
                    timeout=5.0,
                )
            )["result"]["sync_info"]["latest_block_height"]
        )
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
        async with self.client(founder, service.index, session) as client:
            indexed_status = await wait_for_bds_indexed(
                client,
                target_height=activation_height,
                timeout_seconds=60.0,
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
        node4_restart = await self.start_node_runtime(
            session,
            node4,
            target_height=activation_height + 1,
        )
        async with self.client(node4_wallet, 4, session) as restarted_node4:
            restarted_patch_status = await restarted_node4.call(
                "governance",
                "get_patch",
                {"patch_id": patch_id},
            )
            restarted_contract_status = await restarted_node4.call(
                self.contracts["patch_target"],
                "get_status",
                {},
            )
        if normalize_value(restarted_contract_status) != normalize_value(contract_status):
            raise E2EError(
                "restarted validator did not converge to the activated state patch state"
            )

        return {
            "bundle": bundle_payload,
            "existing_patch": normalize_value(existing_patch),
            "node4_outage_during_activation": {
                "stop": normalize_value(node4_stop),
                "restart": normalize_value(node4_restart),
                "governance_patch": normalize_value(restarted_patch_status),
                "patch_target_status": normalize_value(restarted_contract_status),
            },
            "governance_min_patch_delay": governance_min_patch_delay,
            "activation_wait_timeout_seconds": activation_wait_timeout,
            "proposal_receipt": proposal_receipt,
            "proposal_vote": normalize_value(proposal_vote) if existing_patch is None else None,
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
        await self.restart_localnet_and_wait_ready(session)

        trigger_wallet = derive_wallet(self.seed, "logging-trigger")
        rejected_wallet = derive_wallet(self.seed, "logging-checktx-reject")
        await self.fund_wallets(session, [trigger_wallet], amount=5_000)
        async with self.client(rejected_wallet, 0, session) as client:
            rejected_submission = await client.send(
                amount=1,
                to_address=self.founder_wallet.public_key,
                chi=DEFAULT_TRANSFER_CHI,
                wait_for_tx=True,
            )
            if rejected_submission.accepted:
                raise E2EError(
                    "logging checktx rejection probe unexpectedly succeeded"
                )
        async with self.client(trigger_wallet, 0, session) as client:
            debug_submission = await client.send(
                amount=1,
                to_address=self.founder_wallet.public_key,
                chi=DEFAULT_TRANSFER_CHI,
                wait_for_tx=True,
            )
            debug_receipt = ensure_positive_submission(debug_submission, label="debug-log-trigger")

        update_logging_config(level="TRACE", trace_logging=True, json_logging=False)
        await self.restart_localnet_and_wait_ready(session)

        async with self.client(trigger_wallet, 0, session) as client:
            trace_submission = await client.send(
                amount=1,
                to_address=self.founder_wallet.public_key,
                chi=DEFAULT_TRANSFER_CHI,
                wait_for_tx=True,
            )
            trace_receipt = ensure_positive_submission(trace_submission, label="trace-log-trigger")

        debug_lines: dict[str, list[str]] = {}
        checktx_lines: dict[str, list[str]] = {}
        trace_lines: dict[str, list[str]] = {}
        for node in self.nodes:
            logs = local_log_paths(node)
            if not logs:
                raise E2EError(f"no logs found for {node.moniker}")
            recent_logs = logs[-3:]
            text = "\n".join(
                path.read_text(encoding="utf-8") for path in recent_logs
            )
            debug_lines[node.moniker] = [
                line
                for line in text.splitlines()
                if "stage=execute_tx" in line
            ][-3:]
            checktx_lines[node.moniker] = [
                line
                for line in text.splitlines()
                if rejected_wallet.public_key in line and "stage=check_tx" in line
            ][-3:]
            trace_lines[node.moniker] = [
                line
                for line in text.splitlines()
                if "stage=finalize_tx_result" in line
            ][-3:]
            if not debug_lines[node.moniker]:
                raise E2EError(
                    f"DEBUG logs missing stage=execute_tx for {node.moniker}"
                )
            if not trace_lines[node.moniker]:
                raise E2EError(
                    f"TRACE logs missing stage=finalize_tx_result for {node.moniker}"
                )
        if not any(checktx_lines.values()):
            raise E2EError("WARNING logs missing stage=check_tx rejection probe")

        update_logging_config(level=self.args.log_level, trace_logging=False, json_logging=False)
        await self.restart_localnet_and_wait_ready(session)

        return {
            "initial_logs": initial_logs,
            "rejected_submission": rejected_submission,
            "debug_receipt": debug_receipt,
            "trace_receipt": trace_receipt,
            "checktx_matches": checktx_lines,
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
        service = self.service_node or self.nodes[0]
        alice = derive_wallet(self.seed, "shielded-alice")
        bob = derive_wallet(self.seed, "shielded-bob")
        relayer = derive_wallet(self.seed, "shielded-relayer")

        registry_name = "zk_registry"
        base_token_name = f"con_private_e2e_{short_hash(self.run_id)}"
        token_name = base_token_name
        shielded_wallet_balance_target = 1_000_000
        vk_registration_proposals: list[dict[str, Any]] = []
        vk_infos: dict[str, dict[str, Any]] = {}
        vk_bindings: dict[str, dict[str, Any]] = {}

        async def indexed_note_records(
            client: XianAsync,
            *,
            minimum_count: int,
            timeout_seconds: float = 60.0,
        ) -> list[dict[str, Any]]:
            deadline = time.monotonic() + timeout_seconds
            latest_records = []
            while True:
                txs = []
                offset = 0
                while True:
                    page = await client.list_txs_by_contract(
                        token_name,
                        limit=100,
                        offset=offset,
                    )
                    if not page:
                        break
                    txs.extend(page)
                    if len(page) < 100:
                        break
                    offset += len(page)

                latest_records = note_records_from_transactions(txs)
                if len(latest_records) >= minimum_count:
                    return latest_records
                if time.monotonic() >= deadline:
                    return latest_records
                await asyncio.sleep(0.5)

        def normalize_note_records(records) -> list[dict[str, Any]]:
            return [
                {
                    "index": record.index,
                    "commitment": record.commitment,
                    "payload": record.payload,
                    "payload_hash": record.payload_hash,
                    "created_at": record.created_at,
                }
                for record in records
            ]

        async with self.client(founder, 0, session) as client:
            for wallet in (alice, bob, relayer):
                current_balance = await client.get_balance(wallet.public_key)
                delta = shielded_wallet_balance_target - int(current_balance)
                if delta <= 0:
                    continue
                funding = await client.send(
                    amount=delta,
                    to_address=wallet.public_key,
                    chi=DEFAULT_TRANSFER_CHI,
                    wait_for_tx=True,
                )
                ensure_positive_submission(
                    funding,
                    label=f"shielded-topup-{wallet.public_key[:12]}",
                )
            # The shielded deployment is large enough that we want a fresh nonce
            # from chain state instead of carrying the local reservation window
            # across the funding sequence.
            await client.refresh_nonce()
            registry_owner = await client.call(registry_name, "owner", {})
            if registry_owner != "governance":
                raise E2EError(
                    f"expected system zk_registry owner to be governance, got {registry_owner!r}"
                )
            token_source = await client.get_contract(token_name)
            token_suffix = 1
            while token_source is not None:
                token_name = f"{base_token_name}_{token_suffix}"
                token_source = await client.get_contract(token_name)
                token_suffix += 1
            if token_source is None:
                token_submission = await client.submit_contract(
                    name=token_name,
                    **self.contract_submission_kwargs(
                        name=token_name,
                        code=read_text(
                            CONTRACTS_DIR
                            / "shielded-note-token"
                            / "src"
                            / "con_shielded_note_token.py"
                        ),
                    ),
                    args={
                        "token_name": "Local Private USD",
                        "token_symbol": "lpUSD",
                        "operator_address": founder.public_key,
                        "root_window_size": 32,
                    },
                    chi=25_000_000,
                    wait_for_tx=True,
                )
                ensure_positive_submission(
                    token_submission,
                    label="deploy-shielded-token",
                )
            prover = ShieldedNoteProver.build_insecure_dev_bundle()
            relay_prover = ShieldedRelayTransferProver.build_insecure_dev_bundle()
            registry_manifest = shielded_registry_manifest(prover)
            relay_registry_manifest = shielded_relay_registry_manifest(relay_prover)
            proof_config = await client.call(token_name, "get_proof_config", {})
            relay_proof_config = await client.call(
                token_name,
                "get_relay_proof_config",
                {},
            )
            alice_public_balance = await client.call(
                token_name,
                "balance_of",
                {"address": alice.public_key},
            )
            mint_amount = max(100 - int(alice_public_balance or 0), 0)
            if mint_amount > 0:
                mint_submission = await client.send_tx(
                    token_name,
                    "mint_public",
                    {"amount": mint_amount, "to": alice.public_key},
                    chi=500_000,
                    wait_for_tx=True,
                )
                ensure_positive_submission(
                    mint_submission,
                    label="shielded-mint-public",
                )
            asset_id = await client.call(token_name, "asset_id", {})
            zero_root = await client.call(token_name, "zero_shielded_root", {})
            initial_tree_state = await client.call(token_name, "get_tree_state", {})

        if proof_config["zero_root"] != zero_root:
            raise E2EError("shielded proof config zero_root drifted from contract root")
        if proof_config["root_history_window"] != 32:
            raise E2EError(
                f"shielded token root history window drifted: {proof_config['root_history_window']!r}"
            )
        if proof_config["circuit_family"] != "shielded_note_v3":
            raise E2EError("shielded token circuit family drifted")
        if relay_proof_config["circuit_family"] != "shielded_command_v4":
            raise E2EError("shielded relay circuit family drifted")
        if relay_proof_config["statement_version"] != "4":
            raise E2EError("shielded relay statement version drifted")
        if initial_tree_state["root"] != zero_root or initial_tree_state["note_count"] != 0:
            raise E2EError("shielded token did not start from the zero root")

        (
            node0_wallet,
            node1_wallet,
            node2_wallet,
            node3_wallet,
            node4_wallet,
        ) = self.validator_wallets
        async with self.client(node0_wallet, 0, session) as node0, self.client(
            node1_wallet, 1, session
        ) as node1, self.client(node2_wallet, 2, session) as node2, self.client(
            node3_wallet, 3, session
        ) as node3, self.client(node4_wallet, 4, session) as node4:
            registry_entries = {
                entry["action"]: entry
                for entry in registry_manifest["registry_entries"]
            }
            registry_entries.update(
                {
                    entry["action"]: entry
                    for entry in relay_registry_manifest["registry_entries"]
                }
            )
            for action in ("deposit", "transfer", "withdraw", "relay_transfer"):
                vk_entry = registry_entries[action]
                vk_id = vk_entry["vk_id"]
                vk_info = await node0.call(
                    registry_name,
                    "get_vk_info",
                    {"vk_id": vk_id},
                )
                if vk_info is None:
                    vk_vote = await self.approve_governance_proposal(
                        node0,
                        [
                            ("node1", node1),
                            ("node2", node2),
                            ("node3", node3),
                            ("node4", node4),
                        ],
                        proposal_function="propose_contract_call",
                        proposal_kwargs={
                            "target_contract": registry_name,
                            "target_function": "register_vk",
                            "kwargs": {
                                "vk_id": vk_id,
                                "vk_hex": vk_entry["vk_hex"],
                                "circuit_name": vk_entry["circuit_name"],
                                "version": vk_entry["version"],
                                "artifact_contract_name": vk_entry[
                                    "artifact_contract_name"
                                ],
                                "circuit_family": vk_entry["circuit_family"],
                                "statement_version": vk_entry[
                                    "statement_version"
                                ],
                                "tree_depth": vk_entry["tree_depth"],
                                "leaf_capacity": vk_entry["leaf_capacity"],
                                "max_inputs": vk_entry["max_inputs"],
                                "max_outputs": vk_entry["max_outputs"],
                                "setup_mode": vk_entry["setup_mode"],
                                "setup_ceremony": vk_entry["setup_ceremony"],
                                "bundle_hash": vk_entry["bundle_hash"],
                                "artifact_hash": vk_entry["artifact_hash"],
                                "warning": vk_entry["warning"],
                            },
                            "summary": (
                                f"register {action} vk for localnet shielded e2e"
                            ),
                        },
                        expected_final_status="executed",
                        label_prefix=f"register-vk-{action}",
                    )
                    vk_registration_proposals.append(
                        normalize_value(vk_vote)
                    )
                    vk_info = await node0.call(
                        registry_name,
                        "get_vk_info",
                        {"vk_id": vk_id},
                    )
                if vk_info is None:
                    raise E2EError(f"vk {vk_id} was not registered")
                vk_infos[action] = normalize_value(vk_info)
                if vk_info.get("circuit_family") != vk_entry["circuit_family"]:
                    raise E2EError(
                        f"shielded vk {vk_id} circuit family drifted"
                    )
                if vk_info.get("statement_version") != vk_entry["statement_version"]:
                    raise E2EError(
                        f"shielded vk {vk_id} statement version drifted"
                    )
                if vk_info.get("tree_depth") != vk_entry["tree_depth"]:
                    raise E2EError(
                        f"shielded vk {vk_id} tree depth drifted"
                    )
                if vk_info.get("leaf_capacity") != vk_entry["leaf_capacity"]:
                    raise E2EError(
                        f"shielded vk {vk_id} leaf capacity drifted"
                    )
                if vk_info.get("max_inputs") != vk_entry["max_inputs"]:
                    raise E2EError(
                        f"shielded vk {vk_id} max_inputs drifted"
                    )
                if vk_info.get("max_outputs") != vk_entry["max_outputs"]:
                    raise E2EError(
                        f"shielded vk {vk_id} max_outputs drifted"
                    )

                binding = await node0.call(
                    token_name,
                    "get_vk_binding",
                    {"action": action},
                )
                if binding is None or binding.get("vk_id") != vk_id:
                    bind_submission = await node0.send_tx(
                        token_name,
                        "configure_vk",
                        {"action": action, "vk_id": vk_id},
                        chi=500_000,
                        wait_for_tx=True,
                    )
                    ensure_positive_submission(
                        bind_submission,
                        label=f"bind-vk-{action}",
                    )
                    binding = await node0.call(
                        token_name,
                        "get_vk_binding",
                        {"action": action},
                    )
                if binding is None:
                    raise E2EError(f"shielded vk binding missing for {action}")
                if binding.get("vk_hash") != vk_info.get("vk_hash"):
                    raise E2EError(
                        f"shielded vk binding hash drifted for {action}"
                    )
                vk_bindings[action] = normalize_value(binding)

        alice_keys = ShieldedKeyBundle.generate()
        bob_keys = ShieldedKeyBundle.generate()
        alice_wallet = ShieldedRelayTransferWallet.from_parts(
            asset_id=asset_id,
            owner_secret=alice_keys.owner_secret,
            viewing_private_key=alice_keys.viewing_private_key,
        )
        bob_wallet = ShieldedRelayTransferWallet.from_parts(
            asset_id=asset_id,
            owner_secret=bob_keys.owner_secret,
            viewing_private_key=bob_keys.viewing_private_key,
        )

        def field_hex(value: int) -> str:
            return f"0x{value:064x}"

        alice_note_1 = ShieldedNote(
            owner_secret=alice_keys.owner_secret,
            amount=40,
            rho=field_hex(101),
            blind=field_hex(201),
        )
        alice_note_2 = ShieldedNote(
            owner_secret=alice_keys.owner_secret,
            amount=30,
            rho=field_hex(102),
            blind=field_hex(202),
        )
        bob_note = ShieldedNote(
            owner_secret=bob_keys.owner_secret,
            amount=25,
            rho=field_hex(103),
            blind=field_hex(203),
        )
        alice_change = ShieldedNote(
            owner_secret=alice_keys.owner_secret,
            amount=45,
            rho=field_hex(104),
            blind=field_hex(204),
        )
        alice_withdraw_change = ShieldedNote(
            owner_secret=alice_keys.owner_secret,
            amount=25,
            rho=field_hex(105),
            blind=field_hex(205),
        )
        recent_root_note = ShieldedNote(
            owner_secret=alice_keys.owner_secret,
            amount=5,
            rho=field_hex(106),
            blind=field_hex(206),
        )
        relayer_runtime_status: dict[str, Any] | None = None
        relayer_shutdown_status: dict[str, Any] | None = None
        relayer_service_info: dict[str, Any] | None = None
        relayer_quote: dict[str, Any] | None = None
        relayer_unauth_quote_error: str | None = None
        relayer_disallowed_quote_error: str | None = None
        relayer_low_fee_error: str | None = None
        relayer_job_unauth_error: str | None = None
        relayer_down_error: str | None = None
        service_relay_job: dict[str, Any] | None = None
        service_relay_receipt: dict[str, Any] | None = None

        async with self.client(alice, 1, session) as alice_client, self.client(
            relayer, 3, session
        ) as relayer_client, self.client(founder, 0, session) as founder_client:
            deposit_payloads = [
                alice_note_1.to_output().encrypt_for(
                    asset_id=asset_id,
                    viewing_public_key=alice_keys.viewing_public_key,
                ),
                alice_note_2.to_output().encrypt_for(
                    asset_id=asset_id,
                    viewing_public_key=alice_keys.viewing_public_key,
                ),
            ]
            deposit_payload_hashes = output_payload_hashes(deposit_payloads)
            deposit = prover.prove_deposit(
                ShieldedDepositRequest(
                    asset_id=asset_id,
                    old_root=zero_root,
                    append_state=tree_state([]),
                    amount=70,
                    outputs=[alice_note_1.to_output(), alice_note_2.to_output()],
                    output_payload_hashes=deposit_payload_hashes,
                )
            )
            deposit_submission = await alice_client.send_tx(
                token_name,
                "deposit_shielded",
                {
                    "amount": 70,
                    "old_root": deposit.old_root,
                    "output_commitments": deposit.output_commitments,
                    "proof_hex": deposit.proof_hex,
                    "output_payloads": deposit_payloads,
                },
                chi=SHIELDED_TX_CHI["deposit"],
                wait_for_tx=True,
            )
            deposit_receipt = ensure_positive_submission(
                deposit_submission,
                label="shielded-deposit",
            )

            records_after_deposit = await indexed_note_records(
                founder_client,
                minimum_count=2,
            )
            if len(records_after_deposit) < 2:
                raise E2EError(
                    "shielded deposit did not create two note records "
                    f"(saw {len(records_after_deposit)})"
                )
            deposit_records = records_after_deposit[-2:]
            if deposit_records[0].payload != deposit_payloads[0]:
                raise E2EError("shielded deposit did not persist the first payload")
            if deposit_records[1].payload != deposit_payloads[1]:
                raise E2EError("shielded deposit did not persist the second payload")
            alice_sync_after_deposit = alice_wallet.sync_records(records_after_deposit)
            if len(alice_sync_after_deposit.discovered_notes) != 2:
                raise E2EError("shielded wallet did not discover both deposit notes")
            if alice_wallet.available_balance() != 70:
                raise E2EError("shielded wallet deposit balance drifted")
            alice_wallet_snapshot = ShieldedWallet.from_json(alice_wallet.to_json())
            if alice_wallet_snapshot.available_balance() != 70:
                raise E2EError("shielded wallet snapshot restore drifted")
            alice_wallet_seed = ShieldedWallet.from_seed_json(
                alice_wallet.export_seed_json()
            )
            if alice_wallet_seed.owner_secret != alice_wallet.owner_secret:
                raise E2EError("shielded wallet seed export/import drifted")
            recovered_alice = recover_encrypted_notes(
                asset_id=asset_id,
                commitments=[record.commitment for record in records_after_deposit],
                payloads=[record.payload for record in records_after_deposit],
                owner_secret=alice_keys.owner_secret,
                viewing_private_key=alice_keys.viewing_private_key,
            )
            discovered_after_deposit = scan_notes(
                asset_id=asset_id,
                commitments=deposit.output_commitments,
                notes=[alice_note_1, alice_note_2],
            )
            transfer_payloads = [
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
            ]
            transfer_payload_hashes = output_payload_hashes(transfer_payloads)
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
                    output_payload_hashes=transfer_payload_hashes,
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
                    "output_payloads": transfer_payloads,
                },
                chi=SHIELDED_TX_CHI["transfer"],
                wait_for_tx=True,
            )
            transfer_receipt = ensure_positive_submission(
                transfer_submission,
                label="shielded-transfer",
            )

            records_after_transfer = await indexed_note_records(
                founder_client,
                minimum_count=4,
            )
            recovered_bob = recover_encrypted_notes(
                asset_id=asset_id,
                commitments=[record.commitment for record in records_after_transfer],
                payloads=[record.payload for record in records_after_transfer],
                owner_secret=bob_keys.owner_secret,
                viewing_private_key=bob_keys.viewing_private_key,
            )
            alice_sync_after_transfer = alice_wallet.sync_records(records_after_transfer)
            bob_sync_after_transfer = bob_wallet.sync_records(records_after_transfer)
            if len(alice_sync_after_transfer.discovered_notes) != 1:
                raise E2EError(
                    "shielded wallet did not discover the transfer change note"
                )
            if len(bob_sync_after_transfer.discovered_notes) != 1:
                raise E2EError("shielded wallet did not discover Bob's transfer note")
            alice_spent_after_transfer = alice_wallet.apply_spent_nullifiers(
                transfer.input_nullifiers
            )
            if len(alice_spent_after_transfer) != 2:
                raise E2EError("shielded wallet did not mark both transfer inputs spent")
            if alice_wallet.available_balance() != 45:
                raise E2EError("shielded wallet transfer balance drifted")
            if bob_wallet.available_balance() != 25:
                raise E2EError("shielded Bob wallet balance drifted")
            discovered_after_transfer = scan_notes(
                asset_id=asset_id,
                commitments=deposit.output_commitments + transfer.output_commitments,
                notes=[alice_change],
            )
            relay_plan = bob_wallet.build_relay_transfer(
                recipient=alice_wallet.recipient,
                amount=9,
                relayer=relayer.public_key,
                chain_id=self.network["chain_id"],
                fee=2,
            )
            relay_proof = relay_prover.prove_relay_transfer(relay_plan.request)
            relay_submission = await relayer_client.send_tx(
                token_name,
                "relay_transfer_shielded",
                {
                    "old_root": relay_proof.old_root,
                    "input_nullifiers": relay_proof.input_nullifiers,
                    "output_commitments": relay_proof.output_commitments,
                    "proof_hex": relay_proof.proof_hex,
                    "relayer_fee": relay_proof.relayer_fee,
                    "output_payloads": relay_plan.output_payloads,
                },
                chi=SHIELDED_TX_CHI["transfer"],
                wait_for_tx=True,
            )
            relay_receipt = ensure_positive_submission(
                relay_submission,
                label="shielded-relay-transfer",
            )

            records_after_relay = await indexed_note_records(
                founder_client,
                minimum_count=6,
            )
            alice_sync_after_relay = alice_wallet.sync_records(records_after_relay)
            bob_sync_after_relay = bob_wallet.sync_records(records_after_relay)
            if len(alice_sync_after_relay.discovered_notes) != 1:
                raise E2EError(
                    "shielded wallet did not discover Alice's relayed note"
                )
            if len(bob_sync_after_relay.discovered_notes) != 1:
                raise E2EError(
                    "shielded wallet did not discover Bob's relay change note"
                )
            bob_spent_after_relay = bob_wallet.apply_spent_nullifiers(
                relay_proof.input_nullifiers
            )
            if len(bob_spent_after_relay) != 1:
                raise E2EError(
                    "shielded wallet did not mark the relay transfer input spent"
                )
            if alice_wallet.available_balance() != 54:
                raise E2EError("shielded relay recipient balance drifted")
            if bob_wallet.available_balance() != 14:
                raise E2EError("shielded relay change balance drifted")

            relay_indexed_tx = await founder_client.get_indexed_tx(
                relay_receipt["tx_hash"]
            )
            relay_events = await founder_client.get_events_for_tx(
                relay_receipt["tx_hash"]
            )
            if relay_indexed_tx is None:
                raise E2EError("shielded relay indexed transaction missing")
            if relay_indexed_tx.sender != relayer.public_key:
                raise E2EError("shielded relay tx sender drifted from relayer")
            relay_raw_json = json.dumps(relay_indexed_tx.raw, sort_keys=True)
            if alice.public_key in relay_raw_json or bob.public_key in relay_raw_json:
                raise E2EError(
                    "shielded relay tx leaked public account addresses"
                )
            relay_event = next(
                (
                    event
                    for event in relay_events
                    if event.event == "ShieldedRelayTransfer"
                    and event.signer == relayer.public_key
                ),
                None,
            )
            if relay_event is None:
                raise E2EError("shielded relay event stream drifted")

        async with self.client(alice, 1, session) as alice_client, self.client(
            founder, 0, session
        ) as founder_client:
            withdraw_payloads = [
                alice_withdraw_change.to_output().encrypt_for(
                    asset_id=asset_id,
                    viewing_public_key=alice_keys.viewing_public_key,
                )
            ]
            withdraw_payload_hashes = output_payload_hashes(withdraw_payloads)
            withdraw = prover.prove_withdraw(
                ShieldedWithdrawRequest(
                    asset_id=asset_id,
                    old_root=transfer.expected_new_root,
                    append_state=tree_state(
                        deposit.output_commitments + transfer.output_commitments
                    ),
                    amount=20,
                    recipient=bob.public_key,
                    inputs=[discovered_after_transfer[0].to_input()],
                    outputs=[alice_withdraw_change.to_output()],
                    output_payload_hashes=withdraw_payload_hashes,
                )
            )
            withdraw_submission = await alice_client.send_tx(
                token_name,
                "withdraw_shielded",
                {
                    "amount": 20,
                    "to": bob.public_key,
                    "old_root": withdraw.old_root,
                    "input_nullifiers": withdraw.input_nullifiers,
                    "output_commitments": withdraw.output_commitments,
                    "proof_hex": withdraw.proof_hex,
                    "output_payloads": withdraw_payloads,
                },
                chi=SHIELDED_TX_CHI["withdraw"],
                wait_for_tx=True,
            )
            withdraw_receipt = ensure_positive_submission(
                withdraw_submission,
                label="shielded-withdraw",
            )
            records_after_withdraw = await indexed_note_records(
                founder_client,
                minimum_count=7,
            )
            if len(records_after_withdraw) < 7:
                raise E2EError(
                    "shielded withdraw did not leave the expected note count "
                    f"(saw {len(records_after_withdraw)})"
                )
            alice_sync_after_withdraw = alice_wallet.sync_records(records_after_withdraw)
            if len(alice_sync_after_withdraw.discovered_notes) != 1:
                raise E2EError(
                    "shielded wallet did not discover the withdraw change note"
                )
            alice_spent_after_withdraw = alice_wallet.apply_spent_nullifiers(
                withdraw.input_nullifiers
            )
            if len(alice_spent_after_withdraw) != 1:
                raise E2EError(
                    "shielded wallet did not mark the withdraw input as spent"
                )
            if alice_wallet.available_balance() != 34:
                raise E2EError("shielded wallet withdraw balance drifted")
            alice_public = await founder_client.call(
                token_name,
                "balance_of",
                {"address": alice.public_key},
            )
            bob_public = await founder_client.call(
                token_name,
                "balance_of",
                {"address": bob.public_key},
            )
            relayer_public = await founder_client.call(
                token_name,
                "balance_of",
                {"address": relayer.public_key},
            )
            supply_state = await founder_client.call(token_name, "get_supply_state", {})
            current_root_after_withdraw = await founder_client.call(
                token_name,
                "current_shielded_root",
                {},
            )
            tree_state_after_withdraw = await founder_client.call(
                token_name,
                "get_tree_state",
                {},
            )
            transfer_nullifier_spent = await founder_client.call(
                token_name,
                "is_nullifier_spent",
                {"nullifier": transfer.input_nullifiers[0]},
            )
            relay_nullifier_spent = await founder_client.call(
                token_name,
                "is_nullifier_spent",
                {"nullifier": relay_proof.input_nullifiers[0]},
            )
            withdraw_nullifier_spent = await founder_client.call(
                token_name,
                "is_nullifier_spent",
                {"nullifier": withdraw.input_nullifiers[0]},
            )
            replay_submission = await alice_client.send_tx(
                token_name,
                "withdraw_shielded",
                {
                    "amount": 20,
                    "to": bob.public_key,
                    "old_root": withdraw.old_root,
                    "input_nullifiers": withdraw.input_nullifiers,
                    "output_commitments": withdraw.output_commitments,
                    "proof_hex": withdraw.proof_hex,
                    "output_payloads": withdraw_payloads,
                },
                chi=SHIELDED_TX_CHI["withdraw"],
                wait_for_tx=True,
            )
            replay_receipt = ensure_failed_submission(
                replay_submission,
                label="shielded-replay-withdraw",
                expected_message_fragment="Nullifier already spent",
            )

            exact_withdraw_plan = alice_wallet.build_withdraw(
                amount=25,
                recipient=alice.public_key,
            )
            if exact_withdraw_plan.request.outputs != []:
                raise E2EError("shielded exact withdraw unexpectedly created outputs")
            if exact_withdraw_plan.output_payloads != []:
                raise E2EError(
                    "shielded exact withdraw unexpectedly created payloads"
                )
            exact_withdraw = prover.prove_withdraw(exact_withdraw_plan.request)
            exact_withdraw_submission = await alice_client.send_tx(
                token_name,
                "withdraw_shielded",
                {
                    "amount": 25,
                    "to": alice.public_key,
                    "old_root": exact_withdraw.old_root,
                    "input_nullifiers": exact_withdraw.input_nullifiers,
                    "output_commitments": exact_withdraw.output_commitments,
                    "proof_hex": exact_withdraw.proof_hex,
                    "output_payloads": exact_withdraw_plan.output_payloads,
                },
                chi=SHIELDED_TX_CHI["withdraw"],
                wait_for_tx=True,
            )
            exact_withdraw_receipt = ensure_positive_submission(
                exact_withdraw_submission,
                label="shielded-exact-withdraw",
            )
            if exact_withdraw.output_commitments != []:
                raise E2EError(
                    "shielded exact withdraw proof unexpectedly produced commitments"
                )
            alice_spent_after_exact_withdraw = alice_wallet.apply_spent_nullifiers(
                exact_withdraw.input_nullifiers
            )
            if len(alice_spent_after_exact_withdraw) != 1:
                raise E2EError(
                    "shielded wallet did not mark the exact withdraw input as spent"
                )
            if alice_wallet.available_balance() != 9:
                raise E2EError("shielded wallet exact withdraw balance drifted")
            alice_public_after_exact = await founder_client.call(
                token_name,
                "balance_of",
                {"address": alice.public_key},
            )
            current_root_before_recent = await founder_client.call(
                token_name,
                "current_shielded_root",
                {},
            )
            if current_root_before_recent != current_root_after_withdraw:
                raise E2EError(
                    "shielded exact withdraw with no outputs unexpectedly changed the root"
                )

            zero_root_still_accepted = await founder_client.call(
                token_name,
                "is_root_accepted",
                {"root": zero_root},
            )
            if current_root_before_recent == zero_root:
                raise E2EError("shielded recent-root probe did not start from a non-current root")
            if not zero_root_still_accepted:
                raise E2EError("shielded token no longer accepts the zero root in-window")

            recent_root_deposit_payloads = [
                recent_root_note.to_output().encrypt_for(
                    asset_id=asset_id,
                    viewing_public_key=alice_keys.viewing_public_key,
                )
            ]
            recent_root_deposit_payload_hashes = output_payload_hashes(
                recent_root_deposit_payloads
            )
            recent_root_deposit = prover.prove_deposit(
                ShieldedDepositRequest(
                    asset_id=asset_id,
                    old_root=zero_root,
                    append_state=tree_state(alice_wallet.commitments()),
                    amount=5,
                    outputs=[recent_root_note.to_output()],
                    output_payload_hashes=recent_root_deposit_payload_hashes,
                )
            )
            recent_root_deposit_submission = await alice_client.send_tx(
                token_name,
                "deposit_shielded",
                {
                    "amount": 5,
                    "old_root": recent_root_deposit.old_root,
                    "output_commitments": recent_root_deposit.output_commitments,
                    "proof_hex": recent_root_deposit.proof_hex,
                    "output_payloads": recent_root_deposit_payloads,
                },
                chi=SHIELDED_TX_CHI["deposit"],
                wait_for_tx=True,
            )
            recent_root_deposit_receipt = ensure_positive_submission(
                recent_root_deposit_submission,
                label="shielded-recent-root-deposit",
            )
            records_after_recent_root = await indexed_note_records(
                founder_client,
                minimum_count=8,
            )
            alice_sync_after_recent_root = alice_wallet.sync_records(
                records_after_recent_root
            )
            if len(alice_sync_after_recent_root.discovered_notes) != 1:
                raise E2EError(
                    "shielded wallet did not discover the recent-root deposit note"
                )
            if alice_wallet.available_balance() != 14:
                raise E2EError("shielded wallet recent-root balance drifted")
            alice_wallet_restored = ShieldedWallet.from_json(alice_wallet.to_json())
            if alice_wallet_restored.available_balance() != 14:
                raise E2EError("shielded wallet restore drifted after recent-root deposit")
            bob_wallet.sync_records(records_after_recent_root)

            service_relay_plan = bob_wallet.build_relay_transfer(
                recipient=alice_wallet.recipient,
                amount=4,
                relayer=relayer.public_key,
                chain_id=self.network["chain_id"],
                fee=1,
            )
            service_relay_proof = relay_prover.prove_relay_transfer(
                service_relay_plan.request
            )
            relayer_env = make_localnet_env(self.args)
            relayer_secret = "localnet-shielded-relayer-token"
            relayer_env.update(
                {
                    "XIAN_SHIELDED_RELAYER_PRIVATE_KEY": relayer.private_key,
                    "XIAN_SHIELDED_RELAYER_NODE_URL": service.rpc_url,
                    "XIAN_SHIELDED_RELAYER_AUTH_TOKEN": relayer_secret,
                    "XIAN_SHIELDED_RELAYER_PUBLIC_INFO": "1",
                    "XIAN_SHIELDED_RELAYER_PUBLIC_QUOTE": "0",
                    "XIAN_SHIELDED_RELAYER_PUBLIC_JOB_LOOKUP": "0",
                    "XIAN_SHIELDED_RELAYER_SUBMISSION_MODE": "commit",
                    "XIAN_SHIELDED_RELAYER_ALLOWED_NOTE_CONTRACTS": token_name,
                    "XIAN_SHIELDED_RELAYER_MIN_NOTE_RELAYER_FEE": "1",
                }
            )
            relayer_runtime_status = start_shielded_relayer_runtime(
                bind_host=DEFAULT_SHIELDED_RELAYER_HOST,
                port=DEFAULT_SHIELDED_RELAYER_PORT,
                env=relayer_env,
            )
            if not relayer_runtime_status.get("shielded_relayer_health_ok"):
                raise E2EError("shielded relayer runtime did not become healthy")
            relayer_base_url = shielded_relayer_endpoints(
                bind_host=DEFAULT_SHIELDED_RELAYER_HOST,
                port=DEFAULT_SHIELDED_RELAYER_PORT,
            )["shielded_relayer"]
            try:
                async with ShieldedRelayerAsyncClient(
                    relayer_base_url,
                    session=session,
                ) as public_relayer_client, ShieldedRelayerAsyncClient(
                    relayer_base_url,
                    auth_token=relayer_secret,
                    session=session,
                ) as authed_relayer_client:
                    relayer_info = await public_relayer_client.get_info()
                    relayer_service_info = normalize_value(relayer_info.raw)
                    if relayer_info.raw.get("auth", {}).get("scheme") != "bearer":
                        raise E2EError("shielded relayer info auth scheme drifted")
                    if relayer_info.raw.get("auth", {}).get("public_quote") is not False:
                        raise E2EError("shielded relayer quote unexpectedly became public")

                    try:
                        await public_relayer_client.get_quote(
                            kind="shielded_note_relay_transfer",
                            contract=token_name,
                            requested_relayer_fee=1,
                        )
                    except TransportError as exc:
                        relayer_unauth_quote_error = str(exc)
                    else:
                        raise E2EError(
                            "shielded relayer accepted an unauthenticated quote request"
                        )
                    if "missing or invalid bearer token" not in (
                        relayer_unauth_quote_error or ""
                    ):
                        raise E2EError(
                            "shielded relayer unauthenticated quote failure drifted"
                        )

                    try:
                        await authed_relayer_client.get_quote(
                            kind="shielded_note_relay_transfer",
                            contract="currency",
                            requested_relayer_fee=1,
                        )
                    except TransportError as exc:
                        relayer_disallowed_quote_error = str(exc)
                    else:
                        raise E2EError(
                            "shielded relayer accepted a quote for a disallowed contract"
                        )
                    if "not allowed by this relayer policy" not in (
                        relayer_disallowed_quote_error or ""
                    ):
                        raise E2EError(
                            "shielded relayer allowlist rejection drifted"
                        )

                    quote = await authed_relayer_client.get_quote(
                        kind="shielded_note_relay_transfer",
                        contract=token_name,
                        requested_relayer_fee=1,
                    )
                    relayer_quote = normalize_value(quote.raw)
                    if quote.relayer_fee != 1:
                        raise E2EError("shielded relayer quote fee drifted")

                    try:
                        await authed_relayer_client.submit_shielded_note_relay_transfer(
                            contract=token_name,
                            old_root=service_relay_proof.old_root,
                            input_nullifiers=service_relay_proof.input_nullifiers,
                            output_commitments=service_relay_proof.output_commitments,
                            proof_hex=service_relay_proof.proof_hex,
                            relayer_fee=0,
                            output_payloads=service_relay_plan.output_payloads,
                            client_request_id=f"{self.run_id}-low-fee",
                        )
                    except TransportError as exc:
                        relayer_low_fee_error = str(exc)
                    else:
                        raise E2EError(
                            "shielded relayer accepted a below-minimum relay fee"
                        )
                    if "minimum note relay fee" not in (relayer_low_fee_error or ""):
                        raise E2EError("shielded relayer low-fee rejection drifted")

                    submitted_job = await authed_relayer_client.submit_shielded_note_relay_transfer(
                        contract=token_name,
                        old_root=service_relay_proof.old_root,
                        input_nullifiers=service_relay_proof.input_nullifiers,
                        output_commitments=service_relay_proof.output_commitments,
                        proof_hex=service_relay_proof.proof_hex,
                        relayer_fee=service_relay_proof.relayer_fee,
                        output_payloads=service_relay_plan.output_payloads,
                        client_request_id=f"{self.run_id}-service-relay",
                    )
                    service_relay_job = normalize_value(submitted_job.raw)
                    if submitted_job.status == "failed":
                        raise E2EError(
                            "shielded relayer service relay failed: "
                            f"{submitted_job.error or submitted_job.raw}"
                        )

                    fetched_job = await authed_relayer_client.get_job(submitted_job.job_id)
                    if fetched_job.job_id != submitted_job.job_id:
                        raise E2EError("shielded relayer job lookup job_id drifted")
                    if fetched_job.status == "failed":
                        raise E2EError(
                            "shielded relayer service persisted a failed relay job: "
                            f"{fetched_job.error or fetched_job.raw}"
                        )
                    if (
                        submitted_job.tx_hash is not None
                        and fetched_job.tx_hash != submitted_job.tx_hash
                    ):
                        raise E2EError("shielded relayer job lookup tx hash drifted")

                    try:
                        await public_relayer_client.get_job(submitted_job.job_id)
                    except TransportError as exc:
                        relayer_job_unauth_error = str(exc)
                    else:
                        raise E2EError(
                            "shielded relayer allowed unauthenticated job lookup"
                        )
                    if "missing or invalid bearer token" not in (
                        relayer_job_unauth_error or ""
                    ):
                        raise E2EError(
                            "shielded relayer unauthenticated job lookup drifted"
                        )
            finally:
                relayer_shutdown_status = stop_shielded_relayer_runtime(
                    bind_host=DEFAULT_SHIELDED_RELAYER_HOST,
                    port=DEFAULT_SHIELDED_RELAYER_PORT,
                    env=relayer_env,
                )

            async with ShieldedRelayerAsyncClient(
                relayer_base_url,
                session=session,
            ) as stopped_relayer_client:
                try:
                    await stopped_relayer_client.get_info()
                except TransportError as exc:
                    relayer_down_error = str(exc)
                else:
                    raise E2EError(
                        "shielded relayer info still responded after the runtime was stopped"
                    )
            if not relayer_down_error:
                raise E2EError("shielded relayer stop check did not produce a transport error")

            records_after_service_relay = await indexed_note_records(
                founder_client,
                minimum_count=10,
            )
            alice_sync_after_service_relay = alice_wallet.sync_records(
                records_after_service_relay
            )
            bob_sync_after_service_relay = bob_wallet.sync_records(
                records_after_service_relay
            )
            if len(alice_sync_after_service_relay.discovered_notes) != 1:
                raise E2EError(
                    "shielded relayer service did not deliver Alice's relayed note"
                )
            if len(bob_sync_after_service_relay.discovered_notes) != 1:
                raise E2EError(
                    "shielded relayer service did not deliver Bob's relay change note"
                )
            bob_spent_after_service_relay = bob_wallet.apply_spent_nullifiers(
                service_relay_proof.input_nullifiers
            )
            if len(bob_spent_after_service_relay) != 1:
                raise E2EError(
                    "shielded relayer service did not mark the relay input as spent"
                )
            if submitted_job.tx_hash is not None:
                service_relay_indexed_tx = await founder_client.get_indexed_tx(
                    submitted_job.tx_hash
                )
                if service_relay_indexed_tx is None:
                    raise E2EError("shielded relayer service tx was not indexed")
                service_relay_events = await founder_client.get_events_for_tx(
                    submitted_job.tx_hash
                )
                if not any(
                    event.event == "ShieldedRelayTransfer"
                    and event.signer == relayer.public_key
                    for event in service_relay_events
                ):
                    raise E2EError("shielded relayer service event stream drifted")
                service_relay_receipt = normalize_value(
                    {
                        "job": fetched_job.raw,
                        "indexed_tx": service_relay_indexed_tx.raw,
                        "events": [event.raw for event in service_relay_events],
                    }
                )
            else:
                service_relay_receipt = normalize_value({"job": fetched_job.raw})

            alice_public = await founder_client.call(
                token_name,
                "balance_of",
                {"address": alice.public_key},
            )
            bob_public = await founder_client.call(
                token_name,
                "balance_of",
                {"address": bob.public_key},
            )
            relayer_public = await founder_client.call(
                token_name,
                "balance_of",
                {"address": relayer.public_key},
            )
            supply_state = await founder_client.call(
                token_name,
                "get_supply_state",
                {},
            )
            final_root = await founder_client.call(
                token_name,
                "current_shielded_root",
                {},
            )
            final_tree_state = await founder_client.call(
                token_name,
                "get_tree_state",
                {},
            )
            final_note_count = await founder_client.call(
                token_name,
                "get_note_count",
                {},
            )

        if len(recovered_alice) != 2:
            raise E2EError("shielded Alice recovery did not find both deposit notes")
        if len(recovered_bob) != 1:
            raise E2EError("shielded Bob recovery did not find the transfer note")
        if len(discovered_after_deposit) != 2:
            raise E2EError("shielded note scan did not find the deposit notes")
        if len(discovered_after_transfer) != 1:
            raise E2EError("shielded note scan did not find the change note")
        if (
            transfer_nullifier_spent is not True
            or relay_nullifier_spent is not True
            or withdraw_nullifier_spent is not True
        ):
            raise E2EError("shielded nullifier spend tracking drifted")
        if alice_public != 50:
            raise E2EError(
                f"shielded Alice public balance drifted: {alice_public!r}"
            )
        if bob_public != 20:
            raise E2EError(
                f"shielded Bob public balance drifted: {bob_public!r}"
            )
        if relayer_public != 3:
            raise E2EError(
                f"shielded relayer public balance drifted: {relayer_public!r}"
            )
        expected_supply_state = {
            "total_supply": 100,
            "public_supply": 73,
            "shielded_supply": 27,
        }
        if normalize_value(supply_state) != expected_supply_state:
            raise E2EError(
                f"shielded supply state drifted: {supply_state!r}"
            )
        if alice_wallet.available_balance() != 18 or bob_wallet.available_balance() != 9:
            raise E2EError("shielded wallet final balances drifted")
        if final_note_count != 10:
            raise E2EError(f"shielded note count drifted: {final_note_count!r}")
        if final_tree_state["root"] != final_root:
            raise E2EError("shielded final tree-state root drifted")
        if alice_wallet.current_root() != final_root:
            raise E2EError("shielded wallet commitment root drifted from contract root")

        return {
            "token": token_name,
            "registry": registry_name,
            "registry_owner": registry_owner,
            "proof_config": normalize_value(proof_config),
            "relay_proof_config": normalize_value(relay_proof_config),
            "initial_tree_state": normalize_value(initial_tree_state),
            "vk_infos": vk_infos,
            "vk_bindings": vk_bindings,
            "vk_registration_proposals": vk_registration_proposals,
            "deposit_receipt": deposit_receipt,
            "transfer_receipt": transfer_receipt,
            "relay_receipt": relay_receipt,
            "withdraw_receipt": withdraw_receipt,
            "replay_receipt": replay_receipt,
            "exact_withdraw_receipt": exact_withdraw_receipt,
            "recent_root_deposit_receipt": recent_root_deposit_receipt,
            "relayer_runtime_status": normalize_value(relayer_runtime_status),
            "relayer_shutdown_status": normalize_value(relayer_shutdown_status),
            "relayer_service_info": normalize_value(relayer_service_info),
            "relayer_quote": normalize_value(relayer_quote),
            "relayer_unauth_quote_error": relayer_unauth_quote_error,
            "relayer_disallowed_quote_error": relayer_disallowed_quote_error,
            "relayer_low_fee_error": relayer_low_fee_error,
            "relayer_job_unauth_error": relayer_job_unauth_error,
            "relayer_down_error": relayer_down_error,
            "service_relay_job": normalize_value(service_relay_job),
            "service_relay_receipt": normalize_value(service_relay_receipt),
            "alice_public_balance": alice_public,
            "bob_public_balance": bob_public,
            "relayer_public_balance": relayer_public,
            "supply_state": normalize_value(supply_state),
            "alice_recovered_notes": len(recovered_alice),
            "bob_recovered_notes": len(recovered_bob),
            "note_counts": {
                "after_deposit": len(records_after_deposit),
                "after_transfer": len(records_after_transfer),
                "after_relay": len(records_after_relay),
                "after_withdraw": len(records_after_withdraw),
                "after_service_relay": len(records_after_service_relay),
                "final": final_note_count,
            },
            "root_checks": {
                "zero_root": zero_root,
                "current_root_after_withdraw": current_root_after_withdraw,
                "current_root_before_recent_root_probe": current_root_before_recent,
                "final_root": final_root,
                "zero_root_still_accepted": zero_root_still_accepted,
            },
            "wallet_checks": {
                "alice_available_balance": alice_wallet.available_balance(),
                "bob_available_balance": bob_wallet.available_balance(),
                "alice_discovered_after_deposit": len(
                    alice_sync_after_deposit.discovered_notes
                ),
                "alice_discovered_after_transfer": len(
                    alice_sync_after_transfer.discovered_notes
                ),
                "alice_discovered_after_relay": len(
                    alice_sync_after_relay.discovered_notes
                ),
                "alice_discovered_after_withdraw": len(
                    alice_sync_after_withdraw.discovered_notes
                ),
                "alice_discovered_after_recent_root": len(
                    alice_sync_after_recent_root.discovered_notes
                ),
                "alice_discovered_after_service_relay": len(
                    alice_sync_after_service_relay.discovered_notes
                ),
                "bob_discovered_after_transfer": len(
                    bob_sync_after_transfer.discovered_notes
                ),
                "bob_discovered_after_relay": len(
                    bob_sync_after_relay.discovered_notes
                ),
                "bob_discovered_after_service_relay": len(
                    bob_sync_after_service_relay.discovered_notes
                ),
                "exact_withdraw_output_count": len(
                    exact_withdraw.output_commitments
                ),
            },
            "nullifier_checks": {
                "transfer_input_count": len(transfer.input_nullifiers),
                "relay_input_count": len(relay_proof.input_nullifiers),
                "withdraw_input_count": len(withdraw.input_nullifiers),
                "exact_withdraw_input_count": len(
                    exact_withdraw.input_nullifiers
                ),
                "transfer_nullifier_spent": transfer_nullifier_spent,
                "relay_nullifier_spent": relay_nullifier_spent,
                "withdraw_nullifier_spent": withdraw_nullifier_spent,
                "service_relay_input_count": len(
                    service_relay_proof.input_nullifiers
                ),
            },
            "relay_checks": normalize_value(
                {
                    "sender": relay_indexed_tx.sender,
                    "function": relay_indexed_tx.function,
                    "relayer_fee": relay_proof.relayer_fee,
                    "execution": {
                        "execution_id": relay_event.data_indexed.get(
                            "execution_id"
                        )
                        if relay_event.data_indexed
                        else None,
                        "relayer": relay_event.data_indexed.get("relayer")
                        if relay_event.data_indexed
                        else None,
                        "relay_binding": relay_event.data.get("relay_binding")
                        if relay_event.data
                        else None,
                        "execution_tag": relay_event.data.get("execution_tag")
                        if relay_event.data
                        else None,
                        "nullifier_digest": relay_event.data.get(
                            "nullifier_digest"
                        )
                        if relay_event.data
                        else None,
                        "old_root": relay_event.data.get("old_root")
                        if relay_event.data
                        else None,
                        "new_root": relay_event.data_indexed.get("new_root")
                        if relay_event.data_indexed
                        else None,
                        "nullifier_count": relay_event.data.get(
                            "nullifier_count"
                        )
                        if relay_event.data
                        else None,
                        "output_count": relay_event.data.get("output_count")
                        if relay_event.data
                        else None,
                        "fee": relay_event.data.get("fee")
                        if relay_event.data
                        else None,
                        "expires_at": relay_event.data.get("expires_at")
                        if relay_event.data
                        else None,
                    },
                    "event_count": len(relay_events),
                }
            ),
            "records_sample": normalize_value(
                {
                    "after_deposit": normalize_note_records(records_after_deposit),
                    "after_transfer_tail": normalize_note_records(
                        records_after_transfer[-2:]
                    ),
                    "after_relay_tail": normalize_note_records(
                        records_after_relay[-2:]
                    ),
                    "after_withdraw_tail": normalize_note_records(
                        records_after_withdraw[-2:]
                    ),
                    "after_recent_root_tail": normalize_note_records(
                        records_after_recent_root[-2:]
                    ),
                }
            ),
            "tree_state_after_withdraw": normalize_value(tree_state_after_withdraw),
            "final_tree_state": normalize_value(final_tree_state),
            "alice_public_after_exact_withdraw": alice_public_after_exact,
        }

    async def parallel_execution_phase(
        self,
        session: aiohttp.ClientSession,
    ) -> dict[str, Any]:
        payload = await self.run_localnet_workload(
            scenario="parallel_probe",
            seed_label=f"{self.run_id}:parallel",
        )
        scenario_summary = payload["scenario_summary"]
        parallel_config = normalize_value(
            self.network.get("parallel_execution", {})
        )
        expected_enabled = bool(parallel_config.get("enabled"))
        expected_workers = int(parallel_config.get("workers", 0) or 0)
        expected_min_transactions = int(
            parallel_config.get("min_transactions", 8) or 8
        )
        effective_parallel_enabled = expected_enabled and expected_workers > 0
        overall_window = scenario_summary.get("overall_height_window", {})
        max_height = overall_window.get("max_height")
        if max_height is not None:
            await asyncio.gather(
                *(
                    wait_for_height(
                        session,
                        node.rpc_url,
                        int(max_height),
                        timeout_seconds=min(
                            self.args.rpc_timeout_seconds,
                            45.0,
                        ),
                    )
                    for node in self.nodes
                )
            )

        perf_statuses = await perf_status_from_all_nodes(session, self.nodes)
        perf_config = {}
        for node in self.nodes:
            status = perf_statuses[node.moniker]
            node_config = {
                "parallel_execution_enabled": bool(
                    status.get("parallel_execution_enabled")
                ),
                "parallel_execution_workers": int(
                    status.get("parallel_execution_workers", 0) or 0
                ),
                "parallel_execution_min_transactions": int(
                    status.get("parallel_execution_min_transactions", 0) or 0
                ),
            }
            perf_config[node.moniker] = node_config
            if (
                node_config["parallel_execution_enabled"] != expected_enabled
                or node_config["parallel_execution_workers"] != expected_workers
                or node_config["parallel_execution_min_transactions"]
                != expected_min_transactions
            ):
                raise E2EError(
                    "parallel execution config drift detected on "
                    f"{node.moniker}: {node_config} != {parallel_config}"
                )

        if not effective_parallel_enabled:
            unexpected = {}
            min_height = overall_window.get("min_height")
            for node in self.nodes:
                window = recent_blocks_in_window(
                    perf_statuses[node.moniker],
                    min_height=min_height,
                    max_height=max_height,
                )
                matched = [
                    {
                        "height": int(block["height"]),
                        "metadata": normalize_value(
                            block.get("metadata", {})
                        ),
                    }
                    for block in window
                    if bool((block.get("metadata") or {}).get("parallel_enabled"))
                ]
                if matched:
                    unexpected[node.moniker] = matched
            if unexpected:
                raise E2EError(
                    "parallel execution metadata appeared while disabled: "
                    f"{unexpected}"
                )
            return {
                "parallel_config": parallel_config,
                "perf_config": perf_config,
                "scenario_summary": scenario_summary,
                "parallel_metadata": {
                    "disabled": True,
                    "reason": "parallel execution not effectively enabled",
                },
            }

        batch_expectations = [
            (
                "non_conflicting",
                lambda metadata: bool(metadata.get("parallel_enabled"))
                and int(metadata.get("parallel_speculative_accepted", 0)) > 0
                and int(
                    metadata.get(
                        "parallel_planned_parallelizable_transactions", 0
                    )
                )
                > 0,
            ),
            (
                "same_sender",
                lambda metadata: bool(metadata.get("parallel_enabled"))
                and int(metadata.get("parallel_serial_prefiltered", 0)) > 0,
            ),
            (
                "read_after_write",
                lambda metadata: bool(metadata.get("parallel_enabled"))
                and int(metadata.get("parallel_speculative_wave_count", 0)) > 1,
            ),
            (
                "prefix_scan",
                lambda metadata: bool(metadata.get("parallel_enabled"))
                and int(metadata.get("parallel_speculative_wave_count", 0)) > 1,
            ),
        ]

        metadata_matches = {}
        for batch_name, predicate in batch_expectations:
            batch = scenario_summary["batches"][batch_name]
            metadata_matches[batch_name] = await self.wait_for_parallel_metadata_match(
                session,
                label=f"parallel batch {batch_name}",
                min_height=batch.get("min_height"),
                max_height=batch.get("max_height"),
                predicate=predicate,
                timeout_seconds=min(self.args.rpc_timeout_seconds, 45.0),
            )

        return {
            "parallel_config": parallel_config,
            "perf_config": perf_config,
            "scenario_summary": scenario_summary,
            "parallel_metadata": metadata_matches,
        }

    async def finalize_summary(self) -> dict[str, Any]:
        summary = {
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
        if self.vm_rollout_report is not None:
            summary["vm_rollout"] = self.vm_rollout_report
        return summary

    async def run(self) -> int:
        start_phase = self.args.start_phase
        valid_phase_names = self.phase_names()
        if start_phase not in valid_phase_names:
            raise E2EError(f"unknown start phase: {start_phase}")
        if start_phase != "00-bootstrap" and self.args.resume_dir is None:
            raise E2EError("--resume-dir is required when --start-phase is not 00-bootstrap")

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, sock_connect=5, sock_read=25),
            connector=aiohttp.TCPConnector(limit=256, ttl_dns_cache=300),
        ) as session:
            phase_sequence = [
                ("00-bootstrap", lambda: self.bootstrap(session)),
                ("01-health", lambda: self.health_phase(session)),
                ("02-xian-py-smoke", lambda: self.xian_py_smoke(session)),
                (
                    "03-contract-orchestration",
                    lambda: self.contract_orchestration_phase(session),
                ),
                ("04-periodic-load", lambda: self.periodic_load(session)),
                ("05-burst-load", self.burst_phase),
                ("06-conflict-invalid", lambda: self.conflict_phase(session)),
                ("07-dex-mixed", self.dex_phase),
                ("08-simulator-load", lambda: self.simulator_phase(session)),
                ("09-bds-catchup", lambda: self.bds_catchup_phase(session)),
                ("10-retrieval-surfaces", lambda: self.retrieval_phase(session)),
                ("11-determinism", lambda: self.determinism_phase(session)),
                (
                    "12-validator-governance",
                    lambda: self.validator_governance_phase(session),
                ),
                ("13-state-patch", lambda: self.state_patch_phase(session)),
                ("14-logging", lambda: self.logging_phase(session)),
                ("15-shielded-note-token", lambda: self.shielded_phase(session)),
                ("16-parallel-execution", lambda: self.parallel_execution_phase(session)),
            ]
            if start_phase != "00-bootstrap":
                self.load_resume_context()
                await wait_for_localnet_ready(
                    session,
                    self.nodes,
                    timeout_seconds=self.args.rpc_timeout_seconds,
                )
            start_index = valid_phase_names.index(start_phase)
            for phase_name, fn in phase_sequence[start_index:]:
                await self.run_phase(phase_name, fn)

        if self.execution_mode == "xian_vm_v1" and self.network is not None:
            self.vm_rollout_report = await asyncio.to_thread(
                collect_localnet_vm_rollout_report,
                self.network,
                timeout_seconds=min(self.args.rpc_timeout_seconds, 10.0),
                max_shadow_mismatches=self.args.vm_max_shadow_mismatches,
            )
            (self.output_dir / "vm_rollout.json").write_text(
                json.dumps(self.vm_rollout_report, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            if not self.vm_rollout_report["ok"]:
                raise E2EError(
                    "vm rollout report failed checks: "
                    + json.dumps(
                        {
                            "checks": self.vm_rollout_report["checks"],
                            "errors": self.vm_rollout_report["errors"],
                            "nodes_with_mismatches": self.vm_rollout_report[
                                "nodes_with_mismatches"
                            ],
                        },
                        sort_keys=True,
                    )
                )

        summary = await self.finalize_summary()
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a layered 5-validator testnet-shaped localnet end-to-end program",
    )
    parser.add_argument("--bootstrap", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--build", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--nodes", type=int, default=DEFAULT_LOCALNET_NODES)
    parser.add_argument("--topology", choices=("integrated", "fidelity"), default="integrated")
    parser.add_argument("--genesis-network", default=DEFAULT_GENESIS_NETWORK)
    parser.add_argument("--bds-node-index", type=int, default=0)
    parser.add_argument("--port-offset", type=int, default=1000)
    parser.add_argument("--seed", default="xian-localnet-testnet-e2e-v1")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--execution-mode", default="")
    parser.add_argument("--execution-bytecode-version", default="")
    parser.add_argument("--execution-gas-schedule", default="")
    parser.add_argument("--execution-authority", default="")
    parser.add_argument("--execution-shadow-tracer-mode", default="")
    parser.add_argument("--vm-max-shadow-mismatches", type=int, default=0)
    parser.add_argument("--rpc-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--state-sample-nodes", type=int, default=DEFAULT_LOCALNET_NODES)
    parser.add_argument("--app-hash-window", type=int, default=DEFAULT_LOCALNET_NODES)
    parser.add_argument("--receipt-workers", type=int, default=24)
    parser.add_argument("--periodic-rounds", type=int, default=8)
    parser.add_argument("--periodic-interval-seconds", type=float, default=0.35)
    parser.add_argument("--burst-counter-ops", type=int, default=260)
    parser.add_argument("--dex-rounds", type=int, default=8)
    parser.add_argument(
        "--simulator-burst-concurrency",
        type=int,
        default=DEFAULT_SIMULATOR_BURST_CONCURRENCY,
    )
    parser.add_argument(
        "--start-phase",
        default="00-bootstrap",
        choices=E2ERunner.phase_names(),
    )
    parser.add_argument("--resume-dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = E2ERunner(args)
    return asyncio.run(runner.run())


if __name__ == "__main__":
    raise SystemExit(main())
