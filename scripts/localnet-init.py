#!/usr/bin/env python3
"""Generate keys, genesis, and CometBFT config for an N-node local network.

Usage:
    python localnet-init.py --nodes 4 --chain-id xian-localnet-1

Outputs everything under .localnet/node-{i}/.cometbft/ ready to be
mounted into Docker containers.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent

from xian.genesis_builder import (  # noqa: E402
    build_local_network_genesis,
)
from xian.node_setup import (  # noqa: E402
    build_node_key,
    generate_validator_material,
    materialize_cometbft_home,
    render_cometbft_config,
)

LOCALNET_DIR = STACK_DIR / ".localnet"
CONFIGS_DIR = STACK_DIR.parent / "xian-configs"

# Port offsets from base for each node
BASE_P2P_PORT = 26656
BASE_RPC_PORT = 26657
BASE_METRICS_PORT = 26660
PORT_STRIDE = 100  # node-0: 266xx, node-1: 267xx, node-2: 268xx, ...
NODE_IMAGE_INTEGRATED = "xian-node-integrated:local"
NODE_IMAGE_SPLIT = "xian-node-split:local"


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


def node_build_config(target: str) -> dict:
    return {
        "context": ".",
        "dockerfile": "./docker/xian-node.Dockerfile",
        "target": target,
        "additional_contexts": {
            "xian-abci": "${XIAN_ABCI_DIR}",
            "xian-configs": "${XIAN_CONFIGS_DIR}",
            "xian-contracting": "${XIAN_CONTRACTING_DIR}",
            "xian-py": "${XIAN_PY_DIR}",
        },
        "args": {
            "COMETBFT_VERSION": "${XIAN_COMETBFT_VERSION}",
            "S6_OVERLAY_VERSION": "${XIAN_S6_OVERLAY_VERSION}",
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


def write_node_config(
    node: dict,
    all_nodes: list[dict],
    chain_id: str,
    genesis: dict,
    *,
    parallel_execution_enabled: bool,
    parallel_execution_workers: int,
    parallel_execution_min_transactions: int,
):
    """Write all CometBFT config files for a single node."""
    home = LOCALNET_DIR / node["moniker"] / ".cometbft"

    # Exclude self from persistent_peers
    other_nodes = [n for n in all_nodes if n["index"] != node["index"]]
    peers = build_persistent_peers(other_nodes)

    config = render_cometbft_config(
        moniker=node["moniker"],
        seed_nodes=[],
        allow_cors=True,
        prometheus=True,
        parallel_execution_enabled=parallel_execution_enabled,
        parallel_execution_workers=parallel_execution_workers,
        parallel_execution_min_transactions=(
            parallel_execution_min_transactions
        ),
    )
    # Override peers and listen addresses (inside container, always same ports)
    config["p2p"]["persistent_peers"] = peers
    config["p2p"]["laddr"] = "tcp://0.0.0.0:26656"
    config["p2p"]["addr_book_strict"] = False
    config["p2p"]["allow_duplicate_ip"] = True
    config["rpc"]["laddr"] = "tcp://0.0.0.0:26657"
    config["consensus"]["create_empty_blocks"] = True
    config["consensus"]["create_empty_blocks_interval"] = "5s"

    materialize_cometbft_home(
        home=home,
        config=config,
        genesis=genesis,
        priv_validator_key=node["validator_material"]["priv_validator_key"],
        node_key=node["node_key"],
    )


def main():
    parser = argparse.ArgumentParser(description="Initialize a local N-node network")
    parser.add_argument(
        "--nodes", "-n", type=int, default=4,
        help="Number of validator nodes (minimum 4)",
    )
    parser.add_argument(
        "--chain-id", default="xian-localnet-1",
        help="Chain ID for the network",
    )
    parser.add_argument(
        "--clean", action="store_true",
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

    print(f"Generating {args.nodes}-node localnet (chain_id={args.chain_id})")
    parallel_execution_enabled = env_bool(
        "XIAN_LOCALNET_PARALLEL_EXECUTION_ENABLED", False
    )
    parallel_execution_workers = env_int(
        "XIAN_LOCALNET_PARALLEL_EXECUTION_WORKERS", 0
    )
    parallel_execution_min_transactions = env_int(
        "XIAN_LOCALNET_PARALLEL_EXECUTION_MIN_TRANSACTIONS", 8
    )

    # 1. Generate key material for all nodes
    nodes = [generate_node_material(i) for i in range(args.nodes)]

    # 2. Build genesis using the first node's key as founder
    founder_key = nodes[0]["validator_material"]["validator_private_key_hex"]
    validators = [
        {
            "account_public_key": n["account_public_key"],
            "name": n["moniker"],
            "power": 10,
            "priv_validator_key": n["validator_material"][
                "priv_validator_key"
            ],
        }
        for n in nodes
    ]

    print("Building genesis block (submitting system contracts)...")
    genesis = build_local_network_genesis(
        chain_id=args.chain_id,
        founder_private_key=founder_key,
        validators=validators,
        network="local",
        contracts_dir=CONFIGS_DIR / "contracts",
    )
    print(f"  Genesis has {len(genesis.get('validators', []))} validators")

    # 3. Write per-node config
    for node in nodes:
        write_node_config(
            node,
            nodes,
            args.chain_id,
            genesis,
            parallel_execution_enabled=parallel_execution_enabled,
            parallel_execution_workers=parallel_execution_workers,
            parallel_execution_min_transactions=(
                parallel_execution_min_transactions
            ),
        )
        (LOCALNET_DIR / node["moniker"] / "tmp").mkdir(parents=True, exist_ok=True)
        idx = node["index"]
        host_p2p = BASE_P2P_PORT + idx * PORT_STRIDE
        host_rpc = BASE_RPC_PORT + idx * PORT_STRIDE
        print(f"  {node['moniker']}: RPC=:{host_rpc} P2P=:{host_p2p} id={node['node_key']['node_id'][:12]}...")

    # 4. Write docker-compose-localnet.yml
    write_compose_file(nodes, args.topology)

    # 5. Write node summary for scripts
    summary = {
        "chain_id": args.chain_id,
        "topology": args.topology,
        "nodes": [
            {
                "moniker": n["moniker"],
                "node_id": n["node_key"]["node_id"],
                "host_rpc_port": BASE_RPC_PORT + n["index"] * PORT_STRIDE,
                "host_p2p_port": BASE_P2P_PORT + n["index"] * PORT_STRIDE,
                "host_metrics_port": BASE_METRICS_PORT + n["index"] * PORT_STRIDE,
                "abci_container": (
                    f"xian-{n['moniker']}"
                    if args.topology == "integrated"
                    else f"xian-{n['moniker']}-abci"
                ),
                "cometbft_container": f"xian-{n['moniker']}",
            }
            for n in nodes
        ],
        "founder_key": founder_key,
        "parallel_execution": {
            "enabled": parallel_execution_enabled,
            "workers": parallel_execution_workers,
            "min_transactions": parallel_execution_min_transactions,
        },
    }
    (LOCALNET_DIR / "network.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\nLocalnet initialized in {LOCALNET_DIR}")
    print(f"Start with: make localnet-up")


def write_compose_file(nodes: list[dict], topology: str):
    """Generate docker-compose-localnet.yml from node list."""
    services = {}
    integrated_build = node_build_config("integrated")
    split_build = node_build_config("split")
    for node in nodes:
        idx = node["index"]
        moniker = node["moniker"]
        host_p2p = BASE_P2P_PORT + idx * PORT_STRIDE
        host_rpc = BASE_RPC_PORT + idx * PORT_STRIDE
        host_metrics = BASE_METRICS_PORT + idx * PORT_STRIDE
        if topology == "integrated":
            services[moniker] = {
                "image": NODE_IMAGE_INTEGRATED,
                "restart": "always",
                "stop_grace_period": "45s",
                "hostname": moniker,
                "container_name": f"xian-{moniker}",
                "build": integrated_build,
                "mem_limit": "${XIAN_LOCALNET_NODE_MEMORY_LIMIT}",
                "mem_reservation": "${XIAN_LOCALNET_NODE_MEMORY_RESERVATION}",
                "memswap_limit": "${XIAN_LOCALNET_NODE_MEMORY_SWAP}",
                "pids_limit": "${XIAN_LOCALNET_NODE_PIDS_LIMIT}",
                "ulimits": {
                    "nofile": {
                        "soft": "${XIAN_LOCALNET_NODE_NOFILE_SOFT}",
                        "hard": "${XIAN_LOCALNET_NODE_NOFILE_HARD}",
                    }
                },
                "volumes": [
                    f"./.localnet/{moniker}/.cometbft:/root/.cometbft",
                ],
                "environment": {
                    "XIAN_CONFIGS_DIR": "/opt/xian-configs",
                    "S6_VERBOSITY": "${XIAN_S6_VERBOSITY}",
                    "NODE_INDEX": str(idx),
                },
                "ports": [
                    f"{host_p2p}:26656",
                    f"{host_rpc}:26657",
                    f"{host_metrics}:26660",
                ],
                "networks": ["localnet"],
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
                "mem_limit": "${XIAN_LOCALNET_ABCI_MEMORY_LIMIT}",
                "mem_reservation": "${XIAN_LOCALNET_ABCI_MEMORY_RESERVATION}",
                "memswap_limit": "${XIAN_LOCALNET_ABCI_MEMORY_SWAP}",
                "pids_limit": "${XIAN_LOCALNET_ABCI_PIDS_LIMIT}",
                "ulimits": {
                    "nofile": {
                        "soft": "${XIAN_LOCALNET_ABCI_NOFILE_SOFT}",
                        "hard": "${XIAN_LOCALNET_ABCI_NOFILE_HARD}",
                    }
                },
                "volumes": [home_mount, shared_tmp],
                "environment": {
                    "XIAN_CONFIGS_DIR": "/opt/xian-configs",
                    "NODE_INDEX": str(idx),
                },
                "command": ["xian-abci"],
                "networks": ["localnet"],
            }
            services[moniker] = {
                "image": NODE_IMAGE_SPLIT,
                "restart": "always",
                "init": True,
                "stop_grace_period": "45s",
                "hostname": moniker,
                "container_name": f"xian-{moniker}",
                "build": split_build,
                "mem_limit": "${XIAN_LOCALNET_COMETBFT_MEMORY_LIMIT}",
                "mem_reservation": "${XIAN_LOCALNET_COMETBFT_MEMORY_RESERVATION}",
                "memswap_limit": "${XIAN_LOCALNET_COMETBFT_MEMORY_SWAP}",
                "pids_limit": "${XIAN_LOCALNET_COMETBFT_PIDS_LIMIT}",
                "ulimits": {
                    "nofile": {
                        "soft": "${XIAN_LOCALNET_COMETBFT_NOFILE_SOFT}",
                        "hard": "${XIAN_LOCALNET_COMETBFT_NOFILE_HARD}",
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
        "services": services,
    }

    compose_path = STACK_DIR / "docker-compose-localnet.yml"
    # Write as YAML manually (avoid PyYAML dependency)
    with open(compose_path, "w") as f:
        f.write(_compose_to_yaml(compose))

    print(f"  Wrote {compose_path}")


def _compose_to_yaml(compose: dict) -> str:
    """Minimal YAML serializer for docker-compose structure."""
    lines = []
    _yaml_write_mapping(lines, compose, indent=0)
    return "\n".join(lines) + "\n"


def _yaml_write_mapping(lines: list[str], payload: dict, *, indent: int) -> None:
    prefix = " " * indent
    for key, value in payload.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            _yaml_write_mapping(lines, value, indent=indent + 2)
        elif isinstance(value, list):
            lines.append(f"{prefix}{key}:")
            _yaml_write_sequence(lines, value, indent=indent + 2)
        else:
            lines.append(f"{prefix}{key}: {_yaml_val(value)}")


def _yaml_write_sequence(lines: list[str], payload: list, *, indent: int) -> None:
    prefix = " " * indent
    for item in payload:
        if isinstance(item, dict):
            lines.append(f"{prefix}-")
            _yaml_write_mapping(lines, item, indent=indent + 2)
        elif isinstance(item, list):
            lines.append(f"{prefix}-")
            _yaml_write_sequence(lines, item, indent=indent + 2)
        else:
            lines.append(f"{prefix}- {_yaml_val(item)}")


def _yaml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        if (
            not v
            or v.startswith("${")
            or any(c in v for c in ":{},[]&*#?|-<>=!%@\\")
            or v.lower() in {"true", "false", "null"}
        ):
            return f'"{v}"'
        return v
    return str(v)


if __name__ == "__main__":
    main()
