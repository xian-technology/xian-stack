from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "localnet-tps-bench.py"
sys.path.insert(0, str(MODULE_PATH.parent))


def load_tps_bench_module(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_workload_uses_xian_stack_python_env(monkeypatch) -> None:
    monkeypatch.setenv("XIAN_STACK_PYTHON", "3.14.5")
    localnet_tps_bench = load_tps_bench_module("localnet_tps_bench_env_test")
    captured = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        stdout = json.dumps(
            {
                "elapsed_seconds": 2.0,
                "scenario_summary": {
                    "transaction_count": 10,
                    "successful_transactions": 10,
                    "funding_transactions": 2,
                    "elapsed_workload_seconds": 1.0,
                    "committed_window": {
                        "committed_workload_tps": 10.0,
                        "window_basis": "chain_time",
                    },
                },
            }
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(localnet_tps_bench.subprocess, "run", fake_run)

    result = localnet_tps_bench.run_workload(
        scenario="transfer_fanout",
        seed="seed",
        operations=10,
        wallet_count=2,
        submit_workers=2,
        receipt_workers=2,
        receipt_timeout_seconds=30.0,
        broadcast_mode="checktx",
        heavy_rounds=4,
    )

    cmd = captured["cmd"]
    assert result["ok"] is True
    assert result["committed_window_basis"] == "chain_time"
    assert cmd[cmd.index("--python") + 1] == "3.14.5"


def test_campaign_discards_warmups_and_keeps_modes_separate(monkeypatch, tmp_path):
    module = load_tps_bench_module("campaign_test")
    monkeypatch.setattr(module, "load_network", lambda: {"chain_id": "local"})
    monkeypatch.setattr(module, "benchmark_configuration", lambda *_: {"nodes": 5})
    monkeypatch.setattr(module, "repository_revisions", lambda: {})
    monkeypatch.setattr(module, "load_perf_payloads", lambda *_: {})
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return dict(
            ok=True,
            scenario=kwargs["scenario"],
            preparation_mode=kwargs["preparation_mode"],
            operations=10,
            successful_transactions=10,
            workload_transactions=10,
            workload_tps=10,
            committed_workload_tps=12,
        )

    monkeypatch.setattr(module, "run_workload", run)
    path = tmp_path / "report.json"
    argv = ["--scenario", "transfer_fanout", "--ops", "10", "--output-path", str(path)]
    assert module.main(argv) == 0
    report = json.loads(path.read_text())
    assert len(calls) == 8
    assert len({call["seed"] for call in calls}) == 8
    assert len(report["warmups"]) == 2
    assert len(report["runs"]) == 6
    assert report["measurements"]["transfer_fanout/automatic/10"]["sample_count"] == 3
    assert report["baseline_comparison"] is None

    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(report))
    assert module.main(argv + ["--baseline", str(baseline)]) == 0
    for measurement in report["measurements"].values():
        measurement["median_tps"] *= 2
    baseline.write_text(json.dumps(report))
    assert module.main(argv + ["--baseline", str(baseline)]) == 1
    assert not json.loads(path.read_text())["baseline_comparison"]["ok"]
    report["configuration"]["nodes"] = 4
    baseline.write_text(json.dumps(report))
    assert module.main(argv + ["--baseline", str(baseline)]) == 1
    assert "configurations differ" in json.loads(path.read_text())["baseline_comparison"]["error"]


def test_failed_workload_does_not_publish_private_stdout(monkeypatch, tmp_path):
    module = load_tps_bench_module("private_failure_test")
    monkeypatch.setattr(module, "ARTIFACTS_DIR", tmp_path)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=1, stdout="private wallet derivation seed", stderr="private test keys"
        ),
    )
    result = module.run_workload(
        scenario="transfer_fanout",
        seed="private seed",
        operations=10,
        wallet_count=2,
        submit_workers=2,
        receipt_workers=2,
        receipt_timeout_seconds=30,
        broadcast_mode="checktx",
        heavy_rounds=4,
        preparation_mode="automatic",
    )
    assert not result["ok"]
    assert "private seed" not in json.dumps(result)
    assert "private test keys" not in json.dumps(result)
    log = Path(result["diagnostics_path"])
    assert log.stat().st_mode & 0o777 == 0o600
    assert "private test keys" in log.read_text()


def test_resource_fingerprint_excludes_private_mounts(monkeypatch):
    module = load_tps_bench_module("resource_test")
    captured = []

    def inspect(cmd, **_kwargs):
        captured.extend(cmd)
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "Memory": 1024,
                    "NanoCpus": 2_000_000_000,
                    "Binds": ["/private/test-wallet:/root/wallet"],
                }
            )
            + "\n"
        )

    monkeypatch.setattr(module.subprocess, "run", inspect)
    resources = module.container_resources(
        {
            "nodes": [
                {
                    "abci_container": "node",
                    "cometbft_container": "node",
                }
            ]
        }
    )
    assert captured.count("node") == 1
    assert resources[0]["Memory"] == 1024
    assert resources[0]["NanoCpus"] == 2_000_000_000
    assert "private" not in json.dumps(resources)
