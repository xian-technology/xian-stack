#!/usr/bin/env python3
"""Burst-send transactions for 60 seconds to test tracer determinism.

Sends a mix of currency.transfer and contract submissions, then verifies
all nodes agree on app_hash at the same block height.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = STACK_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT / "xian-py" / "src"))

from xian_py.wallet import Wallet  # noqa: E402
from xian_py.xian import Xian  # noqa: E402


def load_network():
    with open(STACK_DIR / ".localnet" / "network.json") as f:
        return json.load(f)


COUNTER_CONTRACT = """
v = Variable()

@construct
def seed():
    v.set(0)

@export
def increment():
    v.set(v.get() + 1)
    return v.get()

@export
def add(amount: int):
    v.set(v.get() + amount)
    return v.get()

@export
def get():
    return v.get()
"""


def main():
    network = load_network()
    founder_key = network["founder_key"]
    nodes = network["nodes"]
    chain_id = network["chain_id"]

    rpc_url = f"http://127.0.0.1:{nodes[0]['host_rpc_port']}"

    wallet = Wallet(private_key=founder_key)
    print(f"Founder: {wallet.public_key[:16]}...")
    print(f"RPC: {rpc_url}")
    print(f"Chain: {chain_id}")

    xian = Xian(node_url=rpc_url, chain_id=chain_id, wallet=wallet)

    # Pre-flight: check balance
    balance = xian.get_balance(wallet.public_key)
    print(f"Balance: {balance}")

    # Submit a counter contract first
    print("\nSubmitting counter contract...")
    try:
        result = xian.submit_contract(
            name="con_counter",
            code=COUNTER_CONTRACT,
            stamps=500,
        )
        print(f"  Contract submitted: {result.get('hash', 'ok')[:16]}...")
    except Exception as e:
        print(f"  Contract submission error: {e}")

    time.sleep(3)

    # Burst phase: send mixed transactions
    duration = 60
    print(f"\nBursting transactions for {duration}s...")
    start = time.time()
    tx_count = 0
    errors = 0

    while time.time() - start < duration:
        try:
            if tx_count % 3 == 0:
                # Currency transfer
                recipient = secrets.token_hex(32)
                xian.send(amount=1, to_address=recipient, stamps=100)
            elif tx_count % 3 == 1:
                # Counter increment
                xian.send_tx(
                    contract="con_counter",
                    function="increment",
                    kwargs={},
                    stamps=100,
                )
            else:
                # Counter add
                xian.send_tx(
                    contract="con_counter",
                    function="add",
                    kwargs={"amount": tx_count},
                    stamps=100,
                )
            tx_count += 1
            if tx_count % 20 == 0:
                elapsed = time.time() - start
                print(f"  {tx_count} txs ({elapsed:.0f}s, {tx_count/elapsed:.1f} tx/s)")
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  ERROR #{errors}: {e}")
            time.sleep(0.2)

    elapsed = time.time() - start
    print(f"\nSent {tx_count} transactions in {elapsed:.1f}s ({tx_count/elapsed:.1f} tx/s)")
    print(f"Errors: {errors}")

    # Wait for blocks to finalize
    print("\nWaiting 15s for blocks to finalize...")
    time.sleep(15)

    # Check block heights across all nodes
    print("\nNode heights:")
    heights = {}
    for node in nodes:
        port = node["host_rpc_port"]
        try:
            with urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as resp:
                data = json.loads(resp.read())
                h = int(data["result"]["sync_info"]["latest_block_height"])
                heights[node["moniker"]] = h
                print(f"  {node['moniker']}: height={h}")
        except Exception as e:
            print(f"  {node['moniker']}: ERROR ({e})")

    # Determinism check: compare app_hash at multiple heights
    if heights:
        check_height = min(heights.values()) - 1
        print(f"\n=== DETERMINISM CHECK ===")
        print(f"Comparing app_hash at heights {max(1, check_height-2)} to {check_height}...\n")

        all_passed = True
        for h in range(max(1, check_height - 2), check_height + 1):
            hashes = {}
            for node in nodes:
                port = node["host_rpc_port"]
                try:
                    with urlopen(f"http://127.0.0.1:{port}/block?height={h}", timeout=5) as resp:
                        data = json.loads(resp.read())
                        app_hash = data["result"]["block"]["header"]["app_hash"]
                        hashes[node["moniker"]] = app_hash
                except Exception:
                    hashes[node["moniker"]] = "ERROR"

            unique = set(hashes.values()) - {"ERROR"}
            if len(unique) == 1:
                print(f"  height {h}: PASS (app_hash={list(unique)[0][:24]}...)")
            elif len(unique) > 1:
                print(f"  height {h}: FAIL - DIVERGED!")
                for name, ah in hashes.items():
                    print(f"    {name}: {ah}")
                all_passed = False
            else:
                print(f"  height {h}: SKIP (no data)")

        print(f"\n{'ALL HEIGHTS MATCH' if all_passed else 'CONSENSUS FAILURE DETECTED'}!")

    # Post-burst memory
    print("\nPost-burst memory:")
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
        capture_output=True, text=True,
    )
    for line in sorted(result.stdout.strip().split("\n")):
        if "xian-node" in line:
            print(f"  {line}")


if __name__ == "__main__":
    main()
