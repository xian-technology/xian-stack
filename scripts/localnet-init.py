#!/usr/bin/env python3
"""Generate keys, genesis, and CometBFT config for an N-node local network.

Usage:
    python localnet-init.py --nodes 5 --genesis-network testnet --chain-id xian-localnet-1

Outputs everything under .localnet/node-{i}/.cometbft/ ready to be
mounted into Docker containers.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent

from xian.execution_policy import VM_EXECUTION_MODE  # noqa: E402
from xian.genesis_builder import (  # noqa: E402
    build_local_network_genesis,
)
from xian.node_setup import (  # noqa: E402
    DEFAULT_PARALLEL_EXECUTION_ACCESS_ESTIMATES_ENABLED,
    DEFAULT_PARALLEL_EXECUTION_ENABLED,
    DEFAULT_PARALLEL_EXECUTION_LOW_ACCEPTANCE_MIN_WAVE_SIZE,
    DEFAULT_PARALLEL_EXECUTION_MAX_SPECULATIVE_WAVES,
    DEFAULT_PARALLEL_EXECUTION_MIN_TRANSACTIONS,
    DEFAULT_PARALLEL_EXECUTION_MIN_WAVE_ACCEPTANCE_RATIO,
    DEFAULT_PARALLEL_EXECUTION_WARM_WORKERS,
    DEFAULT_PARALLEL_EXECUTION_WORKERS,
    AppLoggingOptions,
    BdsOptions,
    MetricsOptions,
    NodeConfigOptions,
    ParallelExecutionOptions,
    build_node_key,
    generate_validator_material,
    materialize_cometbft_home,
    render_node_configs,
)

LOCALNET_DIR = STACK_DIR / ".localnet"
CONFIGS_DIR = STACK_DIR.parent / "xian-configs"

# Port offsets from base for each node
BASE_P2P_PORT = 26656
BASE_RPC_PORT = 26657
BASE_METRICS_PORT = 26660
BASE_XIAN_METRICS_PORT = 9108
PORT_STRIDE = 100  # node-0: 266xx, node-1: 267xx, node-2: 268xx, ...
NODE_IMAGE_INTEGRATED = "xian-node-integrated:local"
NODE_IMAGE_SPLIT = "xian-node-split:local"
LOCALNET_POSTGRES_SERVICE = "localnet-postgres"
COMPOSE_DEFAULTS = {
    "XIAN_ABCI_DIR": "../xian-abci",
    "XIAN_CONFIGS_DIR": "../xian-configs",
    "XIAN_CONTRACTING_DIR": "../xian-contracting",
    "XIAN_PY_DIR": "../xian-py",
    "XIAN_COMETBFT_VERSION": "0.39.3",
    "XIAN_S6_OVERLAY_VERSION": "3.2.1.0",
    "XIAN_S6_VERBOSITY": "1",
    "XIAN_DOCKER_POSTGRES_MEMORY_LIMIT": "1024m",
    "XIAN_DOCKER_POSTGRES_MEMORY_RESERVATION": "512m",
    "XIAN_DOCKER_POSTGRES_MEMORY_SWAP": "1024m",
    "XIAN_DOCKER_POSTGRES_PIDS_LIMIT": "256",
    "XIAN_DOCKER_POSTGRES_NOFILE_SOFT": "65536",
    "XIAN_DOCKER_POSTGRES_NOFILE_HARD": "65536",
    "XIAN_LOCALNET_NODE_MEMORY_LIMIT": "1536m",
    "XIAN_LOCALNET_NODE_MEMORY_RESERVATION": "1024m",
    "XIAN_LOCALNET_NODE_MEMORY_SWAP": "1536m",
    "XIAN_LOCALNET_NODE_PIDS_LIMIT": "512",
    "XIAN_LOCALNET_NODE_NOFILE_SOFT": "65536",
    "XIAN_LOCALNET_NODE_NOFILE_HARD": "65536",
    "XIAN_LOCALNET_ABCI_MEMORY_LIMIT": "1024m",
    "XIAN_LOCALNET_ABCI_MEMORY_RESERVATION": "768m",
    "XIAN_LOCALNET_ABCI_MEMORY_SWAP": "1024m",
    "XIAN_LOCALNET_ABCI_PIDS_LIMIT": "384",
    "XIAN_LOCALNET_ABCI_NOFILE_SOFT": "65536",
    "XIAN_LOCALNET_ABCI_NOFILE_HARD": "65536",
    "XIAN_LOCALNET_COMETBFT_MEMORY_LIMIT": "512m",
    "XIAN_LOCALNET_COMETBFT_MEMORY_RESERVATION": "256m",
    "XIAN_LOCALNET_COMETBFT_MEMORY_SWAP": "512m",
    "XIAN_LOCALNET_COMETBFT_PIDS_LIMIT": "256",
    "XIAN_LOCALNET_COMETBFT_NOFILE_SOFT": "65536",
    "XIAN_LOCALNET_COMETBFT_NOFILE_HARD": "65536",
}


def compose_var(name: str) -> str:
    return f"${{{name}:-{COMPOSE_DEFAULTS[name]}}}"


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return float(raw)


def env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def env_optional_str(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def env_optional_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_optional_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return int(raw)


def node_build_config(target: str) -> dict:
    return {
        "context": ".",
        "dockerfile": "./docker/xian-node.Dockerfile",
        "target": target,
        "additional_contexts": {
            "xian-abci": compose_var("XIAN_ABCI_DIR"),
            "xian-configs": compose_var("XIAN_CONFIGS_DIR"),
            "xian-contracting": compose_var("XIAN_CONTRACTING_DIR"),
            "xian-py": compose_var("XIAN_PY_DIR"),
        },
        "args": {
            "COMETBFT_VERSION": compose_var("XIAN_COMETBFT_VERSION"),
            "S6_OVERLAY_VERSION": compose_var("XIAN_S6_OVERLAY_VERSION"),
        },
    }


def generate_node_material(index: int) -> dict:
    """Generate validator key, node key, and metadata for one node."""
    node_seed = secrets.token_bytes(32)

    validator_material = generate_validator_material(secrets.token_bytes(32).hex())
    node_key = build_node_key(node_seed.hex())

    return {
        "index": index,
        "moniker": f"node-{index}",
        "validator_material": validator_material,
        "node_key": node_key,
        "account_public_key": validator_material["validator_public_key_hex"],
    }


def build_persistent_peers(nodes: list[dict]) -> str:
    """Build the persistent_peers string for CometBFT config.

    Inside Docker, containers address each other by service name.
    Each node's P2P port inside the container is always 26656.
    """
    peers = []
    for n in nodes:
        node_id = n["node_key"]["node_id"].lower()
        hostname = n["moniker"]
        peers.append(f"{node_id}@{hostname}:26656")
    return ",".join(peers)


def bds_runtime_rpc_url(node: dict, topology: str) -> str:
    """Return the RPC URL reachable from the BDS process."""
    if topology == "fidelity":
        return f"http://{node['moniker']}:26657"
    return "http://127.0.0.1:26657"


def write_node_config(
    node: dict,
    all_nodes: list[dict],
    chain_id: str,
    genesis: dict,
    *,
    block_policy_mode: str,
    block_policy_interval: str,
    consensus_timeout_propose: str | None,
    consensus_timeout_propose_delta: str | None,
    consensus_timeout_prevote: str | None,
    consensus_timeout_prevote_delta: str | None,
    consensus_timeout_precommit: str | None,
    consensus_timeout_precommit_delta: str | None,
    consensus_timeout_commit: str | None,
    consensus_skip_timeout_commit: bool | None,
    mempool_size: int | None,
    mempool_cache_size: int | None,
    bds_enabled: bool,
    topology: str,
    parallel_execution_enabled: bool,
    parallel_execution_workers: int,
    parallel_execution_min_transactions: int,
    parallel_execution_max_speculative_waves: int,
    parallel_execution_min_wave_acceptance_ratio: float,
    parallel_execution_low_acceptance_min_wave_size: int,
    parallel_execution_warm_workers: bool,
    parallel_execution_access_estimates_enabled: bool,
    transaction_trace_logging: bool,
    app_log_level: str,
    app_log_json: bool,
    app_log_rotation_hours: int,
    app_log_retention_days: int,
):
    """Write all CometBFT config files for a single node."""
    home = LOCALNET_DIR / node["moniker"] / ".cometbft"

    # Exclude self from persistent_peers
    other_nodes = [n for n in all_nodes if n["index"] != node["index"]]
    peers = build_persistent_peers(other_nodes)

    configs = render_node_configs(
        options=NodeConfigOptions(
            moniker=node["moniker"],
            bds_enabled=bds_enabled,
            allow_cors=True,
            prometheus=True,
            block_policy_mode=block_policy_mode,
            block_policy_interval=block_policy_interval,
            metrics=MetricsOptions(
                enabled=True,
                host="0.0.0.0",
                port=9108,
            ),
            transaction_trace_logging=transaction_trace_logging,
            app_logging=AppLoggingOptions(
                level=app_log_level,
                json_logging=app_log_json,
                rotation_hours=app_log_rotation_hours,
                retention_days=app_log_retention_days,
            ),
            parallel_execution=ParallelExecutionOptions(
                enabled=parallel_execution_enabled,
                workers=parallel_execution_workers,
                min_transactions=parallel_execution_min_transactions,
                max_speculative_waves=parallel_execution_max_speculative_waves,
                min_wave_acceptance_ratio=(parallel_execution_min_wave_acceptance_ratio),
                low_acceptance_min_wave_size=(parallel_execution_low_acceptance_min_wave_size),
                warm_workers=parallel_execution_warm_workers,
                access_estimates_enabled=(parallel_execution_access_estimates_enabled),
            ),
            bds=BdsOptions(
                host=LOCALNET_POSTGRES_SERVICE if bds_enabled else "",
                port=5432,
                database="xian",
                user="xian",
                password="xian",
                rpc_url=bds_runtime_rpc_url(node, topology) if bds_enabled else "",
            ),
        )
    )
    config = configs["cometbft"]
    xian_config = configs["xian"]
    # Override peers and listen addresses (inside container, always same ports)
    config["p2p"]["persistent_peers"] = peers
    config["p2p"]["laddr"] = "tcp://0.0.0.0:26656"
    config["p2p"]["addr_book_strict"] = False
    config["p2p"]["allow_duplicate_ip"] = True
    config["rpc"]["laddr"] = "tcp://0.0.0.0:26657"
    if consensus_timeout_propose is not None:
        config["consensus"]["timeout_propose"] = consensus_timeout_propose
    if consensus_timeout_propose_delta is not None:
        config["consensus"]["timeout_propose_delta"] = consensus_timeout_propose_delta
    if consensus_timeout_prevote is not None:
        config["consensus"]["timeout_prevote"] = consensus_timeout_prevote
    if consensus_timeout_prevote_delta is not None:
        config["consensus"]["timeout_prevote_delta"] = consensus_timeout_prevote_delta
    if consensus_timeout_precommit is not None:
        config["consensus"]["timeout_precommit"] = consensus_timeout_precommit
    if consensus_timeout_precommit_delta is not None:
        config["consensus"]["timeout_precommit_delta"] = consensus_timeout_precommit_delta
    if consensus_timeout_commit is not None:
        config["consensus"]["timeout_commit"] = consensus_timeout_commit
    if consensus_skip_timeout_commit is not None:
        config["consensus"]["skip_timeout_commit"] = consensus_skip_timeout_commit
    if mempool_size is not None:
        config["mempool"]["size"] = mempool_size
    if mempool_cache_size is not None:
        config["mempool"]["cache_size"] = mempool_cache_size

    materialize_cometbft_home(
        home=home,
        config=config,
        xian_config=xian_config,
        genesis=genesis,
        priv_validator_key=node["validator_material"]["priv_validator_key"],
        node_key=node["node_key"],
    )


def main():
    global BASE_P2P_PORT, BASE_RPC_PORT, BASE_METRICS_PORT, BASE_XIAN_METRICS_PORT
    parser = argparse.ArgumentParser(description="Initialize a local N-node network")
    parser.add_argument(
        "--nodes",
        "-n",
        type=int,
        default=4,
        help="Number of validator nodes (minimum 4)",
    )
    parser.add_argument(
        "--chain-id",
        default="xian-localnet-1",
        help="Chain ID for the network",
    )
    parser.add_argument(
        "--genesis-network",
        default=env_str("XIAN_LOCALNET_GENESIS_NETWORK", "local"),
        help=(
            "Genesis bundle name used to seed the localnet genesis "
            "(for example: local, devnet, testnet)"
        ),
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing .localnet directory before init",
    )
    parser.add_argument(
        "--topology",
        choices=("integrated", "fidelity"),
        default="integrated",
        help="Runtime topology for the generated localnet compose file",
    )
    args = parser.parse_args()

    if args.nodes < 4:
        print("ERROR: Minimum 4 nodes required for BFT consensus", file=sys.stderr)
        sys.exit(1)

    if args.clean and LOCALNET_DIR.exists():
        print(f"Cleaning {LOCALNET_DIR}")
        shutil.rmtree(LOCALNET_DIR)

    if LOCALNET_DIR.exists():
        print(f"ERROR: {LOCALNET_DIR} already exists. Use --clean to overwrite.", file=sys.stderr)
        sys.exit(1)

    print(
        "Generating "
        f"{args.nodes}-node localnet "
        f"(chain_id={args.chain_id}, genesis_network={args.genesis_network})"
    )
    parallel_execution_enabled = env_bool(
        "XIAN_LOCALNET_PARALLEL_EXECUTION_ENABLED",
        DEFAULT_PARALLEL_EXECUTION_ENABLED,
    )
    port_offset = env_int("XIAN_LOCALNET_PORT_OFFSET", 0)
    BASE_P2P_PORT = 26656 + port_offset
    BASE_RPC_PORT = 26657 + port_offset
    BASE_METRICS_PORT = 26660 + port_offset
    BASE_XIAN_METRICS_PORT = 9108 + port_offset
    bds_enabled = env_bool("XIAN_LOCALNET_ENABLE_BDS", False)
    bds_node_index = env_int("XIAN_LOCALNET_BDS_NODE_INDEX", 0)
    if bds_enabled and not 0 <= bds_node_index < args.nodes:
        print(
            "ERROR: XIAN_LOCALNET_BDS_NODE_INDEX must point to an existing node",
            file=sys.stderr,
        )
        sys.exit(1)
    parallel_execution_workers = env_int(
        "XIAN_LOCALNET_PARALLEL_EXECUTION_WORKERS",
        DEFAULT_PARALLEL_EXECUTION_WORKERS,
    )
    parallel_execution_min_transactions = env_int(
        "XIAN_LOCALNET_PARALLEL_EXECUTION_MIN_TRANSACTIONS",
        DEFAULT_PARALLEL_EXECUTION_MIN_TRANSACTIONS,
    )
    parallel_execution_max_speculative_waves = env_int(
        "XIAN_LOCALNET_PARALLEL_EXECUTION_MAX_SPECULATIVE_WAVES",
        DEFAULT_PARALLEL_EXECUTION_MAX_SPECULATIVE_WAVES,
    )
    parallel_execution_min_wave_acceptance_ratio = env_float(
        "XIAN_LOCALNET_PARALLEL_EXECUTION_MIN_WAVE_ACCEPTANCE_RATIO",
        DEFAULT_PARALLEL_EXECUTION_MIN_WAVE_ACCEPTANCE_RATIO,
    )
    parallel_execution_low_acceptance_min_wave_size = env_int(
        "XIAN_LOCALNET_PARALLEL_EXECUTION_LOW_ACCEPTANCE_MIN_WAVE_SIZE",
        DEFAULT_PARALLEL_EXECUTION_LOW_ACCEPTANCE_MIN_WAVE_SIZE,
    )
    parallel_execution_warm_workers = env_bool(
        "XIAN_LOCALNET_PARALLEL_EXECUTION_WARM_WORKERS",
        DEFAULT_PARALLEL_EXECUTION_WARM_WORKERS,
    )
    parallel_execution_access_estimates_enabled = env_bool(
        "XIAN_LOCALNET_PARALLEL_EXECUTION_ACCESS_ESTIMATES_ENABLED",
        DEFAULT_PARALLEL_EXECUTION_ACCESS_ESTIMATES_ENABLED,
    )
    transaction_trace_logging = env_bool("XIAN_LOCALNET_TRANSACTION_TRACE_LOGGING", False)
    # Localnet matches the in-process dev client (zk on); real-network genesis
    # stays explicit opt-in via configure_node --runtime-feature-zk.
    runtime_feature_zk = env_bool("XIAN_LOCALNET_RUNTIME_FEATURE_ZK", True)
    app_log_level = env_str("XIAN_LOCALNET_APP_LOG_LEVEL", "INFO")
    app_log_json = env_bool("XIAN_LOCALNET_APP_LOG_JSON", False)
    app_log_rotation_hours = env_int("XIAN_LOCALNET_APP_LOG_ROTATION_HOURS", 1)
    app_log_retention_days = env_int("XIAN_LOCALNET_APP_LOG_RETENTION_DAYS", 7)
    profiling_enabled = env_bool(
        "XIAN_LOCALNET_PROFILE_ENABLED",
        env_bool("XIAN_PERF_ENABLED", False),
    )
    profiling_recent_blocks = env_int(
        "XIAN_LOCALNET_PROFILE_RECENT_BLOCKS",
        env_int("XIAN_PERF_RECENT_BLOCKS", 32),
    )
    localnet_profile = env_str("XIAN_LOCALNET_PROFILE", "default")
    if localnet_profile not in {"default", "throughput"}:
        print(
            "ERROR: XIAN_LOCALNET_PROFILE must be one of: default, throughput",
            file=sys.stderr,
        )
        sys.exit(1)

    block_policy_mode = env_optional_str("XIAN_LOCALNET_BLOCK_POLICY_MODE")
    block_policy_interval = env_optional_str("XIAN_LOCALNET_BLOCK_POLICY_INTERVAL")
    consensus_timeout_propose = env_optional_str("XIAN_LOCALNET_CONSENSUS_TIMEOUT_PROPOSE")
    consensus_timeout_propose_delta = env_optional_str(
        "XIAN_LOCALNET_CONSENSUS_TIMEOUT_PROPOSE_DELTA"
    )
    consensus_timeout_prevote = env_optional_str("XIAN_LOCALNET_CONSENSUS_TIMEOUT_PREVOTE")
    consensus_timeout_prevote_delta = env_optional_str(
        "XIAN_LOCALNET_CONSENSUS_TIMEOUT_PREVOTE_DELTA"
    )
    consensus_timeout_precommit = env_optional_str("XIAN_LOCALNET_CONSENSUS_TIMEOUT_PRECOMMIT")
    consensus_timeout_precommit_delta = env_optional_str(
        "XIAN_LOCALNET_CONSENSUS_TIMEOUT_PRECOMMIT_DELTA"
    )
    consensus_timeout_commit = env_optional_str("XIAN_LOCALNET_CONSENSUS_TIMEOUT_COMMIT")
    consensus_skip_timeout_commit = env_optional_bool("XIAN_LOCALNET_SKIP_TIMEOUT_COMMIT")
    mempool_size = env_optional_int("XIAN_LOCALNET_MEMPOOL_SIZE")
    mempool_cache_size = env_optional_int("XIAN_LOCALNET_MEMPOOL_CACHE_SIZE")

    if localnet_profile == "throughput":
        block_policy_mode = block_policy_mode or "on_demand"
        block_policy_interval = block_policy_interval or "0s"
        consensus_timeout_propose = consensus_timeout_propose or "500ms"
        consensus_timeout_propose_delta = consensus_timeout_propose_delta or "100ms"
        consensus_timeout_prevote = consensus_timeout_prevote or "200ms"
        consensus_timeout_prevote_delta = consensus_timeout_prevote_delta or "50ms"
        consensus_timeout_precommit = consensus_timeout_precommit or "200ms"
        consensus_timeout_precommit_delta = consensus_timeout_precommit_delta or "50ms"
        consensus_timeout_commit = consensus_timeout_commit or "200ms"
        if consensus_skip_timeout_commit is None:
            consensus_skip_timeout_commit = True
        if mempool_size is None:
            mempool_size = 50_000
        if mempool_cache_size is None:
            mempool_cache_size = 100_000
    else:
        block_policy_mode = block_policy_mode or "periodic"
        block_policy_interval = block_policy_interval or "5s"

    # 1. Generate key material for all nodes
    nodes = [generate_node_material(i) for i in range(args.nodes)]

    # 2. Build genesis using the first node's key as founder
    founder_key = nodes[0]["validator_material"]["validator_private_key_hex"]
    validators = [
        {
            "account_public_key": n["account_public_key"],
            "name": n["moniker"],
            "power": 10,
            "priv_validator_key": n["validator_material"]["priv_validator_key"],
        }
        for n in nodes
    ]

    print(f"Building genesis block (submitting {args.genesis_network} system contracts)...")
    genesis = build_local_network_genesis(
        chain_id=args.chain_id,
        founder_private_key=founder_key,
        validators=validators,
        network=args.genesis_network,
        contracts_dir=CONFIGS_DIR / "contracts",
        runtime_features={"zk": runtime_feature_zk},
    )
    print(f"  Genesis has {len(genesis.get('validators', []))} validators")

    # 3. Write per-node config
    for node in nodes:
        write_node_config(
            node,
            nodes,
            args.chain_id,
            genesis,
            block_policy_mode=block_policy_mode,
            block_policy_interval=block_policy_interval,
            consensus_timeout_propose=consensus_timeout_propose,
            consensus_timeout_propose_delta=consensus_timeout_propose_delta,
            consensus_timeout_prevote=consensus_timeout_prevote,
            consensus_timeout_prevote_delta=consensus_timeout_prevote_delta,
            consensus_timeout_precommit=consensus_timeout_precommit,
            consensus_timeout_precommit_delta=(consensus_timeout_precommit_delta),
            consensus_timeout_commit=consensus_timeout_commit,
            consensus_skip_timeout_commit=consensus_skip_timeout_commit,
            mempool_size=mempool_size,
            mempool_cache_size=mempool_cache_size,
            bds_enabled=bds_enabled and node["index"] == bds_node_index,
            topology=args.topology,
            parallel_execution_enabled=parallel_execution_enabled,
            parallel_execution_workers=parallel_execution_workers,
            parallel_execution_min_transactions=(parallel_execution_min_transactions),
            parallel_execution_max_speculative_waves=(parallel_execution_max_speculative_waves),
            parallel_execution_min_wave_acceptance_ratio=(
                parallel_execution_min_wave_acceptance_ratio
            ),
            parallel_execution_low_acceptance_min_wave_size=(
                parallel_execution_low_acceptance_min_wave_size
            ),
            parallel_execution_warm_workers=parallel_execution_warm_workers,
            parallel_execution_access_estimates_enabled=(
                parallel_execution_access_estimates_enabled
            ),
            transaction_trace_logging=transaction_trace_logging,
            app_log_level=app_log_level,
            app_log_json=app_log_json,
            app_log_rotation_hours=app_log_rotation_hours,
            app_log_retention_days=app_log_retention_days,
        )
        (LOCALNET_DIR / node["moniker"] / "tmp").mkdir(parents=True, exist_ok=True)
        idx = node["index"]
        host_p2p = BASE_P2P_PORT + idx * PORT_STRIDE
        host_rpc = BASE_RPC_PORT + idx * PORT_STRIDE
        node_id_prefix = node["node_key"]["node_id"][:12]
        print(f"  {node['moniker']}: RPC=:{host_rpc} P2P=:{host_p2p} id={node_id_prefix}...")

    # 4. Write docker-compose-localnet.yml
    write_compose_file(
        nodes,
        args.topology,
        bds_enabled=bds_enabled,
        bds_node_index=bds_node_index,
        profiling_enabled=profiling_enabled,
        profiling_recent_blocks=profiling_recent_blocks,
    )

    # 5. Write node summary for scripts
    summary = {
        "chain_id": args.chain_id,
        "genesis_network": args.genesis_network,
        "topology": args.topology,
        "execution": {
            "mode": VM_EXECUTION_MODE,
        },
        "runtime_features": {
            "zk": runtime_feature_zk,
        },
        "nodes": [
            {
                "moniker": n["moniker"],
                "node_id": n["node_key"]["node_id"],
                "host_rpc_port": BASE_RPC_PORT + n["index"] * PORT_STRIDE,
                "host_p2p_port": BASE_P2P_PORT + n["index"] * PORT_STRIDE,
                "host_metrics_port": BASE_METRICS_PORT + n["index"] * PORT_STRIDE,
                "host_xian_metrics_port": (BASE_XIAN_METRICS_PORT + n["index"] * PORT_STRIDE),
                "abci_container": (
                    f"xian-{n['moniker']}"
                    if args.topology == "integrated"
                    else f"xian-{n['moniker']}-abci"
                ),
                "cometbft_container": f"xian-{n['moniker']}",
                "account_public_key": n["account_public_key"],
                "account_private_key": n["validator_material"]["validator_private_key_hex"],
                "bds_enabled": bds_enabled and n["index"] == bds_node_index,
            }
            for n in nodes
        ],
        "founder_key": founder_key,
        "parallel_execution": {
            "enabled": parallel_execution_enabled,
            "workers": parallel_execution_workers,
            "min_transactions": parallel_execution_min_transactions,
            "max_speculative_waves": parallel_execution_max_speculative_waves,
            "min_wave_acceptance_ratio": (parallel_execution_min_wave_acceptance_ratio),
            "low_acceptance_min_wave_size": (parallel_execution_low_acceptance_min_wave_size),
            "warm_workers": parallel_execution_warm_workers,
            "access_estimates_enabled": (parallel_execution_access_estimates_enabled),
        },
        "profiling": {
            "enabled": profiling_enabled,
            "recent_blocks": profiling_recent_blocks,
        },
        "localnet_profile": localnet_profile,
        "consensus": {
            "block_policy_mode": block_policy_mode,
            "block_policy_interval": block_policy_interval,
            "timeout_propose": consensus_timeout_propose,
            "timeout_propose_delta": consensus_timeout_propose_delta,
            "timeout_prevote": consensus_timeout_prevote,
            "timeout_prevote_delta": consensus_timeout_prevote_delta,
            "timeout_precommit": consensus_timeout_precommit,
            "timeout_precommit_delta": consensus_timeout_precommit_delta,
            "timeout_commit": consensus_timeout_commit,
            "skip_timeout_commit": consensus_skip_timeout_commit,
        },
        "mempool": {
            "size": mempool_size,
            "cache_size": mempool_cache_size,
        },
        "port_offset": port_offset,
        "bds": {
            "enabled": bds_enabled,
            "bds_node_index": bds_node_index if bds_enabled else None,
            "bds_rpc_url": (
                f"http://127.0.0.1:{BASE_RPC_PORT + bds_node_index * PORT_STRIDE}"
                if bds_enabled
                else None
            ),
        },
        "logging": {
            "transaction_trace_logging": transaction_trace_logging,
            "app_log_level": app_log_level,
            "app_log_json": app_log_json,
            "app_log_rotation_hours": app_log_rotation_hours,
            "app_log_retention_days": app_log_retention_days,
        },
    }
    (LOCALNET_DIR / "network.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\nLocalnet initialized in {LOCALNET_DIR}")
    print("Start with: make localnet-up")


def write_compose_file(
    nodes: list[dict],
    topology: str,
    *,
    bds_enabled: bool,
    bds_node_index: int,
    profiling_enabled: bool,
    profiling_recent_blocks: int,
):
    """Generate docker-compose-localnet.yml from node list."""
    services = {}
    integrated_build = node_build_config("integrated")
    split_build = node_build_config("split")
    integrated_service_base = {
        "image": NODE_IMAGE_INTEGRATED,
        "restart": "always",
        "stop_grace_period": "45s",
        "build": integrated_build,
        "mem_limit": compose_var("XIAN_LOCALNET_NODE_MEMORY_LIMIT"),
        "mem_reservation": compose_var("XIAN_LOCALNET_NODE_MEMORY_RESERVATION"),
        "memswap_limit": compose_var("XIAN_LOCALNET_NODE_MEMORY_SWAP"),
        "pids_limit": compose_var("XIAN_LOCALNET_NODE_PIDS_LIMIT"),
        "ulimits": {
            "nofile": {
                "soft": compose_var("XIAN_LOCALNET_NODE_NOFILE_SOFT"),
                "hard": compose_var("XIAN_LOCALNET_NODE_NOFILE_HARD"),
            }
        },
        "environment": {
            "XIAN_CONFIGS_DIR": "/opt/xian-configs",
            "S6_VERBOSITY": compose_var("XIAN_S6_VERBOSITY"),
            "XIAN_PERF_ENABLED": "1" if profiling_enabled else "0",
            "XIAN_PERF_OUTPUT_PATH": "/root/.cometbft/xian-perf.json",
            "XIAN_PERF_RECENT_BLOCKS": str(profiling_recent_blocks),
        },
        "networks": ["localnet"],
    }
    if bds_enabled:
        services[LOCALNET_POSTGRES_SERVICE] = {
            "image": "postgres:17",
            "restart": "always",
            "init": True,
            "stop_grace_period": "30s",
            "hostname": LOCALNET_POSTGRES_SERVICE,
            "container_name": f"xian-{LOCALNET_POSTGRES_SERVICE}",
            "mem_limit": compose_var("XIAN_DOCKER_POSTGRES_MEMORY_LIMIT"),
            "mem_reservation": compose_var("XIAN_DOCKER_POSTGRES_MEMORY_RESERVATION"),
            "memswap_limit": compose_var("XIAN_DOCKER_POSTGRES_MEMORY_SWAP"),
            "pids_limit": compose_var("XIAN_DOCKER_POSTGRES_PIDS_LIMIT"),
            "ulimits": {
                "nofile": {
                    "soft": compose_var("XIAN_DOCKER_POSTGRES_NOFILE_SOFT"),
                    "hard": compose_var("XIAN_DOCKER_POSTGRES_NOFILE_HARD"),
                }
            },
            "environment": {
                "POSTGRES_USER": "xian",
                "POSTGRES_PASSWORD": "xian",
                "POSTGRES_DB": "xian",
            },
            "healthcheck": {
                "test": [
                    "CMD-SHELL",
                    "pg_isready -U xian -d xian",
                ],
                "interval": "5s",
                "timeout": "5s",
                "retries": 12,
            },
            "volumes": [
                "./.localnet/postgres:/var/lib/postgresql/data",
            ],
            "expose": ["5432"],
            "networks": ["localnet"],
        }
    for node in nodes:
        idx = node["index"]
        moniker = node["moniker"]
        host_p2p = BASE_P2P_PORT + idx * PORT_STRIDE
        host_rpc = BASE_RPC_PORT + idx * PORT_STRIDE
        host_metrics = BASE_METRICS_PORT + idx * PORT_STRIDE
        host_xian_metrics = BASE_XIAN_METRICS_PORT + idx * PORT_STRIDE
        if topology == "integrated":
            service = {
                **integrated_service_base,
                "hostname": moniker,
                "container_name": f"xian-{moniker}",
                "volumes": [
                    f"./.localnet/{moniker}/.cometbft:/root/.cometbft",
                ],
                "environment": {
                    **integrated_service_base["environment"],
                    "NODE_INDEX": str(idx),
                },
                "ports": [
                    f"{host_p2p}:26656",
                    f"{host_rpc}:26657",
                    f"{host_metrics}:26660",
                    f"{host_xian_metrics}:9108",
                ],
            }
            services[moniker] = {key: value for key, value in service.items()}
            if bds_enabled and idx == bds_node_index:
                services[moniker]["depends_on"] = {
                    LOCALNET_POSTGRES_SERVICE: {
                        "condition": "service_healthy",
                    }
                }
        else:
            shared_tmp = f"./.localnet/{moniker}/tmp:/tmp"
            home_mount = f"./.localnet/{moniker}/.cometbft:/root/.cometbft"
            services[f"{moniker}-abci"] = {
                "image": NODE_IMAGE_SPLIT,
                "restart": "always",
                "init": True,
                "stop_grace_period": "45s",
                "hostname": f"{moniker}-abci",
                "container_name": f"xian-{moniker}-abci",
                "build": split_build,
                "mem_limit": compose_var("XIAN_LOCALNET_ABCI_MEMORY_LIMIT"),
                "mem_reservation": compose_var("XIAN_LOCALNET_ABCI_MEMORY_RESERVATION"),
                "memswap_limit": compose_var("XIAN_LOCALNET_ABCI_MEMORY_SWAP"),
                "pids_limit": compose_var("XIAN_LOCALNET_ABCI_PIDS_LIMIT"),
                "ulimits": {
                    "nofile": {
                        "soft": compose_var("XIAN_LOCALNET_ABCI_NOFILE_SOFT"),
                        "hard": compose_var("XIAN_LOCALNET_ABCI_NOFILE_HARD"),
                    }
                },
                "volumes": [home_mount, shared_tmp],
                "environment": {
                    "XIAN_CONFIGS_DIR": "/opt/xian-configs",
                    "NODE_INDEX": str(idx),
                    "XIAN_PERF_ENABLED": "1" if profiling_enabled else "0",
                    "XIAN_PERF_OUTPUT_PATH": "/root/.cometbft/xian-perf.json",
                    "XIAN_PERF_RECENT_BLOCKS": str(profiling_recent_blocks),
                },
                "command": ["xian-abci"],
                "ports": [
                    f"{host_xian_metrics}:9108",
                ],
                "networks": ["localnet"],
            }
            if bds_enabled and idx == bds_node_index:
                services[f"{moniker}-abci"]["depends_on"] = {
                    LOCALNET_POSTGRES_SERVICE: {
                        "condition": "service_healthy",
                    }
                }
            services[moniker] = {
                "image": NODE_IMAGE_SPLIT,
                "restart": "always",
                "init": True,
                "stop_grace_period": "45s",
                "hostname": moniker,
                "container_name": f"xian-{moniker}",
                "build": split_build,
                "mem_limit": compose_var("XIAN_LOCALNET_COMETBFT_MEMORY_LIMIT"),
                "mem_reservation": compose_var("XIAN_LOCALNET_COMETBFT_MEMORY_RESERVATION"),
                "memswap_limit": compose_var("XIAN_LOCALNET_COMETBFT_MEMORY_SWAP"),
                "pids_limit": compose_var("XIAN_LOCALNET_COMETBFT_PIDS_LIMIT"),
                "ulimits": {
                    "nofile": {
                        "soft": compose_var("XIAN_LOCALNET_COMETBFT_NOFILE_SOFT"),
                        "hard": compose_var("XIAN_LOCALNET_COMETBFT_NOFILE_HARD"),
                    }
                },
                "volumes": [home_mount, shared_tmp],
                "command": [
                    "cometbft",
                    "node",
                    "--rpc.laddr",
                    "tcp://0.0.0.0:26657",
                ],
                "ports": [
                    f"{host_p2p}:26656",
                    f"{host_rpc}:26657",
                    f"{host_metrics}:26660",
                ],
                "networks": ["localnet"],
            }

    compose = {
        "networks": {
            "localnet": {
                "driver": "bridge",
            },
        },
    }
    compose["services"] = services

    compose_path = STACK_DIR / "docker-compose-localnet.yml"
    compose_path.write_text(json.dumps(compose, indent=2) + "\n", encoding="utf-8")

    print(f"  Wrote {compose_path}")


if __name__ == "__main__":
    main()
