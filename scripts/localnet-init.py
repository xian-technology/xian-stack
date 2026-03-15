#!/usr/bin/env python3
"""Generate keys, genesis, and CometBFT config for an N-node local network.

Usage:
    python localnet-init.py --nodes 4 --chain-id xian-localnet-1

Outputs everything under .localnet/node-{i}/.cometbft/ ready to be
mounted into Docker containers.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure xian-abci and xian-contracting are importable
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = STACK_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT / "xian-abci" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "xian-contracting" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "xian-py" / "src"))

from xian.genesis_builder import (  # noqa: E402
    build_local_network_genesis,
    write_genesis_block,
)
from xian.node_setup import (  # noqa: E402
    build_node_key,
    build_priv_validator_key,
    render_cometbft_config,
)
from xian import toml_utils  # noqa: E402

LOCALNET_DIR = STACK_DIR / ".localnet"
CONFIGS_DIR = PROJECT_ROOT / "xian-configs"

# Port offsets from base for each node
BASE_P2P_PORT = 26656
BASE_RPC_PORT = 26657
BASE_METRICS_PORT = 26660
PORT_STRIDE = 100  # node-0: 266xx, node-1: 267xx, node-2: 268xx, ...


def generate_node_material(index: int) -> dict:
    """Generate validator key, node key, and metadata for one node."""
    val_seed = secrets.token_bytes(32)
    node_seed = secrets.token_bytes(32)

    val_key = build_priv_validator_key(val_seed.hex())
    node_key = build_node_key(node_seed.hex())

    return {
        "index": index,
        "moniker": f"node-{index}",
        "validator_key": val_key,
        "node_key": node_key,
        "account_public_key": val_key["pub_key"]["value"],
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
):
    """Write all CometBFT config files for a single node."""
    home = LOCALNET_DIR / node["moniker"] / ".cometbft"
    config_dir = home / "config"
    data_dir = home / "data"
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # --- priv_validator_key.json ---
    pvk = dict(node["validator_key"])
    pvk.pop("_private_key_hex", None)
    with open(config_dir / "priv_validator_key.json", "w") as f:
        json.dump(pvk, f, indent=2)

    # --- priv_validator_state.json (empty initial state) ---
    with open(data_dir / "priv_validator_state.json", "w") as f:
        json.dump({"height": "0", "round": 0, "step": 0}, f, indent=2)

    # --- node_key.json ---
    nk = {"priv_key": node["node_key"]["priv_key"]}
    with open(config_dir / "node_key.json", "w") as f:
        json.dump(nk, f, indent=2)

    # --- genesis.json ---
    write_genesis_block(config_dir / "genesis.json", genesis)

    # --- config.toml ---
    # Exclude self from persistent_peers
    other_nodes = [n for n in all_nodes if n["index"] != node["index"]]
    peers = build_persistent_peers(other_nodes)

    config = render_cometbft_config(
        moniker=node["moniker"],
        seed_nodes=[],
        allow_cors=True,
        prometheus=True,
    )
    # Override peers and listen addresses (inside container, always same ports)
    config["p2p"]["persistent_peers"] = peers
    config["p2p"]["laddr"] = "tcp://0.0.0.0:26656"
    config["p2p"]["addr_book_strict"] = False
    config["p2p"]["allow_duplicate_ip"] = True
    config["rpc"]["laddr"] = "tcp://0.0.0.0:26657"
    config["consensus"]["create_empty_blocks"] = True
    config["consensus"]["create_empty_blocks_interval"] = "5s"

    config_path = config_dir / "config.toml"
    with open(config_path, "w") as f:
        f.write(toml_utils.dumps(config))


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

    # 1. Generate key material for all nodes
    nodes = [generate_node_material(i) for i in range(args.nodes)]

    # 2. Build genesis using the first node's key as founder
    founder_key = nodes[0]["validator_key"]["_private_key_hex"]
    validators = [
        {
            "account_public_key": n["account_public_key"],
            "name": n["moniker"],
            "power": 10,
            "priv_validator_key": n["validator_key"],
        }
        for n in nodes
    ]

    print("Building genesis block (submitting system contracts)...")
    genesis = build_local_network_genesis(
        chain_id=args.chain_id,
        founder_private_key=founder_key,
        validators=validators,
        network="local",
        contracts_dir=CONFIGS_DIR / "legacy" / "genesis" / "contracts",
    )
    print(f"  Genesis has {len(genesis.get('validators', []))} validators")

    # 3. Write per-node config
    for node in nodes:
        write_node_config(node, nodes, args.chain_id, genesis)
        idx = node["index"]
        host_p2p = BASE_P2P_PORT + idx * PORT_STRIDE
        host_rpc = BASE_RPC_PORT + idx * PORT_STRIDE
        print(f"  {node['moniker']}: RPC=:{host_rpc} P2P=:{host_p2p} id={node['node_key']['node_id'][:12]}...")

    # 4. Write docker-compose-localnet.yml
    write_compose_file(nodes)

    # 5. Write node summary for scripts
    summary = {
        "chain_id": args.chain_id,
        "nodes": [
            {
                "moniker": n["moniker"],
                "node_id": n["node_key"]["node_id"],
                "host_rpc_port": BASE_RPC_PORT + n["index"] * PORT_STRIDE,
                "host_p2p_port": BASE_P2P_PORT + n["index"] * PORT_STRIDE,
                "host_metrics_port": BASE_METRICS_PORT + n["index"] * PORT_STRIDE,
            }
            for n in nodes
        ],
        "founder_key": founder_key,
    }
    with open(LOCALNET_DIR / "network.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nLocalnet initialized in {LOCALNET_DIR}")
    print(f"Start with: make localnet-up")


def write_compose_file(nodes: list[dict]):
    """Generate docker-compose-localnet.yml from node list."""
    services = {}
    for node in nodes:
        idx = node["index"]
        moniker = node["moniker"]
        host_p2p = BASE_P2P_PORT + idx * PORT_STRIDE
        host_rpc = BASE_RPC_PORT + idx * PORT_STRIDE
        host_metrics = BASE_METRICS_PORT + idx * PORT_STRIDE

        services[moniker] = {
            "build": {
                "context": ".",
                "dockerfile": "./docker/localnet.Dockerfile",
            },
            "hostname": moniker,
            "container_name": f"xian-{moniker}",
            "volumes": [
                "${XIAN_ABCI_DIR}:/usr/src/app/xian-abci",
                "${XIAN_CONFIGS_DIR}:/usr/src/app/xian-configs",
                "${XIAN_CONTRACTING_DIR}:/usr/src/app/xian-contracting",
                "${XIAN_PY_DIR}:/usr/src/app/xian-py",
                f"./.localnet/{moniker}/.cometbft:/root/.cometbft",
            ],
            "environment": {
                "XIAN_CONFIGS_DIR": "/usr/src/app/xian-configs",
                "NODE_INDEX": str(idx),
            },
            "ports": [
                f"{host_p2p}:26656",
                f"{host_rpc}:26657",
                f"{host_metrics}:26660",
            ],
            "networks": ["localnet"],
            "command": (
                'bash -lc "'
                "pip install --quiet ./xian-py ./xian-contracting ./xian-abci && "
                "cd /usr/src/app/xian-abci && "
                "cd ./src/xian && pm2 start xian_abci.py --name xian -f && "
                "pm2 start \\\"cometbft node --rpc.laddr tcp://0.0.0.0:26657\\\" --name cometbft -f && "
                'pm2 logs --raw"'
            ),
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

    # Networks
    lines.append("networks:")
    for name, cfg in compose["networks"].items():
        lines.append(f"  {name}:")
        for k, v in cfg.items():
            lines.append(f"    {k}: {_yaml_val(v)}")

    lines.append("")
    lines.append("services:")
    for svc_name, svc in compose["services"].items():
        lines.append(f"  {svc_name}:")
        for key, val in svc.items():
            if key == "build":
                lines.append(f"    build:")
                for bk, bv in val.items():
                    lines.append(f"      {bk}: {_yaml_val(bv)}")
            elif key == "volumes" or key == "ports" or key == "networks":
                lines.append(f"    {key}:")
                for item in val:
                    lines.append(f"      - {_yaml_val(item)}")
            elif key == "environment":
                lines.append(f"    environment:")
                for ek, ev in val.items():
                    lines.append(f"      {ek}: {_yaml_val(ev)}")
            elif key == "command":
                lines.append(f"    command: >")
                lines.append(f"      {val}")
            else:
                lines.append(f"    {key}: {_yaml_val(val)}")
        lines.append("")

    return "\n".join(lines) + "\n"


def _yaml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        if any(c in v for c in ":{},[]&*#?|-<>=!%@\\"):
            return f'"{v}"'
        return v
    return str(v)


if __name__ == "__main__":
    main()
