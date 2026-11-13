from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# Not resolve(): mutation runs execute the tests from a symlinked overlay.
SCRIPTS = Path(__file__).absolute().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import localnet_runtime_feature_checks as checks  # noqa: E402
from localnet_e2e_phases import phase_names  # noqa: E402
from localnet_e2e_support import E2EError  # noqa: E402
from xian_py.exception import SimulationError  # noqa: E402


def test_zk_toggle_runs_after_the_shielded_phase_in_the_full_suite():
    names = phase_names()
    index = names.index("15-zk-feature-toggle")

    assert names[index - 1] == "15-shielded-note-token"
    assert names[index + 1] == "16-block-ordering"
    assert "15-zk-feature-toggle" not in phase_names("intentkit-x402")


def phase(name, *, ok=True, details=None):
    return SimpleNamespace(name=name, ok=ok, details=details or {})


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([phase("15-shielded-note-token", details={"token": "con_private"})], "con_private"),
        ([phase("15-shielded-note-token", details={"skipped": True})], None),
        ([phase("15-shielded-note-token", ok=False, details={"token": "x"})], None),
        ([phase("14-logging")], None),
    ],
)
def test_shielded_token_is_used_only_when_this_run_deployed_it(results, expected):
    assert checks.shielded_token(results) == expected


def switch_runner(nodes):
    return SimpleNamespace(
        run_id="run",
        nodes=nodes,
        args=SimpleNamespace(app_hash_window=5),
        arm_state_patch=AsyncMock(
            return_value={"bundle": {}, "activation_height": 40, "proposal_receipt": None}
        ),
        stop_node_runtime=AsyncMock(return_value={}),
        wait_past_patch_activation=AsyncMock(return_value=120.0),
        start_node_runtime=AsyncMock(return_value={}),
    )


def test_switch_patches_the_feature_and_restarts_node4_after_activation():
    nodes = [SimpleNamespace(moniker=f"node-{index}") for index in range(5)]
    runner = switch_runner(nodes)
    order = []
    runner.stop_node_runtime.side_effect = lambda node: order.append(("stop", node.moniker))
    runner.start_node_runtime.side_effect = lambda session, node, target_height: order.append(
        ("start", node.moniker, target_height)
    )
    with (
        patch.object(checks, "query", AsyncMock(return_value=False)),
        patch.object(checks, "agreement", AsyncMock(return_value={"ok": True})) as agreement,
    ):
        result = asyncio.run(checks.switch_zk(runner, object(), enabled=False, label="off"))

    change = runner.arm_state_patch.await_args.kwargs["changes"]
    assert change == [{"key": "__runtime_features.zk", "value": False, "comment": "zk off"}]
    assert order == [("stop", "node-4"), ("start", "node-4", 41)]
    assert result["committed"] == {node.moniker: False for node in nodes}
    assert agreement.await_args.kwargs == {"window": 5}


def test_switch_fails_when_a_node_did_not_adopt_the_patched_value():
    nodes = [SimpleNamespace(moniker=f"node-{index}") for index in range(5)]
    with (
        patch.object(checks, "query", AsyncMock(side_effect=[False, False, False, False, True])),
        patch.object(checks, "agreement", AsyncMock()),
        pytest.raises(E2EError, match="did not reach every node"),
    ):
        asyncio.run(checks.switch_zk(switch_runner(nodes), object(), enabled=False, label="off"))


def test_simulation_while_off_must_fail_for_the_zk_reason():
    client = SimpleNamespace(call=AsyncMock(return_value=True))
    with pytest.raises(E2EError, match="succeeded while off"):
        asyncio.run(checks.refused_simulation(client, "con_probe"))

    client.call.side_effect = SimulationError("contract con_probe does not exist")
    with pytest.raises(E2EError, match="another reason"):
        asyncio.run(checks.refused_simulation(client, "con_probe"))

    client.call.side_effect = SimulationError(
        "contract 'con_probe' uses zk host syscalls, but the chain zk runtime feature is disabled"
    )
    assert "disabled" in asyncio.run(checks.refused_simulation(client, "con_probe"))
