"""The one-shot fault hook the E2E harness copies into disposable containers.

One arm file identifies a block by key and value and selects a commit boundary.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import pytest
from contracting.storage.driver import Driver

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


@pytest.mark.parametrize(
    ("stage", "persisted", "mirrored"),
    [
        ("before_persist", False, False),
        ("after_persist", True, False),
        ("before_response", True, True),
    ],
)
def test_a_commit_arm_crashes_at_the_selected_boundary(hook, driver, stage, persisted, mirrored):
    key = "con_probe.value"
    plan = {"key": key, "value": 1, "stage": stage}
    hook.arm_path.write_text(json.dumps(plan))
    unrelated_block = {"hash": "cd" * 32, "height": 1, "nanos": 122}
    driver.set("con_probe.other", 2)
    driver.commit()
    assert hook.commit_module.try_write_latest_block(unrelated_block, driver.storage_home)
    assert driver.get_all_contract_state() == {"con_probe.other": 2}
    assert hook.arm_path.exists()
    assert not hook.hit_path.exists()

    block = {"hash": "ab" * 32, "height": 2, "nanos": 123}
    driver.set(key, 1)
    with pytest.raises(Killed):
        driver.commit()
        hook.commit_module.try_write_latest_block(block, driver.storage_home)

    assert (key in driver.get_all_contract_state()) is persisted
    mirror = driver.storage_home / "__latest_block.json"
    assert json.loads(mirror.read_text()) == (block if mirrored else unrelated_block)
    assert json.loads(hook.hit_path.read_text()) == {**plan, "pid": hook.os.getpid()}
    assert not hook.arm_path.exists()
