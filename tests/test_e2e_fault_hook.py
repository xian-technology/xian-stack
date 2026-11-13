"""The one-shot fault hook the E2E harness copies into disposable containers.

One arm file selects one crash point: a block-commit boundary (with the key
and value that identify the block) or the first accepted state-sync chunk.
An arm for one kind must not disturb the other.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import pytest
from contracting.storage.driver import Driver
from xian.services.state_sync import StateSnapshotManager

HOOK = Path(__file__).resolve().parents[1] / "scripts/e2e_hooks/sitecustomize.py"


class Killed(Exception):
    pass


@pytest.fixture
def hook(tmp_path, monkeypatch):
    commit_module = importlib.import_module("xian.methods.commit")
    # The hook patches these classes; restore them after each test.
    monkeypatch.setattr(Driver, "commit", Driver.commit)
    mirror = commit_module.try_write_latest_block
    monkeypatch.setattr(commit_module, "try_write_latest_block", mirror)
    monkeypatch.setattr(
        StateSnapshotManager,
        "apply_snapshot_chunk_response",
        StateSnapshotManager.apply_snapshot_chunk_response,
    )
    spec = importlib.util.spec_from_file_location("e2e_fault_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.arm_path = tmp_path / "e2e-fault.json"
    module.hit_path = tmp_path / "e2e-fault.hit.json"

    def kill(pid, signal):
        raise Killed(pid)

    monkeypatch.setattr(module.os, "kill", kill)
    return module


@pytest.fixture
def driver(tmp_path):
    driver = Driver(storage_home=tmp_path / "storage")
    yield driver
    driver.close()


def test_a_snapshot_chunk_arm_leaves_block_commits_alone(hook, driver):
    # A state-syncing node commits at startup (the submission contract)
    # before any snapshot chunk arrives.
    hook.arm_path.write_text(json.dumps({"stage": "snapshot_chunk"}))
    driver.set("con_probe.value", 1)
    driver.commit()
    assert driver.get("con_probe.value") == 1
    assert hook.arm_path.exists()
    assert not hook.hit_path.exists()


def test_a_commit_arm_crashes_before_its_block_persists(hook, driver):
    plan = {"key": "con_probe.value", "value": 1, "stage": "before_persist"}
    hook.arm_path.write_text(json.dumps(plan))
    driver.set("con_probe.other", 2)
    driver.commit()
    driver.set("con_probe.value", 1)
    with pytest.raises(Killed):
        driver.commit()
    assert json.loads(hook.hit_path.read_text()) == {**plan, "pid": hook.os.getpid()}
    assert not hook.arm_path.exists()
