#!/usr/bin/env python3
"""Run repeatable TPS sweeps against a running localnet."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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
        "successful_transactions": int(
            scenario_summary["successful_transactions"]
        ),
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
        ["transfer_fanout", "contract_heavy"]
        if args.scenario == "both"
        else [args.scenario]
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
        successful = [
            run for run in runs if run["ok"] and run["scenario"] == scenario
        ]
        if successful:
            best_by_scenario[scenario] = max(
                successful,
                key=lambda run: float(
                    run["committed_workload_tps"] or run["workload_tps"]
                ),
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
