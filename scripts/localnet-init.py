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
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from genesis_fee_policy import genesis_fee_policy  # noqa: E402
from xian.execution_policy import VM_EXECUTION_MODE  # noqa: E402
from xian.genesis_builder import (  # noqa: E402
    build_local_network_genesis,
)
from xian.node_setup import (  # noqa: E402
    AppLoggingOptions,
    BdsOptions,
    MetricsOptions,
    NodeConfigOptions,
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
NODE_IMAGE_INTEGRATED = os.environ.get("XIAN_NODE_INTEGRATED_IMAGE", "xian-node-integrated:local")
NODE_IMAGE_SPLIT = "xian-node-split:local"
LOCALNET_POSTGRES_SERVICE = "localnet-postgres"
LOCALNET_COMPOSE_PROJECT = "xian-localnet"
COMPOSE_DEFAULTS = {
    "XIAN_ABCI_DIR": "../xian-abci",
    "XIAN_CONFIGS_DIR": "../xian-configs",
    "XIAN_CONTRACTING_DIR": "../xian-contracting",
    "XIAN_PY_DIR": "../xian-py",
    "XIAN_COMETBFT_VERSION": "0.39.3",
    "XIAN_S6_OVERLAY_VERSION": "3.2.3.0",
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
    transaction_trace_logging: bool,
    app_log_level: str,
    app_log_json: bool,
    app_log_rotation_hours: int,
    app_log_retention_days: int,
    app_log_max_file_mb: int,
    app_log_max_total_mb: int,
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
                max_file_mb=app_log_max_file_mb,
                max_total_mb=app_log_max_total_mb,
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


def build_localnet_genesis(
    nodes: list[dict],
    *,
    chain_id: str,
    genesis_network: str,
    runtime_feature_zk: bool,
    fee_policy: dict,
) -> dict:
    """Genesis for the localnet; the first node's key is the founder."""
    validators = [
        {
            "account_public_key": n["account_public_key"],
            "name": n["moniker"],
            "power": 10,
            "priv_validator_key": n["validator_material"]["priv_validator_key"],
        }
        for n in nodes
    ]
    return build_local_network_genesis(
        chain_id=chain_id,
        founder_private_key=nodes[0]["validator_material"]["validator_private_key_hex"],
        validators=validators,
        network=genesis_network,
        contracts_dir=CONFIGS_DIR / "contracts",
        runtime_features={"zk": runtime_feature_zk},
        fee_policy=fee_policy,
    )


PROFILE_CONSENSUS_DEFAULTS = {
    "default": {"block_policy_mode": "periodic", "block_policy_interval": "5s"},
    "throughput": {
        "block_policy_mode": "on_demand",
        "block_policy_interval": "0s",
        "consensus_timeout_propose": "500ms",
        "consensus_timeout_propose_delta": "100ms",
        "consensus_timeout_prevote": "200ms",
        "consensus_timeout_prevote_delta": "50ms",
        "consensus_timeout_precommit": "200ms",
        "consensus_timeout_precommit_delta": "50ms",
        "consensus_timeout_commit": "200ms",
        "consensus_skip_timeout_commit": True,
        "mempool_size": 50_000,
        "mempool_cache_size": 100_000,
    },
}


def parse_args() -> argparse.Namespace:
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
    return parser.parse_args()


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def prepare_localnet_dir(*, clean: bool) -> None:
    if clean and LOCALNET_DIR.exists():
        print(f"Cleaning {LOCALNET_DIR}")
        shutil.rmtree(LOCALNET_DIR)
    if LOCALNET_DIR.exists():
        fail(f"{LOCALNET_DIR} already exists. Use --clean to overwrite.")


def apply_port_offset(port_offset: int) -> None:
    global BASE_P2P_PORT, BASE_RPC_PORT, BASE_METRICS_PORT, BASE_XIAN_METRICS_PORT
    BASE_P2P_PORT = 26656 + port_offset
    BASE_RPC_PORT = 26657 + port_offset
    BASE_METRICS_PORT = 26660 + port_offset
    BASE_XIAN_METRICS_PORT = 9108 + port_offset


def logging_settings() -> dict:
    """write_node_config keyword arguments, in summary order."""
    return {
        "transaction_trace_logging": env_bool("XIAN_LOCALNET_TRANSACTION_TRACE_LOGGING", False),
        "app_log_level": env_str("XIAN_LOCALNET_APP_LOG_LEVEL", "INFO"),
        "app_log_json": env_bool("XIAN_LOCALNET_APP_LOG_JSON", False),
        "app_log_rotation_hours": env_int("XIAN_LOCALNET_APP_LOG_ROTATION_HOURS", 1),
        "app_log_retention_days": env_int("XIAN_LOCALNET_APP_LOG_RETENTION_DAYS", 7),
        "app_log_max_file_mb": env_int("XIAN_LOCALNET_APP_LOG_MAX_FILE_MB", 50),
        "app_log_max_total_mb": env_int("XIAN_LOCALNET_APP_LOG_MAX_TOTAL_MB", 250),
    }


def localnet_profile_name() -> str:
    localnet_profile = env_str("XIAN_LOCALNET_PROFILE", "default")
    if localnet_profile not in PROFILE_CONSENSUS_DEFAULTS:
        fail("XIAN_LOCALNET_PROFILE must be one of: default, throughput")
    return localnet_profile


def consensus_settings(localnet_profile: str) -> dict:
    """write_node_config keyword arguments; the profile fills settings left unset."""
    settings = {
        "block_policy_mode": env_optional_str("XIAN_LOCALNET_BLOCK_POLICY_MODE"),
        "block_policy_interval": env_optional_str("XIAN_LOCALNET_BLOCK_POLICY_INTERVAL"),
        "consensus_timeout_propose": env_optional_str("XIAN_LOCALNET_CONSENSUS_TIMEOUT_PROPOSE"),
        "consensus_timeout_propose_delta": env_optional_str(
            "XIAN_LOCALNET_CONSENSUS_TIMEOUT_PROPOSE_DELTA"
        ),
        "consensus_timeout_prevote": env_optional_str("XIAN_LOCALNET_CONSENSUS_TIMEOUT_PREVOTE"),
        "consensus_timeout_prevote_delta": env_optional_str(
            "XIAN_LOCALNET_CONSENSUS_TIMEOUT_PREVOTE_DELTA"
        ),
        "consensus_timeout_precommit": env_optional_str(
            "XIAN_LOCALNET_CONSENSUS_TIMEOUT_PRECOMMIT"
        ),
        "consensus_timeout_precommit_delta": env_optional_str(
            "XIAN_LOCALNET_CONSENSUS_TIMEOUT_PRECOMMIT_DELTA"
        ),
        "consensus_timeout_commit": env_optional_str("XIAN_LOCALNET_CONSENSUS_TIMEOUT_COMMIT"),
        "consensus_skip_timeout_commit": env_optional_bool("XIAN_LOCALNET_SKIP_TIMEOUT_COMMIT"),
        "mempool_size": env_optional_int("XIAN_LOCALNET_MEMPOOL_SIZE"),
        "mempool_cache_size": env_optional_int("XIAN_LOCALNET_MEMPOOL_CACHE_SIZE"),
    }
    for key, default in PROFILE_CONSENSUS_DEFAULTS[localnet_profile].items():
        if settings[key] is None:
            settings[key] = default
    return settings


def strip_prefix(settings: dict, prefix: str) -> dict:
    return {key.removeprefix(prefix): value for key, value in settings.items()}


def node_summary(node: dict, *, topology: str, bds_enabled: bool) -> dict:
    index = node["index"]
    return {
        "moniker": node["moniker"],
        "node_id": node["node_key"]["node_id"],
        "host_rpc_port": BASE_RPC_PORT + index * PORT_STRIDE,
        "host_p2p_port": BASE_P2P_PORT + index * PORT_STRIDE,
        "host_metrics_port": BASE_METRICS_PORT + index * PORT_STRIDE,
        "host_xian_metrics_port": (BASE_XIAN_METRICS_PORT + index * PORT_STRIDE),
        "abci_container": (
            f"xian-{node['moniker']}"
            if topology == "integrated"
            else f"xian-{node['moniker']}-abci"
        ),
        "cometbft_container": f"xian-{node['moniker']}",
        "account_public_key": node["account_public_key"],
        "account_private_key": node["validator_material"]["validator_private_key_hex"],
        "bds_enabled": bds_enabled,
    }


def bds_summary(*, bds_enabled: bool, bds_node_index: int) -> dict:
    return {
        "enabled": bds_enabled,
        "bds_node_index": bds_node_index if bds_enabled else None,
        "bds_rpc_url": (
            f"http://127.0.0.1:{BASE_RPC_PORT + bds_node_index * PORT_STRIDE}"
            if bds_enabled
            else None
        ),
    }


def main():
    args = parse_args()
    # Chain fee policy is genesis state; empty settings mean paid_metered.
    # Validate it before --clean removes an existing localnet.
    fee_policy = genesis_fee_policy(os.environ)

    if args.nodes < 4:
        fail("Minimum 4 nodes required for BFT consensus")
    prepare_localnet_dir(clean=args.clean)

    print(
        "Generating "
        f"{args.nodes}-node localnet "
        f"(chain_id={args.chain_id}, genesis_network={args.genesis_network})"
    )
    port_offset = env_int("XIAN_LOCALNET_PORT_OFFSET", 0)
    apply_port_offset(port_offset)
    bds_enabled = env_bool("XIAN_LOCALNET_ENABLE_BDS", False)
    bds_node_index = env_int("XIAN_LOCALNET_BDS_NODE_INDEX", 0)
    if bds_enabled and not 0 <= bds_node_index < args.nodes:
        fail("XIAN_LOCALNET_BDS_NODE_INDEX must point to an existing node")
    log_settings = logging_settings()
    # Localnet matches the in-process dev client (zk on); real-network genesis
    # stays explicit opt-in via configure_node --runtime-feature-zk.
    runtime_feature_zk = env_bool("XIAN_LOCALNET_RUNTIME_FEATURE_ZK", True)
    profiling_enabled = env_bool("XIAN_PERF_ENABLED", False)
    profiling_recent_blocks = env_int("XIAN_PERF_RECENT_BLOCKS", 32)
    localnet_profile = localnet_profile_name()
    consensus = consensus_settings(localnet_profile)

    # 1. Generate key material for all nodes
    nodes = [generate_node_material(i) for i in range(args.nodes)]

    # 2. Build genesis using the first node's key as founder
    print(f"Building genesis block (submitting {args.genesis_network} system contracts)...")
    genesis = build_localnet_genesis(
        nodes,
        chain_id=args.chain_id,
        genesis_network=args.genesis_network,
        runtime_feature_zk=runtime_feature_zk,
        fee_policy=fee_policy,
    )
    print(f"  Genesis has {len(genesis.get('validators', []))} validators")

    # 3. Write per-node config
    for node in nodes:
        write_node_config(
            node,
            nodes,
            args.chain_id,
            genesis,
            **consensus,
            bds_enabled=bds_enabled and node["index"] == bds_node_index,
            topology=args.topology,
            **log_settings,
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
    consensus_summary = {
        key: value for key, value in consensus.items() if not key.startswith("mempool_")
    }
    mempool_summary = {key: value for key, value in consensus.items() if key.startswith("mempool_")}
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
            node_summary(
                n,
                topology=args.topology,
                bds_enabled=bds_enabled and n["index"] == bds_node_index,
            )
            for n in nodes
        ],
        "founder_key": nodes[0]["validator_material"]["validator_private_key_hex"],
        "profiling": {
            "enabled": profiling_enabled,
            "recent_blocks": profiling_recent_blocks,
        },
        "localnet_profile": localnet_profile,
        "consensus": strip_prefix(consensus_summary, "consensus_"),
        "mempool": strip_prefix(mempool_summary, "mempool_"),
        "port_offset": port_offset,
        "bds": bds_summary(bds_enabled=bds_enabled, bds_node_index=bds_node_index),
        "logging": log_settings,
    }
    (LOCALNET_DIR / "network.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\nLocalnet initialized in {LOCALNET_DIR}")
    print("Start with: make localnet-up")


def postgres_dependency() -> dict:
    return {LOCALNET_POSTGRES_SERVICE: {"condition": "service_healthy"}}


def node_host_ports(idx: int) -> tuple[int, int, int, int]:
    return (
        BASE_P2P_PORT + idx * PORT_STRIDE,
        BASE_RPC_PORT + idx * PORT_STRIDE,
        BASE_METRICS_PORT + idx * PORT_STRIDE,
        BASE_XIAN_METRICS_PORT + idx * PORT_STRIDE,
    )


def integrated_node_services(
    node: dict, *, integrated_service_base: dict, depends_on_postgres: bool
) -> dict:
    """One container runs CometBFT and the ABCI app for this node."""
    idx = node["index"]
    moniker = node["moniker"]
    host_p2p, host_rpc, host_metrics, host_xian_metrics = node_host_ports(idx)
    service = {
        **integrated_service_base,
        "hostname": moniker,
        "container_name": f"xian-{moniker}",
        "volumes": [
            f"./.localnet/{moniker}/.cometbft:/root/.cometbft",
        ],
        "environment": {
            **integrated_service_base["environment"],
        },
        "ports": [
            f"{host_p2p}:26656",
            f"{host_rpc}:26657",
            f"{host_metrics}:26660",
            f"{host_xian_metrics}:9108",
        ],
    }
    if depends_on_postgres:
        service["depends_on"] = postgres_dependency()
    return {moniker: service}


def split_node_services(
    node: dict,
    *,
    split_build: dict,
    profiling_enabled: bool,
    profiling_recent_blocks: int,
    depends_on_postgres: bool,
) -> dict:
    """Separate ABCI and CometBFT containers for this node (fidelity topology)."""
    idx = node["index"]
    moniker = node["moniker"]
    host_p2p, host_rpc, host_metrics, host_xian_metrics = node_host_ports(idx)
    shared_tmp = f"./.localnet/{moniker}/tmp:/tmp"
    home_mount = f"./.localnet/{moniker}/.cometbft:/root/.cometbft"
    abci_service = {
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
    if depends_on_postgres:
        abci_service["depends_on"] = postgres_dependency()
    cometbft_service = {
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
    return {f"{moniker}-abci": abci_service, moniker: cometbft_service}


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
            "image": "postgres:17.10",
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
                    "psql -U xian -d xian -v ON_ERROR_STOP=1 -Atqc 'SELECT 1' >/dev/null",
                ],
                "interval": "5s",
                "timeout": "5s",
                "retries": 12,
                "start_period": "30s",
            },
            "volumes": [
                "localnet-postgres-data:/var/lib/postgresql/data",
            ],
            "expose": ["5432"],
            "networks": ["localnet"],
        }
    for node in nodes:
        depends_on_postgres = bds_enabled and node["index"] == bds_node_index
        if topology == "integrated":
            services.update(
                integrated_node_services(
                    node,
                    integrated_service_base=integrated_service_base,
                    depends_on_postgres=depends_on_postgres,
                )
            )
        else:
            services.update(
                split_node_services(
                    node,
                    split_build=split_build,
                    profiling_enabled=profiling_enabled,
                    profiling_recent_blocks=profiling_recent_blocks,
                    depends_on_postgres=depends_on_postgres,
                )
            )

    compose = {
        # A separate project keeps single-node `down --remove-orphans` from
        # treating localnet validators as orphans of the stack project.
        "name": LOCALNET_COMPOSE_PROJECT,
        "networks": {
            "localnet": {
                "driver": "bridge",
            },
        },
    }
    for service in services.values():
        service["logging"] = {
            "driver": "local",
            "options": {
                "max-size": "${XIAN_DOCKER_LOG_MAX_FILE_MB:-50}m",
                "max-file": "${XIAN_DOCKER_LOG_MAX_FILES:-5}",
                "mode": "blocking",
            },
        }
    compose["services"] = services
    if bds_enabled:
        # Native Docker storage avoids host-filesystem ownership changes during
        # initdb. A fresh generation must never reuse another chain's BDS data.
        compose["volumes"] = {
            "localnet-postgres-data": {
                "name": f"xian-localnet-postgres-{secrets.token_hex(8)}",
            },
        }

    compose_path = STACK_DIR / "docker-compose-localnet.yml"
    compose_path.write_text(json.dumps(compose, indent=2) + "\n", encoding="utf-8")

    print(f"  Wrote {compose_path}")


if __name__ == "__main__":
    main()
