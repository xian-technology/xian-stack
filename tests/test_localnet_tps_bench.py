from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "localnet-tps-bench.py"


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
                    "committed_window": {"committed_workload_tps": 10.0},
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
    assert cmd[cmd.index("--python") + 1] == "3.14.5"
