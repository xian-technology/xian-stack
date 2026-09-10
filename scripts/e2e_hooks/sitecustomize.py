"""One-shot fault injection installed only into disposable E2E containers.

This file is never packaged into node images. The harness copies it into one
container, restarts that container, and removes it in a finally block.
"""

import importlib
import json
import os
import signal
from pathlib import Path

from contracting.storage.driver import Driver
from xian.services.state_sync import StateSnapshotManager

commit_module = importlib.import_module("xian.methods.commit")
arm_path = Path("/root/.cometbft/e2e-fault.json")
hit_path = arm_path.with_suffix(".hit.json")
original_apply = Driver.hard_apply
original_mirror = commit_module.try_write_latest_block
candidate = None


def crash(plan):
    # Remove the arm before killing so CometBFT replay can make progress.
    arm_path.unlink()
    with hit_path.open("w") as stream:
        json.dump({**plan, "pid": os.getpid()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.kill(os.getpid(), signal.SIGKILL)


def hard_apply(self, nanos):
    global candidate
    candidate = None
    if arm_path.exists():
        plan = json.loads(arm_path.read_text())
        if self.pending_writes.get(plan["key"]) == plan["value"]:
            candidate = {**plan, "nanos": str(nanos)}
            if plan["stage"] == "before_persist":
                crash(candidate)
    result = original_apply(self, nanos)
    if candidate is not None and candidate["stage"] == "after_persist":
        crash(candidate)
    return result


def write_mirror(*args, **kwargs):
    result = original_mirror(*args, **kwargs)
    if candidate is not None and candidate["stage"] == "before_response":
        crash(candidate)
    return result


Driver.hard_apply = hard_apply
commit_module.try_write_latest_block = write_mirror

# A separate observer test interrupts transfer after one accepted chunk.

original_chunk = StateSnapshotManager.apply_snapshot_chunk_response


def apply_chunk(self, *args, **kwargs):
    result = original_chunk(self, *args, **kwargs)
    if arm_path.exists():
        plan = json.loads(arm_path.read_text())
        if plan["stage"] == "snapshot_chunk" and kwargs.get("index") == 0:
            crash({**plan, "accepted_chunk": 0})
    return result


StateSnapshotManager.apply_snapshot_chunk_response = apply_chunk
