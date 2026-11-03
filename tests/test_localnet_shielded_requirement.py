import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_localnet_parallel_phase import localnet_e2e as e2e


def runner(tmp_path, monkeypatch, *options):
    monkeypatch.setattr(e2e, "OUTPUT_ROOT", tmp_path)
    return e2e.E2ERunner(e2e.build_parser().parse_args(list(options)))


def test_release_requirement_fails_before_network_changes(tmp_path, monkeypatch):
    target = runner(tmp_path, monkeypatch, "--require-shielded")
    monkeypatch.setattr(e2e, "ShieldedNoteProver", None)
    monkeypatch.setattr(e2e, "XIAN_ZK_IMPORT_ERROR", "No module named 'xian_zk._native'")
    target.bootstrap = AsyncMock()
    with pytest.raises(e2e.E2EError, match="native ZK.*required"):
        asyncio.run(target.run())
    target.bootstrap.assert_not_awaited()


def test_required_shielded_phase_cannot_skip(tmp_path, monkeypatch):
    target = runner(tmp_path, monkeypatch, "--require-shielded")
    monkeypatch.setattr(e2e, "ShieldedNoteProver", None)
    monkeypatch.setattr(e2e, "XIAN_ZK_IMPORT_ERROR", "missing native library")
    monkeypatch.setattr(e2e, "collect_failure_diagnostics", AsyncMock(return_value={}))
    with pytest.raises(e2e.E2EError, match="native ZK.*required"):
        asyncio.run(target.run_phase("15-shielded-note-token", lambda: target.shielded_phase(None)))
    assert target.phase_results[-1].ok is False


def test_developer_mode_can_explicitly_allow_missing_zk(tmp_path, monkeypatch):
    target = runner(tmp_path, monkeypatch, "--no-require-shielded")
    monkeypatch.setattr(e2e, "ShieldedNoteProver", None)
    monkeypatch.setattr(e2e, "XIAN_ZK_IMPORT_ERROR", "missing native library")
    result = asyncio.run(target.shielded_phase(None))
    assert result["skipped"] is True


def test_required_shielded_flag_cannot_select_suite_without_shielded(tmp_path, monkeypatch):
    target = runner(
        tmp_path, monkeypatch, "--suite", "intentkit-x402", "--intentkit-x402", "--require-shielded"
    )
    target.bootstrap = AsyncMock()
    with pytest.raises(e2e.E2EError, match="full suite"):
        asyncio.run(target.run())
    target.bootstrap.assert_not_awaited()


def test_available_native_runtime_accepts_required_full_suite(tmp_path, monkeypatch):
    target = runner(tmp_path, monkeypatch, "--require-shielded")
    monkeypatch.setattr(e2e, "ShieldedNoteProver", object())
    check = AsyncMock(return_value={"verified": True})
    monkeypatch.setattr(
        e2e, "bind_phase_sequence", lambda *args, **kwargs: [("15-shielded-note-token", check)]
    )
    assert asyncio.run(target.run()) == 0
    check.assert_awaited_once()
    assert asyncio.run(target.finalize_summary())["require_shielded"] is True


def test_required_shielded_phase_rejects_a_skip_even_with_native_import(tmp_path, monkeypatch):
    target = runner(tmp_path, monkeypatch, "--require-shielded")
    monkeypatch.setattr(e2e, "ShieldedNoteProver", object())
    monkeypatch.setattr(e2e, "collect_failure_diagnostics", AsyncMock(return_value={}))
    with pytest.raises(e2e.E2EError, match="required.*skipped"):
        asyncio.run(
            target.run_phase("15-shielded-note-token", AsyncMock(return_value={"skipped": True}))
        )
    assert target.phase_results[-1].ok is False


@pytest.mark.parametrize("history", [[], [False], [True]])
def test_required_shielded_resume_rejects_missing_failed_or_skipped_history(
    tmp_path, monkeypatch, history
):
    target = runner(
        tmp_path,
        monkeypatch,
        "--require-shielded",
        "--start-phase",
        "16-parallel-execution",
        "--resume-dir",
        str(tmp_path),
    )
    monkeypatch.setattr(e2e, "ShieldedNoteProver", object())
    phases = [
        e2e.PhaseResult(
            name="15-shielded-note-token",
            ok=ok,
            details={"skipped": ok},
            started_at="2026-11-03T00:00:00Z",
            ended_at="2026-11-03T00:00:01Z",
        )
        for ok in history
    ]
    monkeypatch.setattr(
        target, "load_resume_context", lambda: setattr(target, "phase_results", phases)
    )
    readiness = AsyncMock()
    monkeypatch.setattr(e2e, "wait_for_localnet_ready", readiness)
    monkeypatch.setattr(e2e, "bind_phase_sequence", lambda *args, **kwargs: [])
    with pytest.raises(e2e.E2EError, match="required.*15-shielded-note-token"):
        asyncio.run(target.run())
    readiness.assert_not_awaited()


@pytest.mark.parametrize(
    ("required", "skipped", "start_phase"),
    [
        (True, False, "16-parallel-execution"),
        (False, True, "16-parallel-execution"),
        (True, True, "15-shielded-note-token"),
    ],
)
def test_resume_accepts_completed_required_checks_or_checks_that_will_run(
    required, skipped, start_phase
):
    result = e2e.PhaseResult(
        name="15-shielded-note-token",
        ok=True,
        details={"skipped": skipped},
        started_at="2026-11-03T00:00:00Z",
        ended_at="2026-11-03T00:00:01Z",
    )
    e2e.validate_resumed_phases(
        [result],
        suite="full",
        require_shielded=required,
        start_phase=start_phase,
    )


@pytest.mark.parametrize("e2e_exit", [0, 23])
def test_release_wrapper_requires_shielded_and_preserves_failure(tmp_path, e2e_exit):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    for name in ("make", "docker", "uv"):
        binary = binary_dir / name
        binary.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "if Path(sys.argv[0]).name == 'make':\n"
            "    with open(os.environ['RELEASE_TEST_CALLS'], 'a') as output:\n"
            "        output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "    if 'localnet-parallel-e2e' in sys.argv:\n"
            "        sys.exit(int(os.environ['RELEASE_TEST_EXIT']))\n"
        )
        binary.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    env = {key: value for key, value in os.environ.items() if not key.startswith("XIAN_")}
    env.update(
        PATH=f"{binary_dir}{os.pathsep}{os.environ['PATH']}",
        GITHUB_ACTIONS="false",
        LOCALNET_E2E_REQUIRE_SHIELDED="0",
        RELEASE_TEST_CALLS=str(calls),
        RELEASE_TEST_EXIT=str(e2e_exit),
        XIAN_STACK_SECRETS_ENV=str(tmp_path / "secrets.env"),
        XIAN_COMETBFT_HOME=str(tmp_path / "node"),
        XIAN_BDS_DATA_DIR=str(tmp_path / "bds"),
        XIAN_CONTRACTS_DIR=str(tmp_path / "contracts"),
    )
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "bash",
            str(root / "scripts/release-safety.sh"),
            "--skip-repo-validation",
            "--skip-protocol-safety",
            "--skip-vm-report",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == e2e_exit, result.stderr
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        ["localnet-parallel-e2e", "LOCALNET_E2E_REQUIRE_SHIELDED=1"],
        ["localnet-down"],
    ]
