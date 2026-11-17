"""The small stale-time chain must finish before fresh five-node startup."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
if "localnet_e2e" not in sys.modules:
    spec = importlib.util.spec_from_file_location("localnet_e2e", ROOT / "scripts/localnet-e2e.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
e2e = sys.modules["localnet_e2e"]


@pytest.mark.parametrize("topology", ["integrated", "fidelity"])
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("suite", ["full", "intentkit-x402"])
def test_preflight_is_serial_before_five_node_up_and_absent_on_resume(
    monkeypatch, tmp_path, topology, fresh, suite
):
    args = e2e.build_parser().parse_args([])
    args.bootstrap, args.build, args.topology, args.suite = fresh, True, topology, suite
    events = []
    preflight = AsyncMock(side_effect=lambda image: events.append(("preflight", image)))
    runner = SimpleNamespace(args=args, idle_unbond_preflight=preflight)
    env = {
        "XIAN_NODE_SPLIT_IMAGE": "split-reviewed",
        "XIAN_NODE_INTEGRATED_IMAGE": "integrated-reviewed",
        **{
            key: "fixture"
            for key in (
                "XIAN_LOCALNET_GENESIS_NETWORK",
                "XIAN_LOCALNET_ENABLE_BDS",
                "XIAN_LOCALNET_BDS_NODE_INDEX",
                "XIAN_LOCALNET_PORT_OFFSET",
                "XIAN_LOCALNET_APP_LOG_LEVEL",
                "XIAN_LOCALNET_RUNTIME_FEATURE_ZK",
                "XIAN_LOCALNET_TOPOLOGY",
                "LOCALNET_NODES",
            )
        },
    }
    monkeypatch.setattr(e2e, "make_localnet_env", lambda _: env)
    monkeypatch.setattr(e2e, "STACK_DIR", tmp_path)
    monkeypatch.setattr(e2e, "pin_localnet_addresses", AsyncMock(return_value=[]))

    def make(target, **kwargs):
        events.append((target, None))
        if target == "localnet-up":
            raise RuntimeError("five-node startup boundary")
        return SimpleNamespace(stdout="ok")

    monkeypatch.setattr(e2e, "run_make", make)
    monkeypatch.setattr(
        e2e, "load_network", lambda: (_ for _ in ()).throw(RuntimeError("resume boundary"))
    )
    with pytest.raises(RuntimeError, match="startup boundary" if fresh else "resume boundary"):
        asyncio.run(e2e.E2ERunner.bootstrap(runner, None))
    if fresh:
        selected = "split-reviewed" if topology == "fidelity" else "integrated-reviewed"
        expected = [("localnet-init", None), ("localnet-build", None)]
        if suite == "full":
            expected.append(("preflight", selected))
            preflight.assert_awaited_once_with(selected)
        else:
            preflight.assert_not_awaited()
        assert events == [*expected, ("localnet-up", None)]
    else:
        assert events == []
        preflight.assert_not_awaited()


def test_preflight_wrapper_uses_an_isolated_private_output_root(monkeypatch, tmp_path):
    import localnet_idle_unbond_checks as checks

    run = AsyncMock(return_value={"h1_unbond": True})
    monkeypatch.setattr(checks, "stale_unbond_preflight", run)
    runner = SimpleNamespace(output_dir=tmp_path)
    result = asyncio.run(e2e.E2ERunner.idle_unbond_preflight(runner, "reviewed-image"))
    assert result == {"h1_unbond": True}
    run.assert_awaited_once_with(
        image="reviewed-image", output_dir=tmp_path / "idle-unbond-private"
    )


def test_final_capture_verifies_the_positive_lock_before_exporting_any_blocks(monkeypatch):
    import localnet_idle_unbond_checks as checks
    import localnet_replay_corpus as replay

    events = []

    async def retained(*args):
        events.append("retained")
        return {"record": {"claimed": False, "amount": "1"}}

    async def capture(*args):
        events.append("capture")
        return {"height": 100}

    verify = AsyncMock(side_effect=retained)
    monkeypatch.setattr(checks, "verify_positive_record", verify, raising=False)
    monkeypatch.setattr(replay, "corpus_phase", capture)
    proof = {"unbond_id": 1}
    runner = SimpleNamespace(
        phase_results=[
            SimpleNamespace(name="02-block-cadence", ok=True, details={"positive_unbond": proof})
        ]
    )
    result = asyncio.run(e2e.E2ERunner.replay_corpus_phase(runner, None))
    assert events == ["retained", "capture"]
    assert result["positive_unbond_retained"]["record"]["amount"] == "1"
    verify.assert_awaited_once_with(runner, None, proof)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "failed", "malformed"])
def test_final_capture_refuses_missing_or_ambiguous_positive_lock_proof(monkeypatch, kind):
    import localnet_idle_unbond_checks as checks
    import localnet_replay_corpus as replay

    verify, capture = AsyncMock(return_value={}), AsyncMock(return_value={})
    monkeypatch.setattr(checks, "verify_positive_record", verify)
    monkeypatch.setattr(replay, "corpus_phase", capture)
    phase = SimpleNamespace(name="02-block-cadence", ok=True, details={"positive_unbond": {}})
    phases = [phase]
    if kind == "missing":
        phases = []
    elif kind == "duplicate":
        phases.append(phase)
    elif kind == "failed":
        phase.ok = False
    else:
        phase.details["positive_unbond"] = None
    with pytest.raises(e2e.E2EError, match="one successful positive-unbond proof"):
        asyncio.run(e2e.E2ERunner.replay_corpus_phase(SimpleNamespace(phase_results=phases), None))
    verify.assert_not_awaited()
    capture.assert_not_awaited()
