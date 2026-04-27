#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.request import urlopen

STACK_DIR = Path(__file__).resolve().parent.parent
LOCALNET_NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"
_PROMETHEUS_LINE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?)$"
)
_PROMETHEUS_LABEL_RE = re.compile(
    r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"'
)


def fetch_text(url: str, *, timeout_seconds: float) -> str:
    with urlopen(url, timeout=timeout_seconds) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def parse_prometheus_text(text: str) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PROMETHEUS_LINE_RE.match(line)
        if match is None:
            continue
        labels_text = match.group("labels") or ""
        labels: dict[str, str] = {}
        if labels_text:
            for key, value in _PROMETHEUS_LABEL_RE.findall(labels_text):
                labels[key] = bytes(value, "utf-8").decode("unicode_escape")
        samples.append(
            {
                "name": match.group("name"),
                "labels": labels,
                "value": float(match.group("value")),
            }
        )
    return samples


def _samples_by_name(samples: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault(sample["name"], []).append(sample)
    return grouped


def _metric_fields(
    grouped: dict[str, list[dict[str, Any]]],
    metric_name: str,
) -> dict[str, float]:
    return {
        sample["labels"].get("field", ""): sample["value"]
        for sample in grouped.get(metric_name, [])
    }


def _stage_metric_fields(
    grouped: dict[str, list[dict[str, Any]]],
    metric_name: str,
) -> dict[str, dict[str, float]]:
    stages: dict[str, dict[str, float]] = {}
    for sample in grouped.get(metric_name, []):
        stage = sample["labels"].get("stage", "")
        field = sample["labels"].get("field", "")
        stages.setdefault(stage, {})[field] = sample["value"]
    return stages


def collect_node_vm_rollout_status(
    node: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    rpc_url = f"http://127.0.0.1:{node['host_rpc_port']}/status"
    comet_metrics_url = (
        f"http://127.0.0.1:{node['host_metrics_port']}/metrics"
    )
    xian_metrics_port = int(
        node.get("host_xian_metrics_port", node["host_metrics_port"])
    )
    metrics_url = f"http://127.0.0.1:{xian_metrics_port}/metrics"
    rpc_payload = json.loads(fetch_text(rpc_url, timeout_seconds=timeout_seconds))
    metrics_text = fetch_text(metrics_url, timeout_seconds=timeout_seconds)
    samples = parse_prometheus_text(metrics_text)
    grouped = _samples_by_name(samples)

    node_info_samples = grouped.get("xian_node_info", [])
    node_info = dict(node_info_samples[0]["labels"]) if node_info_samples else {}
    last_mismatch_samples = grouped.get("xian_vm_shadow_last_mismatch_info", [])
    last_mismatch = (
        dict(last_mismatch_samples[0]["labels"]) if last_mismatch_samples else None
    )

    return {
        "moniker": node["moniker"],
        "rpc_url": f"http://127.0.0.1:{node['host_rpc_port']}",
        "metrics_url": metrics_url,
        "comet_metrics_url": comet_metrics_url,
        "height": int(
            rpc_payload.get("result", {})
            .get("sync_info", {})
            .get("latest_block_height", 0)
        ),
        "node_info": node_info,
        "vm_shadow": {
            "metrics": _metric_fields(grouped, "xian_vm_shadow_metric"),
            "stages": _stage_metric_fields(
                grouped,
                "xian_vm_shadow_stage_metric",
            ),
            "last_mismatch": last_mismatch,
        },
    }


def collect_localnet_vm_rollout_report(
    network: dict[str, Any],
    *,
    timeout_seconds: float,
    max_shadow_mismatches: int,
) -> dict[str, Any]:
    expected_execution = dict(network.get("execution", {}) or {})
    nodes: list[dict[str, Any]] = []
    errors: list[str] = []
    for node in network.get("nodes", []):
        try:
            node_status = collect_node_vm_rollout_status(
                node,
                timeout_seconds=timeout_seconds,
            )
            if not node_status["node_info"]:
                errors.append(
                    f"{node['moniker']}: missing xian_node_info metrics at "
                    f"{node_status['metrics_url']}"
                )
            nodes.append(node_status)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{node['moniker']}: {exc}")

    execution_signature_fields = (
        "execution_mode",
        "execution_authority",
        "execution_shadow",
        "execution_bytecode_version",
        "execution_gas_schedule",
    )
    signatures = [
        tuple(node["node_info"].get(field, "") for field in execution_signature_fields)
        for node in nodes
    ]
    uniform_execution = len(set(signatures)) <= 1 if signatures else False
    expected_signature = (
        str(expected_execution.get("mode", "")),
        str(expected_execution.get("authority", "")),
        "false",
        str(expected_execution.get("bytecode_version", "")),
        str(expected_execution.get("gas_schedule", "")),
    )
    matches_expected_execution = (
        all(signature == expected_signature for signature in signatures)
        if signatures
        else False
    )

    comparisons_total = int(
        sum(
            node["vm_shadow"]["metrics"].get("comparisons_total", 0.0)
            for node in nodes
        )
    )
    mismatches_total = int(
        sum(
            node["vm_shadow"]["metrics"].get("mismatches_total", 0.0)
            for node in nodes
        )
    )
    nodes_with_mismatches = [
        node["moniker"]
        for node in nodes
        if node["vm_shadow"]["metrics"].get("mismatches_total", 0.0) > 0
    ]

    ok = (
        not errors
        and bool(nodes)
        and uniform_execution
        and matches_expected_execution
        and mismatches_total <= int(max_shadow_mismatches)
    )

    return {
        "ok": ok,
        "expected_execution": expected_execution,
        "checks": {
            "uniform_execution": uniform_execution,
            "matches_expected_execution": matches_expected_execution,
            "max_shadow_mismatches": int(max_shadow_mismatches),
            "within_shadow_mismatch_budget": (
                mismatches_total <= int(max_shadow_mismatches)
            ),
        },
        "totals": {
            "node_count": len(nodes),
            "comparisons_total": comparisons_total,
            "mismatches_total": mismatches_total,
        },
        "nodes_with_mismatches": nodes_with_mismatches,
        "errors": errors,
        "nodes": nodes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect a VM rollout report from a running localnet"
    )
    parser.add_argument(
        "--network-json",
        default=str(LOCALNET_NETWORK_PATH),
    )
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--max-shadow-mismatches", type=int, default=0)
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    network_path = Path(args.network_json).expanduser().resolve()
    network = json.loads(network_path.read_text(encoding="utf-8"))
    report = collect_localnet_vm_rollout_report(
        network,
        timeout_seconds=args.timeout_seconds,
        max_shadow_mismatches=args.max_shadow_mismatches,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).expanduser().resolve().write_text(
            rendered,
            encoding="utf-8",
        )
    print(rendered, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
