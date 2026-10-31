import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from localnet_dex_recovery import run_recovery_smoke
from localnet_e2e_phases import PHASE_SPECS
from localnet_e2e_support import E2EError


def test_dex_recovery_is_a_required_phase_after_dex_deployment():
    phases = [phase.name for phase in PHASE_SPECS]
    assert phases[phases.index("07-dex-mixed") + 1] == "07-dex-automation-recovery"
    phase = next(phase for phase in PHASE_SPECS if phase.name == "07-dex-automation-recovery")
    assert phase.method_name == "dex_automation_recovery_phase"
    assert phase.uses_session


@pytest.mark.parametrize("failed", [False, True])
def test_phase_removes_private_inputs_on_success_and_failure(tmp_path, monkeypatch, failed):
    repo = tmp_path / "automation"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/recovery_smoke.py").touch()
    output = tmp_path / "output"

    async def launch(*args, **kwargs):
        inputs = output / "inputs.private.json"
        assert inputs.stat().st_mode & 0o777 == 0o600
        assert json.loads(inputs.read_text())["private_key"] == "test-key"
        return SimpleNamespace(
            returncode=1 if failed else 0,
            communicate=AsyncMock(
                return_value=(
                    json.dumps(
                        {
                            "ok": True,
                            "nodes_verified": 5,
                            "consumed_nonce_missing_receipts": [
                                {"original_success": True},
                                {"original_success": False},
                            ],
                        }
                    ).encode(),
                    b"deliberate failure",
                )
            ),
        )

    monkeypatch.setattr("localnet_dex_recovery.asyncio.create_subprocess_exec", launch)
    if failed:
        with pytest.raises(E2EError, match="deliberate failure"):
            asyncio.run(run_recovery_smoke(repo, output, {"private_key": "test-key"}))
    else:
        assert asyncio.run(run_recovery_smoke(repo, output, {"private_key": "test-key"}))["ok"]
    assert not (output / "inputs.private.json").exists()


@pytest.mark.parametrize(
    "cases",
    [
        None,
        [],
        [{"original_success": True}],
        [{"original_success": True}, {"original_success": True}],
    ],
)
def test_phase_requires_missing_receipt_checks_for_success_and_revert(tmp_path, monkeypatch, cases):
    repo = tmp_path / "automation"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/recovery_smoke.py").touch()
    monkeypatch.setattr(
        "localnet_dex_recovery.run_json",
        AsyncMock(
            return_value={
                "ok": True,
                "nodes_verified": 5,
                "consumed_nonce_missing_receipts": cases,
            }
        ),
    )
    with pytest.raises(E2EError, match="missing receipt"):
        asyncio.run(run_recovery_smoke(repo, tmp_path / "output", {}))
