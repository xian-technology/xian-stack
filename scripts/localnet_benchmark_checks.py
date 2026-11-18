"""Compare like-for-like, repeated successful throughput samples."""

from __future__ import annotations

import math
import statistics


def summarize_samples(runs: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[float]] = {}
    for run in runs:
        if not run["ok"] or run["successful_transactions"] != run["workload_transactions"]:
            raise ValueError("benchmark requires every submitted transaction to succeed")
        if run["workload_transactions"] != run["operations"]:
            raise ValueError("benchmark requires every requested transaction to succeed")
        tps = float(run["workload_tps"])
        if not math.isfinite(tps) or tps <= 0:
            raise ValueError("benchmark TPS must be finite and positive")
        key = f"{run['scenario']}/{run['preparation_mode']}/{run['operations']}"
        groups.setdefault(key, []).append(tps)
    return {
        key: {
            "sample_count": len(values),
            "median_tps": statistics.median(values),
            "min_tps": min(values),
            "max_tps": max(values),
        }
        for key, values in groups.items()
    }


def validate_comparable(candidate: dict, baseline: dict) -> None:
    for report in (candidate, baseline):
        if report.get("schema_version") != 1 or not report.get("ok"):
            raise ValueError("baseline and candidate must be successful version-1 reports")
    if candidate["configuration"] != baseline["configuration"]:
        raise ValueError("benchmark configurations differ; establish a matching baseline")
    if (
        not candidate["measurements"]
        or candidate["measurements"].keys() != baseline["measurements"].keys()
    ):
        raise ValueError("benchmark measurement sets differ or are empty")


def compare_measurement(key: str, current: dict, reference: dict, tolerance: float) -> dict:
    for sample in (current, reference):
        if sample["sample_count"] < 3:
            raise ValueError("baseline comparison requires at least three measured repeats")
        if not math.isfinite(sample["median_tps"]) or sample["median_tps"] <= 0:
            raise ValueError("baseline comparison requires finite positive medians")
    ratio = current["median_tps"] / reference["median_tps"]
    return {
        "measurement": key,
        "baseline_tps": reference["median_tps"],
        "candidate_tps": current["median_tps"],
        "change_percent": round((ratio - 1) * 100, 3),
        "ok": ratio >= 1 - tolerance / 100,
    }


def compare_baseline(candidate: dict, baseline: dict, tolerance: float) -> dict:
    if not math.isfinite(tolerance) or not 0 <= tolerance < 100:
        raise ValueError("regression tolerance must be between 0 and 100 percent")
    validate_comparable(candidate, baseline)
    comparisons = [
        compare_measurement(key, current, baseline["measurements"][key], tolerance)
        for key, current in candidate["measurements"].items()
    ]
    return {"ok": all(item["ok"] for item in comparisons), "comparisons": comparisons}
