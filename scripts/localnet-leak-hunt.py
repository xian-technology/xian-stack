#!/usr/bin/env python3
"""Track per-process memory (Python ABCI vs CometBFT) while sending
transactions.  Identifies which process is leaking.
"""

from __future__ import annotations

import json
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


def get_process_memory(container: str) -> dict[str, float]:
    """Get RSS in MiB for xian (Python) and cometbft (Go) inside a container."""
    cmd = f"""
XIAN_PID=$(pm2 pid xian 2>/dev/null)
COMET_PID=$(pm2 pid cometbft 2>/dev/null)
if [ -n "$XIAN_PID" ] && [ -f /proc/$XIAN_PID/status ]; then
    XIAN_RSS=$(awk '/VmRSS/{{print $2}}' /proc/$XIAN_PID/status)
else
    XIAN_RSS=0
fi
if [ -n "$COMET_PID" ] && [ -f /proc/$COMET_PID/status ]; then
    COMET_RSS=$(awk '/VmRSS/{{print $2}}' /proc/$COMET_PID/status)
else
    COMET_RSS=0
fi
echo "$XIAN_RSS $COMET_RSS"
"""
    result = subprocess.run(
        ["docker", "exec", container, "bash", "-c", cmd],
        capture_output=True, text=True,
    )
    parts = result.stdout.strip().split()
    if len(parts) == 2:
        return {
            "python": int(parts[0]) / 1024,  # KB -> MiB
            "cometbft": int(parts[1]) / 1024,
        }
    return {"python": 0, "cometbft": 0}


def get_python_internals(container: str) -> dict:
    """Get Python-level memory diagnostics via a one-liner inside the container."""
    # Use pm2 to send a signal or just read /proc maps for heap size
    cmd = """
XIAN_PID=$(pm2 pid xian 2>/dev/null)
if [ -n "$XIAN_PID" ] && [ -f /proc/$XIAN_PID/smaps_rollup ]; then
    awk '/Rss/ {print $1, $2}' /proc/$XIAN_PID/smaps_rollup 2>/dev/null | head -1
fi
"""
    result = subprocess.run(
        ["docker", "exec", container, "bash", "-c", cmd],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def main():
    duration_minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    sample_interval = 15  # seconds — more granular than the memwatch

    network = load_network()
    founder_key = network["founder_key"]
    nodes = network["nodes"]
    chain_id = network["chain_id"]
    rpc_url = f"http://127.0.0.1:{nodes[0]['host_rpc_port']}"

    wallet = Wallet(private_key=founder_key)
    xian = Xian(node_url=rpc_url, chain_id=chain_id, wallet=wallet)

    # Track all 4 nodes
    containers = [f"xian-node-{i}" for i in range(len(nodes))]

    duration = duration_minutes * 60
    print(f"Leak hunt: {duration_minutes} min, sampling every {sample_interval}s")
    print(f"Tracking per-process RSS (Python ABCI vs CometBFT Go)\n")

    # Header
    hdr = f"{'Time':>5} {'TXs':>6}"
    for c in containers:
        short = c.replace("xian-", "")
        hdr += f"  {short+'-py':>10} {short+'-cb':>10}"
    print(hdr)
    print("-" * len(hdr))

    # Collect samples: list of {container: {python: float, cometbft: float}}
    all_samples: list[dict] = []
    start = time.time()
    tx_count = 0
    errors = 0
    last_sample = -sample_interval  # force immediate first sample

    while time.time() - start < duration:
        elapsed = time.time() - start

        if elapsed - last_sample >= sample_interval:
            sample = {}
            for c in containers:
                sample[c] = get_process_memory(c)
            all_samples.append({"time": elapsed, "tx": tx_count, "mem": sample})
            last_sample = elapsed

            line = f"{elapsed:4.0f}s {tx_count:6d}"
            for c in containers:
                line += f"  {sample[c]['python']:8.1f}MB {sample[c]['cometbft']:8.1f}MB"
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
    sample = {}
    for c in containers:
        sample[c] = get_process_memory(c)
    elapsed = time.time() - start
    all_samples.append({"time": elapsed, "tx": tx_count, "mem": sample})
    line = f"{elapsed:4.0f}s {tx_count:6d}"
    for c in containers:
        line += f"  {sample[c]['python']:8.1f}MB {sample[c]['cometbft']:8.1f}MB"
    print(line, flush=True)

    print(f"\nTotal: {tx_count} txs in {elapsed:.0f}s, {errors} errors\n")

    # Analyze per-process trends
    print("=== PER-PROCESS MEMORY TREND ===\n")
    for c in containers:
        short = c.replace("xian-", "")
        for proc in ["python", "cometbft"]:
            values = [s["mem"][c][proc] for s in all_samples]
            if len(values) < 3:
                continue
            first, last, peak = values[0], values[-1], max(values)
            delta = last - first
            n = len(values)
            xs = [s["time"] / 60 for s in all_samples]
            x_mean = sum(xs) / n
            y_mean = sum(values) / n
            slope = (
                sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values))
                / max(sum((x - x_mean) ** 2 for x in xs), 1e-9)
            )

            if abs(slope) < 0.3:
                verdict = "STABLE"
            elif slope > 2:
                verdict = "LEAK"
            elif slope > 0.5:
                verdict = "GROWING"
            else:
                verdict = "OK"

            label = f"{short}/{proc}"
            print(
                f"  {label:<20} "
                f"start={first:6.1f}  end={last:6.1f}  peak={peak:6.1f}  "
                f"delta={delta:+6.1f}  slope={slope:+5.2f} MB/min  {verdict}"
            )
        print()


if __name__ == "__main__":
    main()
