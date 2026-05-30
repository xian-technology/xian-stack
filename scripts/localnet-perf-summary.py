#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


DEFAULT_METRICS = (
    "check_tx",
    "block_packing",
    "process_proposal",
    "finalize_block",
    "finalize_decode",
    "finalize_parallel",
    "finalize_execute",
    "finalize_result_assembly",
    "finalize_commit_prepare",
    "finalize_bds_enqueue",
    "tx_process_total",
    "tx_execute",
    "tx_process_output",
)


def load_perf_files(perf_root: Path) -> dict[str, dict]:
    perf_files = sorted(perf_root.glob("node-*/.cometbft/xian-perf.json"))
    if not perf_files:
        raise FileNotFoundError(f"no xian-perf.json files found under {perf_root}")
    return {
        perf_file.parent.parent.name: json.loads(perf_file.read_text()) for perf_file in perf_files
    }


def summarize_metrics(
    node_payloads: dict[str, dict],
    metric_names: tuple[str, ...],
) -> dict[str, dict]:
    aggregate: dict[str, dict] = {}
    for metric_name in metric_names:
        samples = []
        counts = []
        totals = []
        for payload in node_payloads.values():
            stat = payload.get("global_metrics", {}).get(metric_name)
            if not stat:
                continue
            avg_ms = stat.get("avg_ms")
            total_ms = stat.get("total_ms")
            if avg_ms is None or total_ms is None:
                continue
            samples.append(float(avg_ms))
            counts.append(int(stat.get("count", 0)))
            totals.append(float(total_ms))
        if not samples:
            continue
        aggregate[metric_name] = {
            "node_count": len(samples),
            "median_avg_ms": round(statistics.median(samples), 3),
            "max_avg_ms": round(max(samples), 3),
            "min_avg_ms": round(min(samples), 3),
            "median_total_ms": round(statistics.median(totals), 3),
            "median_count": int(statistics.median(counts)),
        }
    return aggregate


def summarize_recent_blocks(
    node_payloads: dict[str, dict],
    *,
    limit: int,
) -> list[dict]:
    node_zero = next(iter(sorted(node_payloads)))
    recent_blocks = [
        block
        for block in node_payloads[node_zero].get("recent_blocks", [])
        if block.get("tx_count", 0) > 0
    ]
    if limit > 0:
        recent_blocks = recent_blocks[-limit:]
    summary = []
    for block in recent_blocks:
        metrics = block.get("metrics", {})
        tx_process_total = metrics.get("tx_process_total", {}).get("total_ms", 0.0) or 0.0
        finalize_block = metrics.get("finalize_block", {}).get("total_ms", 0.0) or 0.0
        summary.append(
            {
                "height": block["height"],
                "tx_count": block["tx_count"],
                "duration_ms": block["duration_ms"],
                "finalize_block_ms": finalize_block,
                "tx_process_total_ms": tx_process_total,
                "finalize_overhead_ms": round(finalize_block - tx_process_total, 3),
                "tx_execute_ms": metrics.get("tx_execute", {}).get("total_ms"),
                "tx_process_output_ms": metrics.get("tx_process_output", {}).get("total_ms"),
                "finalize_decode_ms": metrics.get("finalize_decode", {}).get("total_ms"),
                "finalize_parallel_ms": metrics.get("finalize_parallel", {}).get("total_ms"),
                "finalize_execute_ms": metrics.get("finalize_execute", {}).get("total_ms"),
                "finalize_result_assembly_ms": metrics.get("finalize_result_assembly", {}).get(
                    "total_ms"
                ),
                "finalize_commit_prepare_ms": metrics.get("finalize_commit_prepare", {}).get(
                    "total_ms"
                ),
                "finalize_bds_enqueue_ms": metrics.get("finalize_bds_enqueue", {}).get("total_ms"),
                "finalize_fingerprint_ms": metrics.get("finalize_fingerprint", {}).get("total_ms"),
                "parallel_enabled": block.get("metadata", {}).get("parallel_enabled"),
                "parallel_worker_count": block.get("metadata", {}).get("parallel_worker_count"),
            }
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize xian localnet profiling output.")
    parser.add_argument(
        "--perf-root",
        default=".localnet",
        help="Localnet directory containing node-*/.cometbft/xian-perf.json",
    )
    parser.add_argument(
        "--block-limit",
        type=int,
        default=5,
        help="How many recent non-empty blocks to include in the summary",
    )
    parser.add_argument(
        "--metrics",
        nargs="*",
        default=list(DEFAULT_METRICS),
        help="Metric names to summarize",
    )
    args = parser.parse_args()

    perf_root = Path(args.perf_root).expanduser().resolve()
    node_payloads = load_perf_files(perf_root)
    first_payload = next(iter(node_payloads.values()))

    summary = {
        "perf_root": str(perf_root),
        "execution_mode": first_payload.get("execution_mode"),
        "chain_id": first_payload.get("chain_id"),
        "nodes": {
            node_name: {
                "metrics": {
                    metric_name: payload.get("global_metrics", {}).get(metric_name)
                    for metric_name in args.metrics
                    if payload.get("global_metrics", {}).get(metric_name)
                },
                "recent_nonempty_block_count": sum(
                    1 for block in payload.get("recent_blocks", []) if block.get("tx_count", 0) > 0
                ),
            }
            for node_name, payload in sorted(node_payloads.items())
        },
        "aggregate_metrics": summarize_metrics(node_payloads, tuple(args.metrics)),
        "recent_nonempty_blocks": summarize_recent_blocks(
            node_payloads,
            limit=max(0, args.block_limit),
        ),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
