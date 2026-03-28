#!/usr/bin/env python3
"""Run a layered 4-node localnet end-to-end program against real services."""

from __future__ import annotations

import argparse
import asyncio
import base64
import functools
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
OUTPUT_ROOT = STACK_DIR / ".artifacts" / "localnet-e2e"
CONTRACTS_DIR = ROOT_DIR / "xian-contracts" / "contracts"
XIAN_ZK_PYTHON_DIR = (
    ROOT_DIR / "xian-contracting" / "packages" / "xian-zk" / "python"
)
XIAN_ABCI_SRC = ROOT_DIR / "xian-abci" / "src"
RUST_TRACER_MODE = "native_instruction_v1"
DEFAULT_TX_STAMPS = 15_000
DEFAULT_TRANSFER_STAMPS = 2_000
GOVERNANCE_TX_STAMPS = 200_000
STATE_PATCH_DELAY_BLOCKS = 8
STATE_PATCH_ACTIVATION_HEADROOM_BLOCKS = 8
SIMULATOR_BURST_REQUESTS = 128
WEBSOCKET_TIMEOUT_SECONDS = 20.0
LOCALNET_POSTGRES_SERVICE = "localnet-postgres"
LOCALNET_POSTGRES_CONTAINER = "xian-localnet-postgres"
CONTRACT_ORCHESTRATION_TX_STAMPS = {
    "deploy_contract": 180_000,
    "deploy_family": 100_000,
    "dynamic_call": 50_000,
}
SHIELDED_TX_STAMPS = {
    "deposit": 8_000_000,
    "transfer": 10_000_000,
    "withdraw": 8_000_000,
}

sys.path.append(str(XIAN_ZK_PYTHON_DIR))
sys.path.append(str(XIAN_ABCI_SRC))

from xian_py.wallet import Wallet  # noqa: E402
from xian_py.xian_async import XianAsync  # noqa: E402
from xian_py.exception import SimulationError, TxTimeoutError  # noqa: E402

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
        if submission.mode == "commit" and submission.accepted is True and submission.finalized:
            return normalize_receipt(submission, label=label)
        raise E2EError(f"{label}: receipt missing")
    if submission.receipt.success is not True:
        raise E2EError(
            f"{label}: transaction failed during execution: "
            f"{submission.receipt.message}"
        )
    return normalize_receipt(submission, label=label)


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
        completed_phase_names = set(
            self.phase_names()[: self.phase_names().index(self.args.start_phase)]
        )

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

    def restart_localnet(self) -> None:
        env = make_localnet_env(self.args)
        run_make("localnet-down", env=env)
        run_make("localnet-up", env=env)

    async def bootstrap(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        env = make_localnet_env(self.args)
        outputs: dict[str, Any] = {
            "env": {
                "XIAN_LOCALNET_TRACER_MODE": env["XIAN_LOCALNET_TRACER_MODE"],
                "XIAN_LOCALNET_ENABLE_BDS": env["XIAN_LOCALNET_ENABLE_BDS"],
            "XIAN_LOCALNET_BDS_NODE_INDEX": env["XIAN_LOCALNET_BDS_NODE_INDEX"],
            "XIAN_LOCALNET_PORT_OFFSET": env["XIAN_LOCALNET_PORT_OFFSET"],
            "XIAN_LOCALNET_APP_LOG_LEVEL": env["XIAN_LOCALNET_APP_LOG_LEVEL"],
                "XIAN_LOCALNET_TOPOLOGY": env["XIAN_LOCALNET_TOPOLOGY"],
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
                    mode="commit",
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

    async def contract_orchestration_phase(
        self,
        session: aiohttp.ClientSession,
    ) -> dict[str, Any]:
        operator = derive_wallet(self.seed, "contract-orchestration-operator")
        await self.fund_wallets(session, [operator], amount=15_000)

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

        factory_code = read_text(WORKLOADS_DIR / "e2e" / "orchestration_factory.py")
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
                existing_code = await client.get_contract_code(name)
                if existing_code is not None:
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
                    code=code,
                    stamps=CONTRACT_ORCHESTRATION_TX_STAMPS["deploy_contract"],
                    mode="commit",
                )
                deployments.append(
                    ensure_positive_submission(
                        submission,
                        label=f"deploy {name}",
                    )
                )

            alpha_existing = await client.get_contract_code(alpha_name)
            beta_existing = await client.get_contract_code(beta_name)
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
                    stamps=CONTRACT_ORCHESTRATION_TX_STAMPS["deploy_family"],
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
            alpha_source = await client.get_contract_code(alpha_name)
            beta_source = await client.get_contract_code(beta_name)
            alpha_developer = await client.get_state(alpha_name, "__developer__")
            alpha_deployer = await client.get_state(alpha_name, "__deployer__")
            alpha_initiator = await client.get_state(alpha_name, "__initiator__")
            if alpha_source is None or beta_source is None:
                raise E2EError("factory-deployed child contracts were not persisted")
            if family_info["first"] != alpha_name or family_info["second"] != beta_name:
                raise E2EError("factory returned unexpected child contract names")
            if alpha_construct["caller"] != factory_name or beta_construct["caller"] != factory_name:
                raise E2EError("child constructor caller did not resolve to the factory contract")
            if alpha_construct["signer"] != operator.public_key:
                raise E2EError("child constructor signer drifted from the external caller")
            if alpha_construct["submission_name"] != alpha_name:
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
                stamps=CONTRACT_ORCHESTRATION_TX_STAMPS["dynamic_call"],
                mode="commit",
            )
            touch_receipt = ensure_positive_submission(
                touch_submission,
                label="orchestration-dynamic-touch",
            )
            alpha_touch_total = await client.call(alpha_name, "get_touch_total", {})
            private_submission = await client.send_tx(
                router_name,
                "private_probe",
                {
                    "target_contract": alpha_name,
                    "function_name": "internal_secret",
                },
                stamps=CONTRACT_ORCHESTRATION_TX_STAMPS["dynamic_call"],
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
                stamps=CONTRACT_ORCHESTRATION_TX_STAMPS["deploy_family"],
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
            "chain_preview": normalize_value(chain_preview),
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
                    stamps=DEFAULT_TX_STAMPS,
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
                    receipt["stamps_used"] = execution.get("stamps_used")
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

        async def one_simulation(index: int) -> dict[str, Any]:
            node_index = index % len(self.nodes)
            async with self.client(sim_wallet, node_index, session) as client:
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
        responses = await asyncio.gather(
            *(one_simulation(index) for index in range(SIMULATOR_BURST_REQUESTS))
        )
        elapsed = time.monotonic() - started
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
            "baseline_sample": baseline[:4],
            "failure_sample": failures[:8],
            "success_sample": successes[:8],
            "recovery": recovery,
        }

    async def bds_catchup_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        service = self.service_node
        if service is None:
            raise E2EError("service node not available for BDS catch-up phase")

        env = make_localnet_env(self.args)
        current_height = await latest_height(session, service.rpc_url)
        catchup_wallets = [
            derive_wallet(self.seed, f"bds-catchup-{index}")
            for index in range(len(self.nodes))
        ]
        await self.fund_wallets(session, catchup_wallets, amount=5_000)

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
                    stamps=DEFAULT_TX_STAMPS,
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
            lagged_status = await wait_for_bds_backlog(
                client,
                target_height=catchup_height,
                timeout_seconds=30.0,
            )

        run_localnet_compose("start", LOCALNET_POSTGRES_SERVICE, env=env)
        healthy_state = await wait_for_container_state(
            LOCALNET_POSTGRES_CONTAINER,
            expected_states={"healthy"},
            timeout_seconds=45.0,
        )

        async with self.client(self.founder_wallet, service.index, session) as client:
            recovered_status = await wait_for_bds_recovered(
                client,
                target_height=catchup_height,
                timeout_seconds=120.0,
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

        self.sample_event_tx_hash = catchup_records[-1]["tx_hash"]
        return {
            "postgres_stopped_state": stopped_state,
            "postgres_recovered_state": healthy_state,
            "baseline_status": baseline_status,
            "lagged_status": lagged_status,
            "recovered_status": recovered_status,
            "catchup_height": catchup_height,
            "catchup_tx_hashes": [record["tx_hash"] for record in catchup_records],
            "catchup_receipts": catchup_records,
            "indexed_tx_sample": indexed_txs,
            "indexed_events_for_last_tx": normalize_value(
                [item.raw for item in indexed_events]
            ),
            "indexed_state_for_last_tx": normalize_value(
                [item.raw for item in indexed_state]
            ),
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
        orchestration_touch_state = None
        orchestrated_alpha = self.contracts.get("orchestrated_alpha")
        if orchestrated_alpha:
            orchestration_touch_state = await query_state_from_all_nodes(
                session,
                self.nodes,
                contract=orchestrated_alpha,
                variable="touch_total",
            )

        unique_state_groups = {
            "conflict_counter": sorted({json.dumps(value, sort_keys=True) for value in counter_state.values()}),
            "patch_mode": sorted({json.dumps(value, sort_keys=True) for value in patch_mode_state.values()}),
        }
        if orchestration_touch_state is not None:
            unique_state_groups["orchestrated_alpha_touch_total"] = sorted(
                {
                    json.dumps(value, sort_keys=True)
                    for value in orchestration_touch_state.values()
                }
            )

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
                stamps=DEFAULT_TX_STAMPS,
                wait_for_tx=True,
            )
            trigger_receipt = ensure_positive_submission(
                trigger,
                label="retrieval-trigger",
            )
            return {"tx_hash": trigger_receipt["tx_hash"], "slot": slot}

    async def validator_governance_phase(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        node0_wallet, node1_wallet, node2_wallet, node3_wallet = self.validator_wallets
        node3_key = self.nodes[3].account_public_key
        await self.fund_wallets(
            session,
            [node1_wallet, node2_wallet, node3_wallet],
            amount=500_000,
        )

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
        self.restart_localnet()
        await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=self.args.rpc_timeout_seconds,
        )

        node0_wallet, node1_wallet, node2_wallet, node3_wallet = self.validator_wallets
        proposal_receipt: dict[str, Any] | None = None
        if existing_patch is None:
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

        return {
            "bundle": bundle_payload,
            "existing_patch": normalize_value(existing_patch),
            "governance_min_patch_delay": governance_min_patch_delay,
            "activation_wait_timeout_seconds": activation_wait_timeout,
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
        self.restart_localnet()
        await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=self.args.rpc_timeout_seconds,
        )

        trigger_wallet = derive_wallet(self.seed, "logging-trigger")
        rejected_wallet = derive_wallet(self.seed, "logging-checktx-reject")
        await self.fund_wallets(session, [trigger_wallet], amount=5_000)
        async with self.client(rejected_wallet, 0, session) as client:
            rejected_submission = await client.send(
                amount=1,
                to_address=self.founder_wallet.public_key,
                stamps=DEFAULT_TRANSFER_STAMPS,
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
                stamps=DEFAULT_TRANSFER_STAMPS,
                wait_for_tx=True,
            )
            debug_receipt = ensure_positive_submission(debug_submission, label="debug-log-trigger")

        update_logging_config(level="TRACE", trace_logging=True, json_logging=False)
        self.restart_localnet()
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
        self.restart_localnet()
        await wait_for_localnet_ready(
            session,
            self.nodes,
            timeout_seconds=self.args.rpc_timeout_seconds,
        )

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
        alice = derive_wallet(self.seed, "shielded-alice")
        bob = derive_wallet(self.seed, "shielded-bob")

        registry_name = "zk_registry"
        token_name = f"con_private_e2e_{short_hash(self.run_id)}"
        shielded_wallet_balance_target = 1_000_000
        vk_registration_proposals: list[dict[str, Any]] = []
        async with self.client(founder, 0, session) as client:
            for wallet in (alice, bob):
                current_balance = await client.get_balance(wallet.public_key)
                delta = shielded_wallet_balance_target - int(current_balance)
                if delta <= 0:
                    continue
                funding = await client.send(
                    amount=delta,
                    to_address=wallet.public_key,
                    stamps=DEFAULT_TRANSFER_STAMPS,
                    wait_for_tx=True,
                )
                ensure_positive_submission(
                    funding,
                    label=f"shielded-topup-{wallet.public_key[:12]}",
                )
            registry_owner = await client.call(registry_name, "owner", {})
            if registry_owner != "governance":
                raise E2EError(
                    f"expected system zk_registry owner to be governance, got {registry_owner!r}"
                )
            token_source = await client.get_contract(token_name)
            if token_source is None:
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
                    stamps=25_000_000,
                    wait_for_tx=True,
                )
                ensure_positive_submission(
                    token_submission,
                    label="deploy-shielded-token",
                )
            prover = ShieldedNoteProver.build_insecure_dev_bundle()
            alice_public_balance = await client.call(
                token_name,
                "balance_of",
                {"account": alice.public_key},
            )
            mint_amount = max(100 - int(alice_public_balance or 0), 0)
            if mint_amount > 0:
                mint_submission = await client.send_tx(
                    token_name,
                    "mint_public",
                    {"amount": mint_amount, "to": alice.public_key},
                    stamps=500_000,
                    wait_for_tx=True,
                )
                ensure_positive_submission(
                    mint_submission,
                    label="shielded-mint-public",
                )
            asset_id = await client.call(token_name, "asset_id", {})
            zero_root = await client.call(token_name, "zero_shielded_root", {})

        node0_wallet, node1_wallet, node2_wallet, node3_wallet = self.validator_wallets
        async with self.client(node0_wallet, 0, session) as node0, self.client(
            node1_wallet, 1, session
        ) as node1, self.client(node2_wallet, 2, session) as node2, self.client(
            node3_wallet, 3, session
        ) as node3:
            for action in ("deposit", "transfer", "withdraw"):
                vk = prover.bundle[action]
                vk_info = await node0.call(
                    registry_name,
                    "get_vk_info",
                    {"vk_id": vk["vk_id"]},
                )
                if vk_info is None:
                    proposal_submission = await node0.send_tx(
                        "governance",
                        "propose_contract_call",
                        {
                            "target_contract": registry_name,
                            "target_function": "register_vk",
                            "kwargs": {
                                "vk_id": vk["vk_id"],
                                "vk_hex": vk["vk_hex"],
                                "circuit_name": vk["circuit_name"],
                                "version": vk["version"],
                            },
                            "summary": (
                                f"register {action} vk for localnet shielded e2e"
                            ),
                        },
                        stamps=5_000_000,
                        wait_for_tx=True,
                    )
                    ensure_positive_submission(
                        proposal_submission,
                        label=f"register-vk-propose-{action}",
                    )
                    proposal_id = await node0.get_state(
                        "governance",
                        "proposal_count",
                    )
                    for governance_client, label in (
                        (node1, f"register-vk-vote-1-{action}"),
                        (node2, f"register-vk-vote-2-{action}"),
                        (node3, f"register-vk-vote-3-{action}"),
                    ):
                        vote_submission = await governance_client.send_tx(
                            "governance",
                            "vote",
                            {"proposal_id": proposal_id, "support": True},
                            stamps=GOVERNANCE_TX_STAMPS,
                            wait_for_tx=True,
                        )
                        ensure_positive_submission(vote_submission, label=label)

                    proposal_status = await node0.call(
                        "governance",
                        "get_proposal",
                        {"proposal_id": proposal_id},
                    )
                    if proposal_status["status"] != "executed":
                        raise E2EError(
                            f"vk registration proposal {proposal_id} did not execute"
                        )
                    vk_registration_proposals.append(
                        normalize_value(proposal_status)
                    )
                    vk_info = await node0.call(
                        registry_name,
                        "get_vk_info",
                        {"vk_id": vk["vk_id"]},
                    )
                if vk_info is None:
                    raise E2EError(f"vk {vk['vk_id']} was not registered")

                binding = await node0.call(
                    token_name,
                    "get_vk_binding",
                    {"action": action},
                )
                if binding is None or binding.get("vk_id") != vk["vk_id"]:
                    bind_submission = await node0.send_tx(
                        token_name,
                        "configure_vk",
                        {"action": action, "vk_id": vk["vk_id"]},
                        stamps=500_000,
                        wait_for_tx=True,
                    )
                    ensure_positive_submission(
                        bind_submission,
                        label=f"bind-vk-{action}",
                    )

        alice_keys = ShieldedKeyBundle.generate()
        bob_keys = ShieldedKeyBundle.generate()

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

        async with self.client(alice, 1, session) as alice_client, self.client(
            founder, 0, session
        ) as founder_client:
            deposit = prover.prove_deposit(
                ShieldedDepositRequest(
                    asset_id=asset_id,
                    old_root=zero_root,
                    append_state=tree_state([]),
                    amount=70,
                    outputs=[alice_note_1.to_output(), alice_note_2.to_output()],
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
                stamps=SHIELDED_TX_STAMPS["deposit"],
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
                stamps=SHIELDED_TX_STAMPS["transfer"],
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
                    amount=20,
                    recipient=bob.public_key,
                    inputs=[discovered_after_transfer[0].to_input()],
                    outputs=[alice_withdraw_change.to_output()],
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
                    "output_payloads": [
                        alice_withdraw_change.to_output().encrypt_for(
                            asset_id=asset_id,
                            viewing_public_key=alice_keys.viewing_public_key,
                        )
                    ],
                },
                stamps=SHIELDED_TX_STAMPS["withdraw"],
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
            "registry_owner": registry_owner,
            "vk_registration_proposals": vk_registration_proposals,
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
            ]
            if start_phase != "00-bootstrap":
                self.load_resume_context()
            start_index = valid_phase_names.index(start_phase)
            for phase_name, fn in phase_sequence[start_index:]:
                await self.run_phase(phase_name, fn)

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
    parser.add_argument("--build", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--nodes", type=int, default=4)
    parser.add_argument("--topology", choices=("integrated", "fidelity"), default="integrated")
    parser.add_argument("--bds-node-index", type=int, default=0)
    parser.add_argument("--port-offset", type=int, default=1000)
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
