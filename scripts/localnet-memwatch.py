#!/usr/bin/env python3
"""Send transactions continuously while sampling memory every 30 seconds.

Runs for a configurable duration (default 10 minutes) and reports whether
memory is stable, growing, or leaking.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent

from xian_py.wallet import Wallet  # noqa: E402
from xian_py.xian import Xian  # noqa: E402


def load_network():
    with open(STACK_DIR / ".localnet" / "network.json", encoding="utf-8") as f:
        return json.load(f)


def sample_memory(network: dict) -> dict[str, float]:
    """Return memory in MiB per logical node."""
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
        capture_output=True, text=True,
    )
    container_mem = {}
    for line in result.stdout.strip().split("\n"):
        if not line or "xian-node" not in line:
            continue
        parts = line.split("\t")
        name = parts[0]
        usage_str = parts[1].split("/")[0].strip()
        # Parse "289.1MiB" or "1.2GiB"
        match = re.match(r"([\d.]+)\s*(MiB|GiB|KiB)", usage_str)
        if match:
            val = float(match.group(1))
            unit = match.group(2)
            if unit == "GiB":
                val *= 1024
            elif unit == "KiB":
                val /= 1024
            container_mem[name] = val

    node_mem = {}
    for node in network["nodes"]:
        containers = {node["cometbft_container"], node["abci_container"]}
        node_mem[node["moniker"]] = sum(
            container_mem.get(container, 0.0) for container in containers
        )
    return node_mem


def main():
    duration_minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    sample_interval = 30  # seconds

    network = load_network()
    founder_key = network["founder_key"]
    nodes = network["nodes"]
    chain_id = network["chain_id"]
    rpc_url = f"http://127.0.0.1:{nodes[0]['host_rpc_port']}"

    wallet = Wallet(private_key=founder_key)
    xian = Xian(node_url=rpc_url, chain_id=chain_id, wallet=wallet)

    duration = duration_minutes * 60
    print(f"Memory watch: {duration_minutes} min, sampling every {sample_interval}s")
    print(f"Sending mixed transactions continuously...\n")

    # Column headers
    header = f"{'Time':>6}  {'TXs':>6}  {'tx/s':>6}"
    for n in sorted(sample_memory(network).keys()):
        header += f"  {n:>10}"
    print(header)
    print("-" * len(header))

    samples: list[dict[str, float]] = []
    start = time.time()
    tx_count = 0
    errors = 0
    last_sample = 0

    while time.time() - start < duration:
        elapsed = time.time() - start

        # Sample memory every interval
        if elapsed - last_sample >= sample_interval or last_sample == 0:
            mem = sample_memory(network)
            samples.append(mem)
            last_sample = elapsed
            rate = tx_count / elapsed if elapsed > 0 else 0
            line = f"{elapsed:5.0f}s  {tx_count:6d}  {rate:5.1f}"
            for n in sorted(mem.keys()):
                line += f"  {mem[n]:8.1f}MB"
            print(line, flush=True)

        # Send transactions
        try:
            r = secrets.randbelow(3)
            if r == 0:
                xian.send(amount=1, to_address=secrets.token_hex(32), stamps=100)
            elif r == 1:
                xian.send_tx(contract="con_counter", function="increment", kwargs={}, stamps=100)
            else:
                xian.send_tx(contract="con_counter", function="add", kwargs={"amount": tx_count % 100}, stamps=100)
            tx_count += 1
        except Exception:
            errors += 1
            time.sleep(0.1)

    # Final sample
    mem = sample_memory(network)
    samples.append(mem)
    elapsed = time.time() - start
    rate = tx_count / elapsed if elapsed > 0 else 0
    line = f"{elapsed:5.0f}s  {tx_count:6d}  {rate:5.1f}"
    for n in sorted(mem.keys()):
        line += f"  {mem[n]:8.1f}MB"
    print(line, flush=True)

    print(f"\nTotal: {tx_count} txs in {elapsed:.0f}s ({rate:.1f} tx/s), {errors} errors")

    # Analyze trends
    print("\n=== MEMORY TREND ANALYSIS ===\n")
    node_names = sorted(samples[0].keys())
    for name in node_names:
        values = [s[name] for s in samples if name in s]
        if len(values) < 3:
            print(f"{name}: insufficient samples")
            continue

        first = values[0]
        last = values[-1]
        peak = max(values)
        delta = last - first
        # Linear regression slope (MiB per minute)
        n = len(values)
        xs = [i * sample_interval / 60 for i in range(n)]
        x_mean = sum(xs) / n
        y_mean = sum(values) / n
        slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values)) / max(sum((x - x_mean) ** 2 for x in xs), 1e-9)

        if abs(slope) < 0.5:
            verdict = "STABLE"
        elif slope > 2:
            verdict = "GROWING (possible leak)"
        elif slope > 0.5:
            verdict = "SLIGHT GROWTH"
        else:
            verdict = "DECREASING"

        print(f"  {name}:")
        print(f"    Start: {first:.1f} MB  End: {last:.1f} MB  Peak: {peak:.1f} MB")
        print(f"    Delta: {delta:+.1f} MB  Slope: {slope:+.2f} MB/min  -> {verdict}")


if __name__ == "__main__":
    main()
