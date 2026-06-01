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


def _process_rss(container: str, lookup: str) -> float:
    cmd = f"""
python3 - <<'PY'
import subprocess

lookup = {lookup!r}
ps_output = subprocess.check_output(["ps", "-eo", "pid=,comm=,args="], text=True)
fallback = None
preferred = None

for raw_line in ps_output.splitlines():
    line = raw_line.strip()
    if not line:
        continue
    pid_text, command, args = line.split(None, 2)
    if lookup not in args:
        continue
    pid = int(pid_text)
    if fallback is None:
        fallback = pid
    if command != "docker-init":
        preferred = pid
        break

pid = preferred or fallback
if pid is None:
    print(0)
    raise SystemExit(0)

try:
    with open(f"/proc/{{pid}}/status", "r", encoding="utf-8") as handle:
        for raw_line in handle:
            if raw_line.startswith("VmRSS:"):
                print(raw_line.split()[1])
                break
        else:
            print(0)
except OSError:
    print(0)
PY
"""
    result = subprocess.run(
        ["docker", "exec", container, "bash", "-c", cmd],
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip() or "0") / 1024
    except ValueError:
        return 0.0


def get_process_memory(node: dict) -> dict[str, float]:
    """Get RSS in MiB for xian (Python) and cometbft (Go)."""
    abci_container = node["abci_container"]
    cometbft_container = node["cometbft_container"]
    python_rss = _process_rss(abci_container, "xian-abci")
    cometbft_rss = _process_rss(cometbft_container, "cometbft node")
    return {"python": python_rss, "cometbft": cometbft_rss}


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

    duration = duration_minutes * 60
    print(f"Leak hunt: {duration_minutes} min, sampling every {sample_interval}s")
    print("Tracking per-process RSS (Python ABCI vs CometBFT Go)\n")

    # Header
    hdr = f"{'Time':>5} {'TXs':>6}"
    for node in nodes:
        short = node["moniker"]
        hdr += f"  {short + '-py':>10} {short + '-cb':>10}"
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
            for node in nodes:
                sample[node["moniker"]] = get_process_memory(node)
            all_samples.append({"time": elapsed, "tx": tx_count, "mem": sample})
            last_sample = elapsed

            line = f"{elapsed:4.0f}s {tx_count:6d}"
            for node in nodes:
                moniker = node["moniker"]
                line += f"  {sample[moniker]['python']:8.1f}MB {sample[moniker]['cometbft']:8.1f}MB"
            print(line, flush=True)

        # Send transactions
        try:
            r = secrets.randbelow(3)
            if r == 0:
                xian.send(amount=1, to_address=secrets.token_hex(32), chi=100)
            elif r == 1:
                xian.send_tx(contract="con_counter", function="increment", kwargs={}, chi=100)
            else:
                xian.send_tx(
                    contract="con_counter",
                    function="add",
                    kwargs={"amount": tx_count % 100},
                    chi=100,
                )
            tx_count += 1
        except Exception:
            errors += 1
            time.sleep(0.1)

    # Final sample
    sample = {}
    for node in nodes:
        sample[node["moniker"]] = get_process_memory(node)
    elapsed = time.time() - start
    all_samples.append({"time": elapsed, "tx": tx_count, "mem": sample})
    line = f"{elapsed:4.0f}s {tx_count:6d}"
    for node in nodes:
        moniker = node["moniker"]
        line += f"  {sample[moniker]['python']:8.1f}MB {sample[moniker]['cometbft']:8.1f}MB"
    print(line, flush=True)

    print(f"\nTotal: {tx_count} txs in {elapsed:.0f}s, {errors} errors\n")

    # Analyze per-process trends
    print("=== PER-PROCESS MEMORY TREND ===\n")
    for node in nodes:
        short = node["moniker"]
        for proc in ["python", "cometbft"]:
            values = [s["mem"][short][proc] for s in all_samples]
            if len(values) < 3:
                continue
            first, last, peak = values[0], values[-1], max(values)
            delta = last - first
            n = len(values)
            xs = [s["time"] / 60 for s in all_samples]
            x_mean = sum(xs) / n
            y_mean = sum(values) / n
            slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values)) / max(
                sum((x - x_mean) ** 2 for x in xs), 1e-9
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
