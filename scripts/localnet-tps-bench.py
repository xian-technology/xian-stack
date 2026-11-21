#!/usr/bin/env python3
"""Run repeatable TPS sweeps against a running localnet."""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import statistics
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from localnet_benchmark_checks import compare_baseline, summarize_samples

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent
WORKLOAD_SCRIPT = SCRIPT_DIR / "localnet-workload.py"
NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"
ARTIFACTS_DIR = STACK_DIR / ".artifacts" / "tps-bench"
WORKLOAD_PYTHON = os.environ.get("XIAN_STACK_PYTHON", "3.14")
PERF_GLOBAL_METRICS = (
    "finalize_block",
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
    "finalize_execute",
    "finalize_result_assembly",
    "finalize_evidence",
    "finalize_epoch_rebalance",
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


def save_private_workload_output(completed) -> str:
    directory = ARTIFACTS_DIR / "private"
    directory.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="workload-", suffix=".log", dir=directory)
    with os.fdopen(fd, "w") as output:
        output.write(completed.stdout + "\n" + completed.stderr)
    return path


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
    preparation_mode: str = "simulated",
    manual_chi: int = 2000,
) -> dict[str, Any]:
    cmd = [
        "uv",
        "run",
        "--project",
        str(STACK_DIR.parent / "xian-py"),
        "--python",
        WORKLOAD_PYTHON,
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
        "--preparation-mode",
        preparation_mode,
        "--manual-chi",
        str(manual_chi),
        "--state-sample-nodes",
        "5",
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
            "preparation_mode": preparation_mode,
            "error": "workload failed; inspect the private local workload output",
            "diagnostics_path": save_private_workload_output(completed),
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
        "ok": int(scenario_summary["successful_transactions"]) == operations
        and workload_transactions == operations,
        "scenario": scenario,
        "preparation_mode": preparation_mode,
        "operations": operations,
        "elapsed_seconds": elapsed_seconds,
        "workload_transactions": workload_transactions,
        "setup_transactions": setup_transactions,
        "successful_transactions": int(scenario_summary["successful_transactions"]),
        "workload_tps": round(workload_transactions / elapsed_seconds, 3),
        "full_scenario_tps": round(
            (workload_transactions + setup_transactions) / float(payload["elapsed_seconds"]), 3
        ),
        "committed_workload_tps": committed_window.get("committed_workload_tps"),
        "committed_chain_tps": committed_window.get("committed_chain_tps"),
        "committed_window_basis": committed_window.get("window_basis"),
        "peak_block_tps": committed_window.get("peak_block_tps"),
        "median_block_tps": committed_window.get("median_block_tps"),
        "elapsed_submission_seconds": scenario_summary.get("elapsed_submission_seconds"),
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
        "--preparation-mode", choices=("manual", "automatic", "both"), default="both"
    )
    parser.add_argument("--manual-chi", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--runner-class", default=f"local-{platform.system()}-{platform.machine()}")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--max-regression-percent", type=float, default=25)
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


def repository_revisions() -> dict:
    revisions = {}
    for name in ("xian-stack", "xian-abci", "xian-contracting", "xian-py", "xian-configs"):
        repo = STACK_DIR.parent / name
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        revisions[name] = {"commit": result.stdout.strip(), "dirty": bool(dirty.stdout)}
    return revisions


def benchmark_configuration(args, network):
    genesis_path = (
        STACK_DIR / ".localnet" / network["nodes"][0]["moniker"] / ".cometbft/config/genesis.json"
    )
    genesis = json.loads(genesis_path.read_text())
    fee_policy = next(
        item["value"]
        for item in genesis["abci_genesis"]["genesis"]
        if item["key"] == "__fee_policy"
    )
    return {
        "fee_policy": fee_policy,
        "container_resources": container_resources(network),
        "consensus_params": genesis["consensus_params"],
        "measurement": "successful-transactions-per-client-wall-second-v1",
        "runner_class": args.runner_class,
        "architecture": platform.machine(),
        "cpu_count": os.cpu_count(),
        "node_count": len(network["nodes"]),
        "topology": network.get("topology"),
        "execution": network.get("execution"),
        "localnet_profile": network.get("localnet_profile"),
        "consensus": network.get("consensus"),
        "mempool": network.get("mempool"),
        "bds": network.get("bds"),
        "profiling": network.get("profiling"),
        "runtime_features": network.get("runtime_features"),
        "genesis_network": network.get("genesis_network"),
        **{
            key: getattr(args, key)
            for key in (
                "scenario",
                "ops",
                "wallet_count",
                "submit_workers",
                "receipt_workers",
                "receipt_timeout_seconds",
                "broadcast_mode",
                "heavy_rounds",
                "preparation_mode",
                "manual_chi",
                "repeats",
                "warmups",
            )
        },
    }


def container_resources(network):
    containers = sorted(
        {node[key] for node in network["nodes"] for key in ("abci_container", "cometbft_container")}
    )
    completed = subprocess.run(
        ["docker", "inspect", "--format", "{{json .HostConfig}}", *containers],
        capture_output=True,
        text=True,
        check=True,
    )
    fields = (
        "Memory",
        "MemorySwap",
        "NanoCpus",
        "CpuQuota",
        "CpuPeriod",
        "CpusetCpus",
        "PidsLimit",
    )
    return [
        {key: config.get(key) for key in fields}
        for config in map(json.loads, completed.stdout.splitlines())
    ]


def run_sweep(args, scenarios, modes):
    runs, warmups = [], []
    for scenario in scenarios:
        for operations in args.ops:
            for mode in modes:
                for repeat in range(args.warmups + args.repeats):
                    result = run_workload(
                        scenario=scenario,
                        seed=secrets.token_hex(32),
                        operations=operations,
                        wallet_count=args.wallet_count,
                        submit_workers=args.submit_workers,
                        receipt_workers=args.receipt_workers,
                        receipt_timeout_seconds=args.receipt_timeout_seconds,
                        broadcast_mode=args.broadcast_mode,
                        heavy_rounds=args.heavy_rounds,
                        preparation_mode=mode,
                        manual_chi=args.manual_chi,
                    )
                    target = warmups if repeat < args.warmups else runs
                    target.append(result)
                    if not result["ok"]:
                        break

    return runs, warmups


def validate_args(args):
    if args.repeats < 1 or args.warmups < 0 or args.manual_chi <= 0:
        raise BenchmarkError("positive repeats and manual chi, and nonnegative warmups required")
    if any(ops <= 0 for ops in args.ops):
        raise BenchmarkError("operation counts must be positive")


def apply_baseline(summary, args):
    summary["baseline_comparison"] = None
    if args.baseline:
        try:
            summary["baseline_comparison"] = compare_baseline(
                summary, json.loads(args.baseline.read_text()), args.max_regression_percent
            )
            summary["ok"] = summary["ok"] and summary["baseline_comparison"]["ok"]
        except ValueError as exc:
            summary["ok"] = False
            summary["baseline_comparison"] = {"ok": False, "error": str(exc)}


def print_report(summary, args):
    if args.json:
        print(
            json.dumps(dict(summary, artifact_path=str(args.output_path)), indent=2, sort_keys=True)
        )
    else:
        for key, measurement in summary["measurements"].items():
            print(f"{key}: median end-to-end TPS={measurement['median_tps']:.3f}")
        print(f"artifact_path={args.output_path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    network = load_network()
    scenarios = (
        ["transfer_fanout", "contract_heavy"] if args.scenario == "both" else [args.scenario]
    )

    validate_args(args)
    modes = ["manual", "automatic"] if args.preparation_mode == "both" else [args.preparation_mode]
    runs, warmups = run_sweep(args, scenarios, modes)

    summary = {
        "schema_version": 1,
        "ok": all(run["ok"] for run in runs + warmups),
        "configuration": benchmark_configuration(args, network),
        "provenance": repository_revisions(),
        "warmups": warmups,
        "network": {
            "chain_id": network["chain_id"],
            "execution": network.get("execution", {}),
            "localnet_profile": network.get("localnet_profile"),
            "consensus": network.get("consensus", {}),
        },
        "runs": runs,
        "perf_summary": summarize_perf_payloads(load_perf_payloads(network)),
    }
    summary["measurements"] = summarize_samples(runs) if summary["ok"] else {}
    apply_baseline(summary, args)
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print_report(summary, args)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
