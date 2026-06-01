#!/usr/bin/env python3
"""Run repeatable TPS sweeps against a running localnet."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent
WORKLOAD_SCRIPT = SCRIPT_DIR / "localnet-workload.py"
NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"
ARTIFACTS_DIR = STACK_DIR / ".artifacts" / "tps-bench"
DEFAULT_PYTHON = "3.14"
PERF_GLOBAL_METRICS = (
    "finalize_block",
    "finalize_parallel",
    "finalize_execute",
    "finalize_result_assembly",
    "finalize_commit_prepare",
    "finalize_bds_enqueue",
    "tx_process_total",
    "tx_execute",
    "tx_process_output",
)
PERF_BLOCK_METRICS = (
    "finalize_decode",
    "finalize_parallel",
    "finalize_execute",
    "finalize_result_assembly",
    "finalize_evidence",
    "finalize_epoch_rebalance",
    "finalize_rewards",
    "finalize_commit_prepare",
    "finalize_bds_enqueue",
)


class BenchmarkError(RuntimeError):
    pass


def load_network() -> dict[str, Any]:
    if not NETWORK_PATH.exists():
        raise BenchmarkError(
            f"localnet metadata not found at {NETWORK_PATH}; run localnet-init first"
        )
    return json.loads(NETWORK_PATH.read_text(encoding="utf-8"))


def default_output_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return ARTIFACTS_DIR / f"vm-tps-bench-{timestamp}.json"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(float(statistics.median(values)), 3)


def load_perf_payloads(network: dict[str, Any]) -> dict[str, dict[str, Any]]:
    payloads = {}
    for node in network.get("nodes", []):
        perf_path = STACK_DIR / ".localnet" / str(node["moniker"]) / ".cometbft" / "xian-perf.json"
        if not perf_path.exists():
            continue
        payloads[str(node["moniker"])] = json.loads(perf_path.read_text(encoding="utf-8"))
    return payloads


def summarize_perf_payloads(
    node_payloads: dict[str, dict[str, Any]],
    *,
    recent_block_limit: int = 8,
) -> dict[str, Any]:
    if not node_payloads:
        return {"nodes": {}, "aggregate_global_metrics": {}, "aggregate_recent_blocks": []}

    nodes: dict[str, Any] = {}
    aggregate_global: dict[str, Any] = {}
    reference_payload = next(iter(node_payloads.values()))
    reference_blocks = [
        block
        for block in reference_payload.get("recent_blocks", [])
        if int(block.get("tx_count", 0)) > 0
    ]
    if recent_block_limit > 0:
        reference_blocks = reference_blocks[-recent_block_limit:]

    for node_name, payload in sorted(node_payloads.items()):
        global_metrics = payload.get("global_metrics", {})
        recent_blocks = [
            block for block in payload.get("recent_blocks", []) if int(block.get("tx_count", 0)) > 0
        ]
        if recent_block_limit > 0:
            recent_blocks = recent_blocks[-recent_block_limit:]
        nodes[node_name] = {
            "global_metrics": {
                metric_name: global_metrics.get(metric_name)
                for metric_name in PERF_GLOBAL_METRICS
                if global_metrics.get(metric_name) is not None
            },
            "recent_nonempty_block_count": len(recent_blocks),
            "latest_nonempty_block": (
                {
                    "height": recent_blocks[-1]["height"],
                    "tx_count": recent_blocks[-1]["tx_count"],
                    "duration_ms": recent_blocks[-1]["duration_ms"],
                    "metadata": recent_blocks[-1].get("metadata", {}),
                    "metrics": {
                        metric_name: recent_blocks[-1].get("metrics", {}).get(metric_name)
                        for metric_name in PERF_BLOCK_METRICS
                        if recent_blocks[-1].get("metrics", {}).get(metric_name) is not None
                    },
                }
                if recent_blocks
                else None
            ),
        }

    for metric_name in PERF_GLOBAL_METRICS:
        samples = []
        for payload in node_payloads.values():
            stat = payload.get("global_metrics", {}).get(metric_name)
            if not stat or stat.get("avg_ms") is None:
                continue
            samples.append(float(stat["avg_ms"]))
        if not samples:
            continue
        aggregate_global[metric_name] = {
            "node_count": len(samples),
            "median_avg_ms": _median(samples),
            "max_avg_ms": round(max(samples), 3),
            "min_avg_ms": round(min(samples), 3),
        }

    aggregate_recent_blocks = []
    for reference_block in reference_blocks:
        height = int(reference_block["height"])
        aggregate_recent_blocks.append(
            {
                "height": height,
                "median_tx_count": _median(
                    [
                        float(block["tx_count"])
                        for payload in node_payloads.values()
                        for block in payload.get("recent_blocks", [])
                        if int(block.get("height", -1)) == height
                    ]
                ),
                "median_duration_ms": _median(
                    [
                        float(block["duration_ms"])
                        for payload in node_payloads.values()
                        for block in payload.get("recent_blocks", [])
                        if int(block.get("height", -1)) == height
                    ]
                ),
                "median_metrics_ms": {
                    metric_name: _median(
                        [
                            float(metric["total_ms"])
                            for payload in node_payloads.values()
                            for block in payload.get("recent_blocks", [])
                            if int(block.get("height", -1)) == height
                            for metric in [block.get("metrics", {}).get(metric_name)]
                            if metric is not None and metric.get("total_ms") is not None
                        ]
                    )
                    for metric_name in PERF_BLOCK_METRICS
                },
            }
        )

    return {
        "nodes": nodes,
        "aggregate_global_metrics": aggregate_global,
        "aggregate_recent_blocks": aggregate_recent_blocks,
    }


def parse_json_payload(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    if start < 0:
        raise BenchmarkError("benchmark command produced no JSON payload")
    return json.loads(stdout[start:])


def run_workload(
    *,
    scenario: str,
    seed: str,
    operations: int,
    wallet_count: int,
    submit_workers: int,
    receipt_workers: int,
    receipt_timeout_seconds: float,
    broadcast_mode: str,
    heavy_rounds: int,
) -> dict[str, Any]:
    cmd = [
        "uv",
        "run",
        "--project",
        str(STACK_DIR.parent / "xian-py"),
        "--python",
        DEFAULT_PYTHON,
        "python3",
        str(WORKLOAD_SCRIPT),
        "--scenario",
        scenario,
        "--seed",
        seed,
        "--throughput-ops",
        str(operations),
        "--wallet-count",
        str(wallet_count),
        "--submit-workers",
        str(submit_workers),
        "--receipt-workers",
        str(receipt_workers),
        "--receipt-timeout-seconds",
        str(receipt_timeout_seconds),
        "--broadcast-mode",
        broadcast_mode,
        "--heavy-rounds",
        str(heavy_rounds),
        "--json",
    ]
    completed = subprocess.run(
        cmd,
        cwd=STACK_DIR,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return {
            "ok": False,
            "scenario": scenario,
            "operations": operations,
            "error": completed.stderr.strip() or completed.stdout.strip(),
        }
    payload = parse_json_payload(completed.stdout)
    scenario_summary = payload["scenario_summary"]
    elapsed_seconds = float(
        scenario_summary.get("elapsed_workload_seconds", payload["elapsed_seconds"])
    )
    workload_transactions = int(scenario_summary["transaction_count"])
    setup_transactions = int(scenario_summary.get("funding_transactions", 0))
    if scenario_summary.get("deploy_transaction_hash"):
        setup_transactions += 1
    committed_window = scenario_summary.get("committed_window", {})
    return {
        "ok": True,
        "scenario": scenario,
        "operations": operations,
        "elapsed_seconds": elapsed_seconds,
        "workload_transactions": workload_transactions,
        "setup_transactions": setup_transactions,
        "successful_transactions": int(scenario_summary["successful_transactions"]),
        "workload_tps": round(workload_transactions / elapsed_seconds, 3),
        "full_scenario_tps": round(
            (workload_transactions + setup_transactions) / elapsed_seconds, 3
        ),
        "committed_workload_tps": committed_window.get("committed_workload_tps"),
        "committed_chain_tps": committed_window.get("committed_chain_tps"),
        "peak_block_tps": committed_window.get("peak_block_tps"),
        "median_block_tps": committed_window.get("median_block_tps"),
        "payload": payload,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run throughput sweeps against the running localnet",
    )
    parser.add_argument(
        "--scenario",
        choices=("transfer_fanout", "contract_heavy", "both"),
        default="both",
    )
    parser.add_argument(
        "--ops",
        nargs="+",
        type=int,
        default=[4000, 8000, 12000, 16000],
        help="Transaction counts to benchmark in ascending order",
    )
    parser.add_argument("--wallet-count", type=int, default=64)
    parser.add_argument("--submit-workers", type=int, default=128)
    parser.add_argument("--receipt-workers", type=int, default=128)
    parser.add_argument("--receipt-timeout-seconds", type=float, default=90.0)
    parser.add_argument(
        "--broadcast-mode",
        choices=("async", "checktx"),
        default="checktx",
    )
    parser.add_argument("--heavy-rounds", type=int, default=64)
    parser.add_argument(
        "--output-path",
        type=Path,
        default=default_output_path(),
        help="Path to write the benchmark summary JSON",
    )
    parser.add_argument(
        "--json",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    network = load_network()
    scenarios = (
        ["transfer_fanout", "contract_heavy"] if args.scenario == "both" else [args.scenario]
    )

    runs = []
    for scenario in scenarios:
        for operations in args.ops:
            seed = f"xian-tps-bench:{scenario}:{operations}:{int(time.time())}"
            result = run_workload(
                scenario=scenario,
                seed=seed,
                operations=operations,
                wallet_count=args.wallet_count,
                submit_workers=args.submit_workers,
                receipt_workers=args.receipt_workers,
                receipt_timeout_seconds=args.receipt_timeout_seconds,
                broadcast_mode=args.broadcast_mode,
                heavy_rounds=args.heavy_rounds,
            )
            runs.append(result)
            if not result["ok"]:
                break

    best_by_scenario = {}
    for scenario in scenarios:
        successful = [run for run in runs if run["ok"] and run["scenario"] == scenario]
        if successful:
            best_by_scenario[scenario] = max(
                successful,
                key=lambda run: float(run["committed_workload_tps"] or run["workload_tps"]),
            )

    summary = {
        "ok": all(run["ok"] for run in runs),
        "network": {
            "chain_id": network["chain_id"],
            "execution": network.get("execution", {}),
            "localnet_profile": network.get("localnet_profile"),
            "consensus": network.get("consensus", {}),
        },
        "runs": runs,
        "best_by_scenario": best_by_scenario,
        "perf_summary": summarize_perf_payloads(load_perf_payloads(network)),
    }
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if args.json:
        payload = dict(summary)
        payload["artifact_path"] = str(args.output_path)
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for scenario, result in best_by_scenario.items():
            print(
                f"{scenario}: best stable committed workload TPS="
                f"{float(result['committed_workload_tps'] or result['workload_tps']):.3f} "
                f"at {result['operations']} tx"
            )
        print(f"artifact_path={args.output_path}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
