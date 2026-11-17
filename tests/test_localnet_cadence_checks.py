"""Behavioral checks for real idle cadence, without advancing the test chain."""

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))


def cadence():
    return importlib.import_module("localnet_cadence_checks")


def sample(elapsed, height, root="a" * 64):
    return {"elapsed_seconds": elapsed, "heights": [height] * 5, "app_hash": root}


def retention_point(nanos=100, duration=80, height=40, pending=False):
    return {
        "height": height,
        "last_block_nanos": nanos,
        "effective_duration": duration,
        "cutoff_nanos": nanos - duration,
        "block_cutoff_height": height - 1000,
        "pending": pending,
    }


def test_on_demand_requires_actual_quiet_heights_and_root_after_settling():
    proof = cadence().validate_window([sample(0, 40), sample(6, 40)], interval=0)
    assert proof == {"elapsed_seconds": 6, "minimum_blocks": 0, "maximum_blocks": 0}
    assert cadence().validate_window([sample(2, 40), sample(8, 40)], interval=0) == proof
    with pytest.raises(Exception, match="on-demand"):
        cadence().validate_window([sample(0, 40), sample(6, 41)], interval=0)
    with pytest.raises(Exception, match="root changed"):
        cadence().validate_window([sample(0, 40), sample(6, 40, "b" * 64)], interval=0)


@pytest.mark.parametrize("interval", [2.0, 5.0])
def test_timed_modes_produce_blocks_at_the_selected_interval(interval):
    proof = cadence().validate_window([sample(0, 40), sample(interval * 5, 45)], interval=interval)
    assert proof["minimum_blocks"] == proof["maximum_blocks"] == 5
    with pytest.raises(Exception, match="proof-block feedback"):
        cadence().validate_window([sample(0, 40), sample(interval * 5, 80)], interval=interval)
    with pytest.raises(Exception, match="did not advance"):
        cadence().validate_window([sample(0, 40), sample(interval * 5, 40)], interval=interval)
    with pytest.raises(Exception, match="root changed"):
        cadence().validate_window(
            [sample(0, 40), sample(interval * 5, 45, "b" * 64)], interval=interval
        )
    for height in (43, 47):
        cadence().validate_window([sample(0, 40), sample(interval * 5, height)], interval=interval)
    with pytest.raises(Exception, match="proof-block feedback"):
        cadence().validate_window([sample(0, 40), sample(interval * 5, 48)], interval=interval)


def test_measurement_refuses_short_and_backwards_or_incomplete_samples():
    with pytest.raises(Exception, match="too short"):
        cadence().validate_window([sample(0, 40), sample(1, 40)], interval=0)
    with pytest.raises(Exception, match="regressed"):
        cadence().validate_window([sample(0, 40), sample(6, 39)], interval=0)
    with pytest.raises(Exception, match="five"):
        cadence().validate_window([sample(0, 40), {**sample(6, 40), "heights": [40]}], interval=0)


def test_retention_clock_can_advance_but_never_resurrect_expired_evidence():
    cadence().validate_cutoff(retention_point(), retention_point(120, 100, 41))
    with pytest.raises(Exception, match="cutoff moved backward"):
        cadence().validate_cutoff(retention_point(), retention_point(120, 101, 41))
    with pytest.raises(Exception, match="retention shrank"):
        cadence().validate_cutoff(retention_point(), retention_point(120, 79, 41))
    with pytest.raises(Exception, match="Block evidence cutoff moved backward"):
        cadence().validate_cutoff(
            retention_point(), {**retention_point(120, 100, 41), "block_cutoff_height": -961}
        )


def checkpoint_fixture(monkeypatch):
    c = cadence()
    view = SimpleNamespace(
        height=10,
        app_hash="a" * 64,
        last_block_nanos=10_000_000_000,
        evidence={"max_age_duration": "100", "max_age_num_blocks": "1000", "max_bytes": "1024"},
    )
    schedule = {
        "version": 2,
        "base_max_age_duration": 90,
        "target_max_age_duration": 100,
        "growth_start_nanos": 9_000_000_000,
        "max_age_num_blocks": 1000,
        "max_bytes": 1024,
        "security_checkpoint_height": 0,
        "security_checkpoint_nanos": 9_000_000_000,
        "previous_security_checkpoint_height": None,
    }
    block = {
        "block": {
            "header": {"height": "10", "time": "1970-01-01T00:00:10Z", "app_hash": "a" * 64},
            "data": {"txs": []},
        },
        "block_id": {"hash": "B" * 64},
    }

    async def native_rpc(session, node, method, **kwargs):
        if method == "block":
            assert kwargs == {"height": 10}
            return block
        assert method == "consensus_params" and kwargs == {"height": 11}
        return {"consensus_params": {"evidence": dict(view.evidence)}}

    monkeypatch.setattr(c, "committed_policy", AsyncMock(return_value=(view, {})))
    monkeypatch.setattr(c, "query", AsyncMock(return_value=schedule))
    monkeypatch.setattr(c, "rpc", native_rpc)
    return c, view, schedule, block


def test_idle_checkpoint_reads_next_params_without_requesting_a_future_block(monkeypatch):
    c, _, _, _ = checkpoint_fixture(monkeypatch)
    result = asyncio.run(c.checkpoint(object(), [object()] * 5))
    assert result["height"] == 10 and result["effective_duration"] == 100
    assert result["block_hash"] == "B" * 64 and result["tx_count"] == 0
    c, _, _, block = checkpoint_fixture(monkeypatch)
    block["block"]["data"]["txs"] = ["one-public-transaction"]
    assert asyncio.run(c.checkpoint(object(), [object()] * 5))["tx_count"] == 1


def test_checkpoint_derives_pending_duration_from_authenticated_schedule(monkeypatch):
    c, view, schedule, _ = checkpoint_fixture(monkeypatch)
    schedule["target_max_age_duration"] = 2_000_000_000
    view.evidence["max_age_duration"] = "1000000090"
    result = asyncio.run(c.checkpoint(object(), [object()] * 5))
    assert result["effective_duration"] == 1_000_000_090
    assert result["schedule"]["target_max_age_duration"] == 2_000_000_000
    view.evidence["max_age_duration"] = "1000000091"
    with pytest.raises(Exception, match="schedule"):
        asyncio.run(c.checkpoint(object(), [object()] * 5))


def test_checkpoint_refuses_header_time_disagreement(monkeypatch):
    c, _, _, block = checkpoint_fixture(monkeypatch)
    block["block"]["header"]["time"] = "1970-01-01T00:00:11Z"
    with pytest.raises(Exception, match="header time"):
        asyncio.run(c.checkpoint(object(), [object()] * 5))


def test_rehearsal_requires_a_common_checkpoint_before_reading_fixture_state(monkeypatch):
    c = cadence()
    quiet = AsyncMock()
    monkeypatch.setattr(c, "require_quiet_fixture", quiet)
    monkeypatch.setattr(c, "ready_checkpoint", AsyncMock(side_effect=RuntimeError("forked peers")))
    runner = SimpleNamespace(
        nodes=[None] * 5, validator_wallets=[SimpleNamespace(public_key="a")] * 5
    )
    with pytest.raises(RuntimeError, match="forked peers"):
        asyncio.run(c.cadence_rehearsal(runner, None, label="invalid", expected_pending=False))
    quiet.assert_not_awaited()


def test_checkpoint_binds_block_and_byte_limits_to_the_authenticated_schedule(monkeypatch):
    c, view, _, _ = checkpoint_fixture(monkeypatch)
    view.evidence["max_age_num_blocks"] = "999"
    with pytest.raises(Exception, match="schedule"):
        asyncio.run(c.checkpoint(object(), [object()] * 5))


def test_checkpoint_requires_the_same_header_on_all_five_peers(monkeypatch):
    import copy

    c, view, _, block = checkpoint_fixture(monkeypatch)
    nodes = list(range(5))

    async def forked(session, node, method, **kwargs):
        if method == "block":
            result = copy.deepcopy(block)
            if node == 4:
                result["block_id"]["hash"] = "C" * 64
            return result
        return {"consensus_params": {"evidence": dict(view.evidence)}}

    monkeypatch.setattr(c, "rpc", forked)
    with pytest.raises(Exception, match="same block"):
        asyncio.run(c.checkpoint(None, nodes))


def test_checkpoint_refuses_native_parameters_that_differ_from_committed_view(monkeypatch):
    c, view, _, _ = checkpoint_fixture(monkeypatch)

    async def wrong_native(session, node, method, **kwargs):
        if method == "block":
            return {
                "block": {
                    "header": {
                        "height": "10",
                        "time": "1970-01-01T00:00:10Z",
                        "app_hash": "a" * 64,
                    },
                    "data": {"txs": []},
                },
                "block_id": {"hash": "B" * 64},
            }
        return {"consensus_params": {"evidence": {**view.evidence, "max_bytes": "0"}}}

    monkeypatch.setattr(c, "rpc", wrong_native)
    with pytest.raises(Exception, match="native evidence"):
        asyncio.run(c.checkpoint(object(), [object()] * 5))


def fake_clock(monkeypatch, c):
    clock = SimpleNamespace(value=0.0)

    async def sleep(seconds):
        clock.value += seconds

    monkeypatch.setattr(c, "time", SimpleNamespace(monotonic=lambda: clock.value))
    monkeypatch.setattr(c, "asyncio", SimpleNamespace(gather=asyncio.gather, sleep=sleep))
    return clock


def test_ready_checkpoint_retries_then_reports_a_bounded_failure(monkeypatch):
    c = cadence()
    fake_clock(monkeypatch, c)
    check = AsyncMock(side_effect=[RuntimeError("restarting"), {"height": 40}])
    monkeypatch.setattr(c, "checkpoint", check)
    assert asyncio.run(c.ready_checkpoint(None, [], timeout=1)) == {"height": 40}
    assert check.await_count == 2
    monkeypatch.setattr(c, "checkpoint", AsyncMock(side_effect=RuntimeError("bad native params")))
    with pytest.raises(Exception, match="bad native params"):
        asyncio.run(c.ready_checkpoint(None, [], timeout=1))


def test_sampler_records_all_five_heights_and_rejects_catchup(monkeypatch):
    c = cadence()
    clock = fake_clock(monkeypatch, c)
    clock.value = 3
    status = {"sync_info": {"latest_block_height": "40", "catching_up": False}}
    monkeypatch.setattr(c, "rpc", AsyncMock(return_value=status))
    monkeypatch.setattr(
        c, "committed_policy", AsyncMock(return_value=(SimpleNamespace(app_hash="a" * 64), {}))
    )
    assert asyncio.run(c.sample_nodes(None, [None] * 5, 1)) == sample(2, 40)
    status["sync_info"]["catching_up"] = True
    with pytest.raises(Exception, match="caught-up"):
        asyncio.run(c.sample_nodes(None, [None] * 5, 1))


def test_settle_waits_for_real_quiet_and_rejects_continuous_proofblocks(monkeypatch):
    c = cadence()
    clock = fake_clock(monkeypatch, c)
    clock.value = 10

    async def quiet(*args):
        return sample(clock.value, 40)

    monkeypatch.setattr(c, "sample_nodes", quiet)
    assert asyncio.run(c.settle(None, [], timeout=4))["heights"] == [40] * 5
    assert clock.value >= 12

    async def storm(*args):
        return sample(clock.value, 40 + int(clock.value * 4))

    monkeypatch.setattr(c, "sample_nodes", storm)
    with pytest.raises(Exception, match="never settled"):
        asyncio.run(c.settle(None, [], timeout=2))


@pytest.mark.parametrize("interval", [0, 2])
def test_observation_samples_real_progress_without_any_probe_transaction(monkeypatch, interval):
    c = cadence()
    clock = fake_clock(monkeypatch, c)
    clock.value = 10
    point = {
        **retention_point(),
        "tx_count": 0,
        "header_app_hash": "b" * 64 if interval else "a" * 64,
        "app_hash": "a" * 64,
        "last_commit_flags": [2] * 5,
    }
    monkeypatch.setattr(c, "ready_checkpoint", AsyncMock(return_value=point))

    async def heights(*args):
        elapsed = clock.value - args[-1]
        return sample(elapsed, 40 + (int(elapsed / interval) if interval else 0))

    async def block(session, node, method, *, height):
        assert method == "block"
        return {"block": {"data": {"txs": []}, "header": {"time": str(height)}}}

    monkeypatch.setattr(c, "sample_nodes", heights)
    monkeypatch.setattr(c, "rpc", block)
    probe = AsyncMock(side_effect=AssertionError("no probe permitted inside observation"))
    monkeypatch.setattr(c, "paid_probe", probe)
    result = asyncio.run(c.observe_window(None, [None] * 5, interval=interval))
    assert result["validation"]["maximum_blocks"] == (5 if interval else 0)
    assert len(result["empty_headers"]) == (5 if interval else 0)
    probe.assert_not_awaited()


def test_empty_header_verifier_detects_unrelated_traffic(monkeypatch):
    c = cadence()
    monkeypatch.setattr(
        c, "rpc", AsyncMock(return_value={"block": {"data": {"txs": ["not-empty"]}}})
    )
    with pytest.raises(Exception, match="contains a transaction"):
        asyncio.run(c.empty_headers(None, None, 1, 1))


@pytest.mark.parametrize(
    "mode,enabled,interval",
    [("on_demand", False, "0s"), ("idle_interval", False, "2s"), ("periodic", True, "2s")],
)
def test_mode_configuration_uses_canonical_node_policy(mode, enabled, interval):
    import tomllib

    text = '[consensus]\ncreate_empty_blocks = true\ncreate_empty_blocks_interval = "5s"\n'
    changed = cadence().configured_text(text, mode)
    assert tomllib.loads(changed)["consensus"] == {
        "create_empty_blocks": enabled,
        "create_empty_blocks_interval": interval,
    }


def test_restart_batch_always_starts_consensus_even_if_config_write_fails(monkeypatch, tmp_path):
    c = cadence()
    command = AsyncMock()
    monkeypatch.setattr(c, "command", command)
    runner = SimpleNamespace(
        nodes=[SimpleNamespace(cometbft_container=f"review-{i}") for i in range(5)]
    )
    with pytest.raises(IsADirectoryError):
        asyncio.run(c.install_configs(runner, None, {tmp_path: "bad"}))
    assert command.await_args_list[0].args == (
        "docker",
        "stop",
        "--time",
        "30",
        *[f"review-{i}" for i in range(5)],
    )
    assert command.await_args_list[1].args == (
        "docker",
        "start",
        *[f"review-{i}" for i in range(5)],
    )


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("expected_pending", [True, False])
def test_rehearsal_restores_exact_original_configs_even_after_failed_idle_assertion(
    monkeypatch, tmp_path, failure, expected_pending
):
    c = cadence()
    originals = {tmp_path / "config.toml": "original bytes"}
    runner = SimpleNamespace(
        nodes=[None] * 5, validator_wallets=[SimpleNamespace(public_key="account")] * 5
    )
    monkeypatch.setattr(c, "require_quiet_fixture", AsyncMock())
    point = retention_point(pending=expected_pending)
    monkeypatch.setattr(c, "ready_checkpoint", AsyncMock(return_value=point))
    monkeypatch.setattr(c, "original_configs", lambda r: originals)
    monkeypatch.setattr(c, "configured_text", lambda text, mode: mode)
    install = AsyncMock(return_value=point)
    monkeypatch.setattr(c, "install_configs", install)
    settle = AsyncMock()
    monkeypatch.setattr(c, "settle", settle)
    monkeypatch.setattr(c, "brief_absence", AsyncMock(return_value={"recovered": True}))
    observe = AsyncMock(return_value={"samples": [1]})
    if failure:
        observe.side_effect = RuntimeError("idle advanced unexpectedly")
    monkeypatch.setattr(c, "observe_window", observe)
    if failure:
        with pytest.raises(RuntimeError, match="idle advanced"):
            asyncio.run(
                c.cadence_rehearsal(runner, None, label="test", expected_pending=expected_pending)
            )
    else:
        result = asyncio.run(
            c.cadence_rehearsal(runner, None, label="test", expected_pending=expected_pending)
        )
        assert [v["mode"] for v in result["cases"]] == ["on_demand", "idle_interval", "periodic"]
        assert result["cases"][0]["brief_absence_recovery"] == {"recovered": True}
        settle.assert_awaited_once_with(None, runner.nodes)
    assert install.await_args_list[-1].args == (runner, None, originals)


@pytest.mark.parametrize("bad_checkpoint", [0, 1])
def test_idle_observation_checks_both_boundary_headers(monkeypatch, bad_checkpoint):
    c = cadence()
    clock = fake_clock(monkeypatch, c)
    point = {
        **retention_point(),
        "tx_count": 0,
        "header_app_hash": "a" * 64,
        "app_hash": "a" * 64,
        "last_commit_flags": [2] * 5,
    }
    points = [dict(point), dict(point)]
    points[bad_checkpoint]["header_app_hash"] = "b" * 64
    monkeypatch.setattr(c, "ready_checkpoint", AsyncMock(side_effect=points))

    async def sample_now(*args):
        return sample(clock.value, 40)

    monkeypatch.setattr(c, "sample_nodes", sample_now)
    with pytest.raises(Exception, match="stable committed"):
        asyncio.run(c.observe_window(None, [None] * 5, interval=0))


def test_observation_includes_a_transaction_in_the_first_sample_gap(monkeypatch):
    c = cadence()
    clock = fake_clock(monkeypatch, c)
    monkeypatch.setattr(c, "ready_checkpoint", AsyncMock(return_value=retention_point()))

    async def sample_now(*args):
        return sample(clock.value, 40 + (1 + int(clock.value / 2) if clock.value else 0))

    async def block(session, node, method, *, height):
        return {
            "block": {
                "data": {"txs": ["unexpected"] if height == 41 else []},
                "header": {"time": "0"},
            }
        }

    monkeypatch.setattr(c, "sample_nodes", sample_now)
    monkeypatch.setattr(c, "rpc", block)
    with pytest.raises(Exception, match="contains a transaction"):
        asyncio.run(c.observe_window(None, [None] * 5, interval=2))


@pytest.mark.parametrize("bad", [None, "mode", "pending", "mempool", "missed"])
def test_idle_fixture_excludes_legitimate_continuous_consensus_work(monkeypatch, bad):
    c = cadence()

    async def state(session, node, path):
        if path.endswith("selection_mode"):
            return "auto_top_n" if bad == "mode" else "manual"
        if path.endswith("pending_head"):
            return 1 if bad == "pending" else 0
        assert path.startswith("/get/validators.missed_blocks:")
        return 1 if bad == "missed" else 0

    monkeypatch.setattr(c, "query", state)
    monkeypatch.setattr(
        c, "rpc", AsyncMock(return_value={"n_txs": "1" if bad == "mempool" else "0"})
    )
    if bad:
        expected = {
            "mode": "manual",
            "pending": "slash",
            "mempool": "transactions",
            "missed": "missed",
        }
        with pytest.raises(Exception, match=expected[bad]):
            asyncio.run(c.require_quiet_fixture(None, [None] * 5, ["account"] * 5))
    else:
        proof = asyncio.run(c.require_quiet_fixture(None, [None] * 5, ["account"] * 5))
        assert proof["missed_blocks"] == [0] * 5 and proof["manual_selection"] is True
        assert proof["empty_mempools"] is True


def test_idle_header_must_already_bind_post_state_and_full_signing_commit():
    point = {
        "tx_count": 0,
        "header_app_hash": "a" * 64,
        "app_hash": "a" * 64,
        "last_commit_flags": [2] * 5,
    }
    cadence().validate_idle_commit(point)
    for changed, message in [
        ({"tx_count": 1}, "empty"),
        ({"header_app_hash": "b" * 64}, "stable committed"),
        ({"last_commit_flags": [2, 2, 2, 2, 1]}, "full five"),
    ]:
        with pytest.raises(Exception, match=message):
            cadence().validate_idle_commit({**point, **changed})


@pytest.mark.parametrize("integrated", [False, True])
def test_original_config_inventory_preserves_both_supported_runtime_mappings(
    monkeypatch, tmp_path, integrated
):
    c = cadence()
    monkeypatch.setattr(c, "STACK", tmp_path)
    nodes = []
    for index in range(5):
        node = SimpleNamespace(
            moniker=f"node-{index}",
            cometbft_container=f"comet-{index}",
            abci_container=f"comet-{index}" if integrated else f"app-{index}",
        )
        nodes.append(node)
        path = tmp_path / ".localnet" / node.moniker / ".cometbft/config/config.toml"
        path.parent.mkdir(parents=True)
        path.write_text(f"exact-{index}\n")
    inventory = c.original_configs(SimpleNamespace(nodes=nodes))
    assert len(inventory) == 5 and list(inventory.values()) == [f"exact-{i}\n" for i in range(5)]
    nodes[0].abci_container = "different-topology" if integrated else nodes[0].cometbft_container
    with pytest.raises(Exception, match="consistent runtime topology"):
        c.original_configs(SimpleNamespace(nodes=nodes))
    with pytest.raises(Exception, match="five"):
        c.original_configs(SimpleNamespace(nodes=nodes[:4]))


def test_successful_config_restart_writes_exact_text_then_checks_checkpoint(monkeypatch, tmp_path):
    c = cadence()
    cmd = AsyncMock()
    monkeypatch.setattr(c, "command", cmd)
    check = AsyncMock(return_value={"height": 41})
    monkeypatch.setattr(c, "ready_checkpoint", check)
    runner = SimpleNamespace(nodes=[SimpleNamespace(cometbft_container=f"c{i}") for i in range(5)])
    path = tmp_path / "config.toml"
    assert asyncio.run(c.install_configs(runner, None, {path: "exact\n"})) == {"height": 41}
    assert path.read_text() == "exact\n"
    check.assert_awaited_once_with(None, runner.nodes)
    assert cmd.await_count == 2


def test_paid_probe_requires_a_real_successful_receipt_and_omits_raw_payload(monkeypatch):
    c = cadence()
    send = AsyncMock(
        return_value={"success": True, "height": 45, "tx_hash": "B" * 64, "raw": "omit"}
    )
    runner = SimpleNamespace(
        founder_wallet=SimpleNamespace(public_key="owner"), submit_contract_call=send
    )
    assert asyncio.run(c.paid_probe(runner, None, "probe")) == {
        "success": True,
        "height": 45,
        "tx_hash": "B" * 64,
    }
    assert send.await_args.kwargs["kwargs"] == {"amount": 1, "to": "owner"}
    send.return_value["height"] = 1
    assert asyncio.run(c.paid_probe(runner, None, "first"))["height"] == 1
    send.return_value["height"] = 0
    with pytest.raises(Exception, match="did not commit"):
        asyncio.run(c.paid_probe(runner, None, "zero"))
    send.return_value["height"] = 45
    send.return_value["success"] = False
    with pytest.raises(Exception, match="did not commit"):
        asyncio.run(c.paid_probe(runner, None, "bad"))


@pytest.mark.parametrize("fails", [False, True])
def test_brief_absence_recovers_the_owned_peer_before_asserting_idle(monkeypatch, fails):
    c = cadence()
    runner = SimpleNamespace(
        nodes=[SimpleNamespace(cometbft_container=f"review-{i}") for i in range(5)],
        validator_wallets=[SimpleNamespace(public_key="account")],
    )
    cmd = AsyncMock()
    monkeypatch.setattr(c, "command", cmd)
    probe = (
        AsyncMock(side_effect=RuntimeError("probe rejected"))
        if fails
        else AsyncMock(return_value={"height": 40})
    )
    monkeypatch.setattr(c, "paid_probe", probe)
    monkeypatch.setattr(c, "ready_checkpoint", AsyncMock())
    monkeypatch.setattr(c, "settle", AsyncMock())
    quiet = AsyncMock()
    monkeypatch.setattr(c, "require_quiet_fixture", quiet)
    window = AsyncMock(return_value={"quiet": True})
    monkeypatch.setattr(c, "observe_window", window)
    if fails:
        with pytest.raises(RuntimeError, match="probe rejected"):
            asyncio.run(c.brief_absence(runner, None, "probe"))
        window.assert_not_awaited()
    else:
        result = asyncio.run(c.brief_absence(runner, None, "probe"))
        assert result["idle_after_recovery"] == {"quiet": True} and probe.await_count == 2
        quiet.assert_awaited_once_with(None, runner.nodes, ["account"])
        window.assert_awaited_once_with(None, runner.nodes, interval=0)
    assert cmd.await_args_list[0].args == ("docker", "stop", "--time", "30", "review-4")
    assert cmd.await_args_list[1].args == ("docker", "start", "review-4")
