"""Offline regressions for the permanent public full-store restore rehearsal."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
import localnet_sync_checks as sync


def test_observer_configuration_explicitly_disables_peer_state_sync():
    text = 'proxy_app = "tcp://app"\n[p2p]\npersistent_peers = ""\n[statesync]\nenable = true\n'
    configured = sync.observer_config(text, "peer@node-0:26656")
    assert "enable = false" in configured
    assert 'proxy_app = "unix:///tmp/abci.sock"' in configured
    assert 'persistent_peers = "peer@node-0:26656"' in configured


def test_identity_fingerprint_contains_hashes_and_detects_signing_state_changes(tmp_path):
    paths = [
        "config/node_key.json",
        "config/priv_validator_key.json",
        "data/priv_validator_state.json",
    ]
    for relative in paths:
        p = tmp_path / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("private-" + relative)
    before = sync.identity_fingerprint(tmp_path)
    assert set(before) == set(paths)
    assert all(len(value) == 64 for value in before.values())
    (tmp_path / paths[-1]).write_text("higher signing height")
    assert sync.identity_fingerprint(tmp_path) != before


def test_failed_restore_must_not_publish_any_application_data(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/priv_validator_state.json").write_text("local-highwater")
    runner = SimpleNamespace(output_dir=tmp_path)
    monkeypatch.setattr(
        sync,
        "snapshot_cli",
        AsyncMock(
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="signature rejected")
        ),
    )
    before = sync.restore_fingerprint(tmp_path)
    result = asyncio.run(
        sync.rejected_restore(runner, tmp_path, "file:///bad.json", "bad-signature")
    )
    assert result["rejected"] is True
    assert sync.restore_fingerprint(tmp_path) == before


def test_failure_case_refuses_a_restore_that_returns_success(tmp_path, monkeypatch):
    runner = SimpleNamespace(output_dir=tmp_path)
    monkeypatch.setattr(
        sync,
        "snapshot_cli",
        AsyncMock(return_value=SimpleNamespace(returncode=0, stdout="{}", stderr="")),
    )
    with pytest.raises(Exception, match="unexpectedly succeeded"):
        asyncio.run(sync.rejected_restore(runner, tmp_path, "file:///bad.json", "bad"))


def test_failure_case_detects_partial_publication(tmp_path, monkeypatch):
    runner = SimpleNamespace(output_dir=tmp_path)

    async def bad_restore(*args, **kwargs):
        (tmp_path / "xian").mkdir()
        (tmp_path / "xian/state").write_text("partial")
        return SimpleNamespace(returncode=1, stdout="", stderr="native store missing")

    monkeypatch.setattr(sync, "snapshot_cli", bad_restore)
    with pytest.raises(Exception, match="published"):
        asyncio.run(sync.rejected_restore(runner, tmp_path, "file:///bad.json", "incomplete"))


def test_cli_environment_selects_current_source_and_preserves_existing_path(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "existing-source")
    env = sync.cli_environment(Path("/workspace/xian-stack"))
    assert env["PYTHONPATH"].split(os.pathsep)[:3] == [
        "/workspace/xian-cli/src",
        "/workspace/xian-abci/src",
        "existing-source",
    ]


def test_module_no_longer_advertises_or_exercises_peer_snapshot_sync():
    source = Path(sync.__file__).read_text()
    assert "StateSnapshotManager" not in source
    assert "snapshot_chunk" not in source
    assert "state_sync_interrupted" not in source
    assert "snapshot export" in source and "snapshot restore" in source


def test_full_store_stage_orders_stop_export_verification_restore_and_evidence(
    tmp_path, monkeypatch
):
    runner = SimpleNamespace(output_dir=tmp_path, run_id="unit-run")
    events = []

    async def record(name, value=None):
        events.append(name)
        return value

    monkeypatch.setattr(
        sync,
        "prepare_home",
        lambda *args: record("prepare", [{"filename": "historical.json", "sha256": "a" * 64}]),
    )
    monkeypatch.setattr(
        sync,
        "create_observer",
        lambda *args: record("start-" + args[-1], SimpleNamespace(moniker=args[-1])),
    )
    monkeypatch.setattr(
        sync,
        "observer_agreement",
        lambda *args: record("agreement", {"voting_power": 0, "consensus": {"ok": True}}),
    )
    monkeypatch.setattr(
        sync, "retention_rehearsal", lambda *args: record("retention", {"target_after": 9})
    )
    monkeypatch.setattr(sync, "command", lambda *args: record(" ".join(map(str, args[:3]))))
    monkeypatch.setattr(sync, "advance_recipient_signing_state", lambda *args: None)
    monkeypatch.setattr(sync, "stop_backup_source", lambda *args: record("stop-source"))
    manifest = {"height": 20, "manifest_version": 2, "snapshot_kind": "full_node_store"}
    monkeypatch.setattr(
        sync,
        "snapshot_export",
        lambda *args: record("export", (tmp_path / "manifest.json", manifest, {"bad": "url"})),
    )
    monkeypatch.setattr(
        sync,
        "restore_snapshot",
        lambda *args: record("negative-then-valid-restore", [{"rejected": True}]),
    )
    monkeypatch.setattr(
        sync, "historical_evidence", lambda *args: record("old-proof", {"included_height": 30})
    )
    monkeypatch.setattr(
        sync,
        "repeated_restart",
        lambda *args: record("restart-twice", [{"attempt": 1}, {"attempt": 2}]),
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(sync, "remove_observer", cleanup)
    proof = asyncio.run(
        sync.full_store_rehearsal(
            runner, object(), "image", "network", tmp_path / "source", "peers"
        )
    )
    assert (
        events.index("stop-source")
        < events.index("export")
        < events.index("negative-then-valid-restore")
        < events.index("start-full-node-store")
        < events.index("old-proof")
        < events.index("restart-twice")
    )
    assert proof["snapshot"] == manifest and proof["historical_evidence"]["included_height"] == 30
    assert (
        proof["public_state_patch_bundles"]["replay"]
        == proof["public_state_patch_bundles"]["full_node_store"]
    )
    assert cleanup.await_count == 2


def test_failed_export_still_removes_stopped_replay_observer(tmp_path, monkeypatch):
    runner = SimpleNamespace(output_dir=tmp_path, run_id="unit-run")
    for name in [
        "prepare_home",
        "observer_agreement",
        "retention_rehearsal",
        "command",
        "stop_backup_source",
    ]:
        monkeypatch.setattr(sync, name, AsyncMock(return_value={}))
    monkeypatch.setattr(
        sync,
        "prepare_home",
        AsyncMock(return_value=[{"filename": "historical.json", "sha256": "a" * 64}]),
    )
    monkeypatch.setattr(sync, "create_observer", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        sync, "snapshot_export", AsyncMock(side_effect=ValueError("bad native checkpoint"))
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(sync, "remove_observer", cleanup)
    with pytest.raises(ValueError, match="bad native checkpoint"):
        asyncio.run(
            sync.full_store_rehearsal(
                runner, object(), "image", "network", tmp_path / "source", "peers"
            )
        )
    assert cleanup.await_count == 1


def test_retention_rehearsal_raises_above_existing_highwater_then_restores_original_days(
    monkeypatch,
):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_state(self, *args):
            return 0

    runner = SimpleNamespace(
        nodes=[object()], validator_wallets=[object()], client=lambda *args: Client()
    )
    day = sync.DAY_NS
    policies = [
        {"target_max_age_duration": 8 * day, "max_age_duration": 8 * day},
        {"target_max_age_duration": 10 * day, "max_age_duration": 8 * day + 1},
        {"target_max_age_duration": 10 * day, "max_age_duration": 8 * day + 2},
    ]
    monkeypatch.setattr(sync, "query", AsyncMock(side_effect=policies))
    votes = AsyncMock(return_value={"approved": True})
    monkeypatch.setattr(sync, "governance_vote", votes)
    monkeypatch.setattr(sync, "collect_policy_proof", AsyncMock(return_value={"linked": True}))
    result = asyncio.run(sync.retention_rehearsal(runner, object()))
    assert votes.await_args_list[0].args[3] == {"unbonding_period_days": 9}
    assert votes.await_args_list[1].args[3] == {"unbonding_period_days": 0}
    assert result["target_after"] == 10 * day


def test_target_highwater_cannot_shrink_after_governed_reduction(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_state(self, *args):
            return 0

    runner = SimpleNamespace(
        nodes=[object()], validator_wallets=[object()], client=lambda *args: Client()
    )
    policies = [
        {"target_max_age_duration": 8 * sync.DAY_NS, "max_age_duration": 8 * sync.DAY_NS},
        {"target_max_age_duration": 10 * sync.DAY_NS, "max_age_duration": 8 * sync.DAY_NS + 1},
        {"target_max_age_duration": 8 * sync.DAY_NS, "max_age_duration": 8 * sync.DAY_NS},
    ]
    monkeypatch.setattr(sync, "query", AsyncMock(side_effect=policies))
    monkeypatch.setattr(sync, "governance_vote", AsyncMock(return_value={}))
    monkeypatch.setattr(sync, "collect_policy_proof", AsyncMock(return_value={}))
    with pytest.raises(Exception, match="high-water shrank"):
        asyncio.run(sync.retention_rehearsal(runner, object()))


def test_growth_proof_requires_actual_effective_retention_to_increase(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_state(self, *args):
            return 0

    runner = SimpleNamespace(
        nodes=[object()], validator_wallets=[object()], client=lambda *args: Client()
    )
    before = {"target_max_age_duration": 8 * sync.DAY_NS, "max_age_duration": 8 * sync.DAY_NS}
    raised = {**before, "target_max_age_duration": 10 * sync.DAY_NS}
    monkeypatch.setattr(sync, "query", AsyncMock(side_effect=[before, raised, raised]))
    monkeypatch.setattr(sync, "governance_vote", AsyncMock(return_value={}))
    monkeypatch.setattr(sync, "collect_policy_proof", AsyncMock(return_value={}))
    with pytest.raises(Exception, match="Effective retention did not grow"):
        asyncio.run(sync.retention_rehearsal(runner, object()))


def test_consensus_check_also_requires_identical_block_ids(monkeypatch):
    nodes = [SimpleNamespace(moniker="a"), SimpleNamespace(moniker="b")]
    responses = [
        {"block_id": {"hash": "A"}, "block": {"header": {"height": "4"}}},
        {"block_id": {"hash": "B"}, "block": {"header": {"height": "4"}}},
    ]
    monkeypatch.setattr(sync, "rpc", AsyncMock(side_effect=responses))
    with pytest.raises(Exception, match="block identities"):
        asyncio.run(sync.common_block_proof(object(), nodes, {"checks": [{"height": 4}]}))


def test_common_block_proof_retains_the_agreed_public_header(monkeypatch):
    block = {"block_id": {"hash": "same"}, "block": {"header": {"height": "4"}}}
    monkeypatch.setattr(sync, "rpc", AsyncMock(return_value=block))
    result = asyncio.run(
        sync.common_block_proof(
            object(), [object(), object()], {"checks": [{"height": 3}, {"height": 4}]}
        )
    )
    assert result == {"height": 4, "block_hash": "same", "header": {"height": "4"}}


def test_public_cli_invocation_has_current_source_and_isolated_compose_project(
    tmp_path, monkeypatch
):
    observed = []

    def run(args, **kwargs):
        observed.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(sync.subprocess, "run", run)
    asyncio.run(
        sync.snapshot_cli(
            object(), tmp_path, "export", "snapshot-source", "--signing-key-file", tmp_path / "key"
        )
    )
    args, kwargs = observed[0]
    assert args[:4] == [sys.executable, "-m", "xian_cli", "snapshot"]
    assert args[-2:] == ["--base-dir", str(tmp_path)]
    assert kwargs["env"]["COMPOSE_PROJECT_NAME"].startswith("xian-snapshot-check-")
    assert kwargs["env"]["XIAN_STACK_TOPOLOGY"] == "integrated"


def test_public_cli_command_failure_preserves_its_actual_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr="native DB locked"),
    )
    with pytest.raises(Exception, match="native DB locked"):
        asyncio.run(sync.snapshot_cli(object(), tmp_path, "export", "source"))


def test_prepare_home_uses_only_public_source_configuration_and_creates_own_keys(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    (source / "config").mkdir(parents=True)
    for name, text in {
        "config.toml": (
            'proxy_app = "old"\n[p2p]\npersistent_peers = "old"\n[statesync]\nenable = true\n'
        ),
        "genesis.json": '{"chain_id":"test"}',
        "xian.toml": "bds_enabled = true\n",
        "node_key.json": "source secret",
        "priv_validator_key.json": "source validator secret",
    }.items():
        (source / "config" / name).write_text(text)
    target = tmp_path / "target"
    calls = []

    async def init(*args, **kwargs):
        calls.append(args)
        (target / "config/node_key.json").write_text("new own key")
        (target / "config/priv_validator_key.json").write_text("new validator key")
        (target / "data").mkdir()
        (target / "data/priv_validator_state.json").write_text("own state0")

    monkeypatch.setattr(sync, "command", init)
    asyncio.run(sync.prepare_home(source, target, "current-image", "peers"))
    assert (target / "config/node_key.json").read_text() == "new own key"
    assert "enable = false" in (target / "config/config.toml").read_text()
    assert (target / "config/xian.toml").read_text() == "bds_enabled = false\n"
    assert "--user" in calls[0] and calls[0][-2:] == ("--home", "/node-home")
    with pytest.raises(Exception, match="already exists"):
        asyncio.run(sync.prepare_home(source, target, "current-image", "peers"))


def test_observer_port_is_refreshed_from_actual_container_mapping(monkeypatch):
    payload = [
        {
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {"26657/tcp": [{"HostPort": "45123"}]}},
        }
    ]
    monkeypatch.setattr(sync, "command", AsyncMock(return_value=json.dumps(payload)))
    node = asyncio.run(sync.observer_address("container", "observer"))
    assert node.rpc_url == "http://127.0.0.1:45123"
    payload[0]["State"]["Running"] = False
    sync.command.return_value = json.dumps(payload)
    with pytest.raises(Exception, match="exited before RPC"):
        asyncio.run(sync.observer_address("container", "observer"))


def test_startup_failure_cleans_only_its_new_observer_container(tmp_path, monkeypatch):
    calls = AsyncMock(return_value="")
    monkeypatch.setattr(sync, "command", calls)
    monkeypatch.setattr(sync, "config_hashes", lambda *args: "{}")
    monkeypatch.setattr(
        sync, "observer_address", AsyncMock(side_effect=ValueError("startup failed"))
    )
    with pytest.raises(ValueError, match="startup failed"):
        asyncio.run(sync.create_observer(tmp_path, "own-container", "image", "bridge", "observer"))
    assert calls.await_args_list[-1].args == ("docker", "rm", "-f", "-v", "own-container")


def test_observer_log_saved_before_container_removal(tmp_path, monkeypatch):
    calls = AsyncMock(side_effect=["public logs", ""])
    monkeypatch.setattr(sync, "command", calls)
    asyncio.run(
        sync.remove_observer(SimpleNamespace(output_dir=tmp_path), "own-container", "replay")
    )
    assert (tmp_path / "observer-replay.log").read_text() == "public logs"
    assert calls.await_args_list[-1].args == ("docker", "rm", "-f", "-v", "own-container")


def test_model_profiles_reference_fresh_homes_current_image_and_publisher(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(sync.STACK.parent / "xian-cli/src"))
    sync.write_snapshot_profiles(
        tmp_path / "profiles",
        tmp_path / "source",
        tmp_path / "target",
        "current-image",
        "a" * 64,
        "local-test",
    )
    target = json.loads((tmp_path / "profiles/nodes/snapshot-target.json").read_text())
    assert target["home"] == str(tmp_path / "target")
    assert target["node_integrated_image"] == "current-image"
    assert target["snapshot_signing_keys"] == ["a" * 64]
    assert not target["services"]["bds"]["enabled"]


def test_historical_evidence_search_advances_without_repeating_scanned_heights(monkeypatch):
    monkeypatch.setattr(sync, "height", AsyncMock(side_effect=[10, 11]))
    responses = [
        {"block": {"evidence": {"evidence": []}}},
        {"block": {"evidence": {"evidence": [{"value": {"vote_a": {"height": "7"}}}]}}},
    ]
    calls = AsyncMock(side_effect=responses)
    monkeypatch.setattr(sync, "rpc", calls)
    monkeypatch.setattr(sync.asyncio, "sleep", AsyncMock())
    found = asyncio.run(sync.find_evidence_block(object(), object(), 10, 7))
    assert found == 11
    assert [call.kwargs["height"] for call in calls.await_args_list] == [10, 11]


def test_evidence_search_timeout_is_a_failure_not_a_skipped_check(monkeypatch):
    ticks = iter([0, 121])
    monkeypatch.setattr(sync, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(RuntimeError, match="not committed"):
        asyncio.run(sync.find_evidence_block(object(), object(), 10, 7))


def test_validator_recovery_readds_manual_target_after_unjail(monkeypatch):
    before = [{"account": "node0", "power": 10}, {"account": "node4", "power": 10}]
    monkeypatch.setattr(
        sync,
        "wait_validator_ready",
        AsyncMock(return_value={"jailed": True, "pending_slash_count": 0}),
    )
    votes = AsyncMock(side_effect=[{"type": "unjail"}, {"type": "add"}])
    monkeypatch.setattr(sync, "governance_vote", votes)
    monkeypatch.setattr(sync, "validator_selection", AsyncMock(return_value=before[:1]))
    monkeypatch.setattr(sync, "wait_selection", AsyncMock(return_value=before))
    result = asyncio.run(sync.restore_evidence_validator(object(), object(), before, "node4"))
    assert [call.args[2] for call in votes.await_args_list] == ["unjail_member", "add_member"]
    assert result["restored_active"] == before


def test_validator_recovery_does_not_readd_an_automatically_restored_member(monkeypatch):
    before = [{"account": "node0", "power": 10}, {"account": "node4", "power": 10}]
    monkeypatch.setattr(
        sync,
        "wait_validator_ready",
        AsyncMock(return_value={"jailed": True, "pending_slash_count": 0}),
    )
    votes = AsyncMock(return_value={})
    monkeypatch.setattr(sync, "governance_vote", votes)
    monkeypatch.setattr(sync, "validator_selection", AsyncMock(return_value=before))
    monkeypatch.setattr(sync, "wait_selection", AsyncMock(return_value=before))
    result = asyncio.run(sync.restore_evidence_validator(object(), object(), before, "node4"))
    assert votes.await_count == 1 and result["add_member"] is None


def test_restarts_refresh_endpoint_and_keep_retention_highwater(monkeypatch):
    runner = SimpleNamespace(nodes=[])
    policies = [
        {"target_max_age_duration": 10, "max_age_duration": 8},
        {"target_max_age_duration": 10, "max_age_duration": 9},
        {"target_max_age_duration": 10, "max_age_duration": 9},
        {"target_max_age_duration": 10, "max_age_duration": 10},
    ]
    monkeypatch.setattr(sync, "query", AsyncMock(side_effect=policies))
    commands = AsyncMock()
    monkeypatch.setattr(sync, "command", commands)
    addresses = AsyncMock(side_effect=[object(), object()])
    monkeypatch.setattr(sync, "observer_address", addresses)
    monkeypatch.setattr(sync, "observer_agreement", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(sync, "collect_policy_proof", AsyncMock(return_value={"linked": True}))
    result = asyncio.run(
        sync.repeated_restart(runner, object(), "own-container", object(), {"target_after": 10})
    )
    assert len(result) == 2 and addresses.await_count == 2
    assert commands.await_args.args == ("docker", "restart", "--time", "30", "own-container")


def test_restart_cannot_erase_retention_target(monkeypatch):
    monkeypatch.setattr(
        sync,
        "query",
        AsyncMock(
            side_effect=[
                {"max_age_duration": 8},
                {"target_max_age_duration": 9, "max_age_duration": 9},
            ]
        ),
    )
    monkeypatch.setattr(sync, "command", AsyncMock())
    monkeypatch.setattr(sync, "observer_address", AsyncMock(return_value=object()))
    monkeypatch.setattr(sync, "observer_agreement", AsyncMock(return_value={}))
    with pytest.raises(Exception, match="retention target changed"):
        asyncio.run(
            sync.repeated_restart(
                object(), object(), "own-container", object(), {"target_after": 10}
            )
        )


def test_negative_archives_are_independently_signed_but_fail_distinct_integrity_conditions(
    tmp_path, monkeypatch
):
    import gzip
    import hashlib
    import io
    import tarfile

    from xian.node_admin import verify_full_node_store_manifest

    wallet = sync.Wallet()
    archive = tmp_path / "store.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name in ["data/blockstore.db/CURRENT", "data/state.db/CURRENT", "xian/lmdb/data.mdb"]:
            value = name.encode()
            member = tarfile.TarInfo(name)
            member.size = len(value)
            stream.addfile(member, io.BytesIO(value))
    original = archive.read_bytes()
    manifest = {
        "manifest_version": 2,
        "snapshot_kind": "full_node_store",
        "chain_id": "unit-chain",
        "height": 9,
        "genesis_sha256": "a" * 64,
        "app_hash": "b" * 64,
        "block_hash": "c" * 64,
        **sync.archive_metadata(archive),
        "signing_public_key": wallet.public_key,
    }
    urls = sync.negative_manifests(archive, manifest, wallet, tmp_path)
    assert set(urls) == {"tampered-archive", "wrong-chain", "incomplete-native-store"}
    docs = {name: json.loads((tmp_path / (name + ".json")).read_text()) for name in urls}
    for doc in docs.values():
        verify_full_node_store_manifest(doc, trusted_manifest_public_keys=[wallet.public_key])
    assert docs["wrong-chain"]["chain_id"] == "unit-chain-wrong"
    assert (tmp_path / "tampered.tar.gz").stat().st_size == archive.stat().st_size
    assert (
        hashlib.sha256((tmp_path / "tampered.tar.gz").read_bytes()).hexdigest()
        != docs["tampered-archive"]["snapshot_sha256"]
    )
    missing = docs["incomplete-native-store"]
    assert missing["uncompressed_bytes"] == len(
        gzip.decompress((tmp_path / "incomplete.tar.gz").read_bytes())
    )
    with tarfile.open(tmp_path / "incomplete.tar.gz") as stream:
        assert not any(name.startswith("data/blockstore.db/") for name in stream.getnames())
        assert "xian/lmdb/data.mdb" in stream.getnames()
    assert archive.read_bytes() == original


def test_public_restore_preserves_recipient_identity_and_highwater(tmp_path, monkeypatch):
    for relative in sync.IDENTITY_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("recipient " + relative)
    monkeypatch.setattr(sync, "snapshot_cli", AsyncMock(return_value=SimpleNamespace(returncode=0)))
    runner = SimpleNamespace(output_dir=tmp_path)
    assert (
        asyncio.run(sync.restore_snapshot(runner, tmp_path, tmp_path / "manifest.json", {})) == []
    )


def test_public_restore_cannot_replace_signing_state_even_on_success_return(tmp_path, monkeypatch):
    for relative in sync.IDENTITY_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("recipient " + relative)

    async def replaced(*args, **kwargs):
        (tmp_path / "data/priv_validator_state.json").write_text("source state0")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(sync, "snapshot_cli", replaced)
    with pytest.raises(Exception, match="imported another node"):
        asyncio.run(
            sync.restore_snapshot(
                SimpleNamespace(output_dir=tmp_path), tmp_path, tmp_path / "manifest.json", {}
            )
        )


def test_members_vote_barrier_waits_past_latest_receipt_on_every_peer(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    vote = {"proposal_receipt": {"height": 12}, "vote_receipts": [{"height": 13}, {"height": 15}]}
    runner = SimpleNamespace(
        nodes=[object(), object()],
        validator_wallets=[1, 2, 3, 4, 5],
        client=lambda *args: Client(),
        approve_members_vote=AsyncMock(return_value=vote),
    )
    wait = AsyncMock()
    monkeypatch.setattr(sync, "wait_height", wait)
    result = asyncio.run(sync.governance_vote(runner, object(), "unjail_member", "node4", "unit"))
    assert result == vote
    assert wait.await_count == 2 and all(call.args[2] == 16 for call in wait.await_args_list)
    assert len(runner.approve_members_vote.await_args.args[1]) == 3


def test_penalty_job_must_complete_before_jailed_validator_is_rehabilitated(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        call = AsyncMock(
            side_effect=[
                {"jailed": True, "pending_slash_count": 2},
                {"jailed": True, "pending_slash_count": 0},
            ]
        )

    runner = SimpleNamespace(validator_wallets=[object()], client=lambda *args: Client())
    monkeypatch.setattr(sync.asyncio, "sleep", AsyncMock())
    record = asyncio.run(sync.wait_validator_ready(runner, object(), "node4"))
    assert record["pending_slash_count"] == 0 and Client.call.await_count == 2


def test_validator_recovery_timeout_fails_instead_of_unjailing_early(monkeypatch):
    ticks = iter([0, 121])
    monkeypatch.setattr(sync, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(RuntimeError, match="did not finish"):
        asyncio.run(sync.wait_validator_ready(object(), object(), "node4"))


def test_selection_barrier_requires_exact_powers_not_just_account_names(monkeypatch):
    wanted = [{"account": "node0", "power": 10}, {"account": "node4", "power": 12}]
    monkeypatch.setattr(
        sync,
        "validator_selection",
        AsyncMock(
            side_effect=[
                [{"account": "node0", "power": 10}, {"account": "node4", "power": 11}],
                list(reversed(wanted)),
            ]
        ),
    )
    agreement = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(sync, "agreement", agreement)
    monkeypatch.setattr(sync, "verify_native_selection", AsyncMock(return_value=[]))
    monkeypatch.setattr(sync.asyncio, "sleep", AsyncMock())
    result = asyncio.run(sync.wait_selection(SimpleNamespace(nodes=[]), object(), wanted))
    assert result == list(reversed(wanted)) and agreement.await_count == 1


def test_observer_agreement_reads_state_and_full_block_identity(monkeypatch):
    observer = object()
    source = object()
    runner = SimpleNamespace(nodes=[source], founder_wallet=SimpleNamespace(public_key="owner"))
    monkeypatch.setattr(sync, "height", AsyncMock(return_value=30))
    monkeypatch.setattr(sync, "wait_height", AsyncMock())
    monkeypatch.setattr(
        sync, "rpc", AsyncMock(return_value={"validator_info": {"voting_power": "0"}})
    )
    monkeypatch.setattr(sync, "agreement", AsyncMock(return_value={"checks": [{"height": 31}]}))
    monkeypatch.setattr(sync, "query", AsyncMock(return_value=7))
    common = AsyncMock(return_value={"height": 31, "block_hash": "hash"})
    monkeypatch.setattr(sync, "common_block_proof", common)
    result = asyncio.run(sync.observer_agreement(runner, object(), observer))
    assert result["voting_power"] == 0 and result["common_block"]["block_hash"] == "hash"
    assert common.await_args.args[1] == [source, observer]


def test_fresh_node_phase_is_bound_to_current_runtime_image_and_private_bridge(
    tmp_path, monkeypatch
):
    source = SimpleNamespace(moniker="node-0", abci_container="node0-app")
    runner = SimpleNamespace(
        nodes=[source], network={"nodes": [{"node_id": "ABC", "moniker": "node-0"}]}
    )
    monkeypatch.setattr(
        sync,
        "command",
        AsyncMock(
            return_value=json.dumps(
                [
                    {
                        "Image": "sha256:current",
                        "NetworkSettings": {"Networks": {"review-private": {}}},
                    }
                ]
            )
        ),
    )
    rehearsal = AsyncMock(return_value={"done": True})
    monkeypatch.setattr(sync, "full_store_rehearsal", rehearsal)
    assert asyncio.run(sync.sync_phase(runner, object())) == {"done": True}
    assert rehearsal.await_args.args[2:4] == ("sha256:current", "review-private")
    assert rehearsal.await_args.args[-1] == "abc@node-0:26656"


def test_restore_recipient_has_a_distinct_nonzero_signing_highwater(tmp_path):
    state = tmp_path / "data/priv_validator_state.json"
    state.parent.mkdir()
    state.write_text('{"height":"0","round":0,"step":0}')
    sync.advance_recipient_signing_state(tmp_path, 50)
    assert int(json.loads(state.read_text())["height"]) > 50


@pytest.fixture
def historical_context(monkeypatch):
    accounts = [{"account": f"node{i}", "power": 10} for i in range(5)]
    runner = SimpleNamespace(
        nodes=[SimpleNamespace(account_public_key=f"node{i}") for i in range(5)],
        network={"chain_id": "unit"},
    )
    broadcast = AsyncMock(return_value={"response": {"result": {"hash": "proof"}}})
    helper = type("Helper", (), {"broadcast_duplicate_vote_evidence": broadcast})
    monkeypatch.setattr(
        sync, "protocol_module", lambda: SimpleNamespace(ProtocolSafetyRunner=helper)
    )
    monkeypatch.setattr(sync, "validator_selection", AsyncMock(return_value=accounts))
    monkeypatch.setattr(sync, "height", AsyncMock(return_value=100))
    monkeypatch.setattr(sync, "find_evidence_block", AsyncMock(return_value=110))
    monkeypatch.setattr(sync, "wait_height", AsyncMock())

    async def rpc(session, node, method, **kwargs):
        if method == "block_results":
            return {"txs_results": [], "height": str(kwargs["height"])}
        return {"block": {"header": {"last_results_hash": "same", "height": str(kwargs["height"])}}}

    monkeypatch.setattr(sync, "rpc", rpc)
    monkeypatch.setattr(sync, "agreement", AsyncMock(return_value={"ok": True}))
    recovery = AsyncMock(return_value={"restored_active": accounts})
    monkeypatch.setattr(sync, "restore_evidence_validator", recovery)
    return runner, broadcast, recovery


def test_old_evidence_uses_prebackup_height_and_checks_all_six_results(historical_context):
    runner, broadcast, recovery = historical_context
    proof = asyncio.run(sync.historical_evidence(runner, object(), object(), 90))
    assert broadcast.await_args.kwargs["evidence_height"] == 89
    assert broadcast.await_args.kwargs["target_node_index"] == 4
    assert proof["infraction_height"] == 89 and len(proof["exact_results"]) == 6
    assert len(proof["next_headers"]) == 6 and recovery.await_count == 1
    assert {header["height"] for header in proof["next_headers"]} == {"111"}


def test_evidence_result_divergence_fails_before_validator_rehabilitation(
    historical_context, monkeypatch
):
    runner, broadcast, recovery = historical_context
    results = iter([{"result": 1}] * 5 + [{"result": 2}])
    monkeypatch.setattr(sync, "rpc", AsyncMock(side_effect=lambda *args, **kwargs: next(results)))
    with pytest.raises(Exception, match="block results differ"):
        asyncio.run(sync.historical_evidence(runner, object(), object(), 90))
    recovery.assert_not_called()


def test_evidence_result_hash_divergence_is_not_hidden_by_equal_app_state(
    historical_context, monkeypatch
):
    runner, broadcast, recovery = historical_context
    values = iter(
        [{"same": 1}] * 6
        + [{"block": {"header": {"last_results_hash": "A"}}}] * 5
        + [{"block": {"header": {"last_results_hash": "B"}}}]
    )
    monkeypatch.setattr(sync, "rpc", AsyncMock(side_effect=lambda *args, **kwargs: next(values)))
    with pytest.raises(Exception, match="LastResultsHash differs"):
        asyncio.run(sync.historical_evidence(runner, object(), object(), 90))
    recovery.assert_not_called()


def test_snapshot_export_uses_public_commands_and_only_key_file_arguments(tmp_path, monkeypatch):
    runner = SimpleNamespace(output_dir=tmp_path, network={"chain_id": "unit-chain"})
    backup = tmp_path / "backup"
    backup.mkdir()
    profiles = []
    monkeypatch.setattr(sync, "write_snapshot_profiles", lambda *args: profiles.append(args))
    calls = []

    async def export(runner, base, *args, **kwargs):
        calls.append(args)
        path = Path(args[args.index("--manifest-output") + 1])
        path.write_text(
            json.dumps({"height": 90, "manifest_version": 2, "snapshot_kind": "full_node_store"})
        )
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(sync, "snapshot_cli", export)
    monkeypatch.setattr(sync, "negative_manifests", lambda *args: {"bad": "url"})
    manifest_path, manifest, negatives = asyncio.run(
        sync.snapshot_export(
            runner, tmp_path / "source", tmp_path / "target", "current-image", backup
        )
    )
    key = backup / "publisher.key"
    assert key.stat().st_mode & 0o077 == 0 and len(key.read_text()) == 64
    assert key.read_text() not in [str(item) for item in calls[0]]
    assert calls[0][:2] == ("export", "snapshot-source")
    assert manifest_path == backup / "manifest.json" and manifest["height"] == 90
    assert profiles[0][3] == "current-image" and len(profiles[0][4]) == 64
    assert negatives == {"bad": "url"}


def test_restore_rejection_for_wrong_reason_cannot_count_as_integrity_coverage(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        sync,
        "snapshot_cli",
        AsyncMock(
            return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="Docker daemon not available"
            )
        ),
    )
    with pytest.raises(Exception, match="unrelated reason"):
        asyncio.run(
            sync.rejected_restore(
                SimpleNamespace(output_dir=tmp_path), tmp_path, "file:///bad.json", "wrong-chain"
            )
        )


def test_protocol_helper_loads_current_source_once_and_exposes_anchored_height():
    import inspect

    module = sync.protocol_module()
    assert module is sync.protocol_module()
    assert (
        "evidence_height"
        in inspect.signature(
            module.ProtocolSafetyRunner.broadcast_duplicate_vote_evidence
        ).parameters
    )


def test_validator_selection_uses_committed_primary_simulation_client():
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call(self, *args):
            assert args == ("validators", "get_active_validators", {})
            return [{"account": "node0", "power": 10}]

    runner = SimpleNamespace(validator_wallets=["wallet"], client=lambda *args: Client())
    assert asyncio.run(sync.validator_selection(runner, object())) == [
        {"account": "node0", "power": 10}
    ]


def test_selection_timeout_does_not_report_rehabilitated_validator(monkeypatch):
    ticks = iter([0, 91])
    monkeypatch.setattr(sync, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(RuntimeError, match="did not recover"):
        asyncio.run(sync.wait_selection(object(), object(), [{"account": "node4", "power": 10}]))


def test_public_restore_executes_every_negative_control_before_valid_restore(tmp_path, monkeypatch):
    for relative in sync.IDENTITY_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("recipient " + relative)
    checks = AsyncMock(side_effect=[{"case": "bad1"}, {"case": "bad2"}])
    monkeypatch.setattr(sync, "rejected_restore", checks)
    cli = AsyncMock()
    monkeypatch.setattr(sync, "snapshot_cli", cli)
    result = asyncio.run(
        sync.restore_snapshot(
            SimpleNamespace(output_dir=tmp_path),
            tmp_path,
            tmp_path / "good.json",
            {"bad1": "url1", "bad2": "url2"},
        )
    )
    assert result == [{"case": "bad1"}, {"case": "bad2"}]
    assert checks.await_count == 2 and cli.await_count == 1


def test_backup_source_stops_comet_before_supervisor_closes_application(monkeypatch):
    calls = AsyncMock(
        side_effect=["", "0", json.dumps([{"State": {"Running": False, "OOMKilled": False}}])]
    )
    monkeypatch.setattr(sync, "command", calls)
    asyncio.run(sync.stop_backup_source("owned-replay"))
    assert calls.await_args_list[0].args == (
        "docker",
        "exec",
        "owned-replay",
        "pkill",
        "-TERM",
        "-x",
        "cometbft",
    )
    assert calls.await_args_list[1].args == ("docker", "wait", "owned-replay")


@pytest.mark.parametrize(
    "state", [{"Running": True, "OOMKilled": False}, {"Running": False, "OOMKilled": True}]
)
def test_backup_source_must_be_stopped_without_oom(monkeypatch, state):
    monkeypatch.setattr(
        sync, "command", AsyncMock(side_effect=["", "0", json.dumps([{"State": state}])])
    )
    with pytest.raises(Exception, match="did not stop cleanly"):
        asyncio.run(sync.stop_backup_source("owned-replay"))


def test_retention_target_must_increase_even_when_effective_duration_grows(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_state(self, *args):
            return 0

    runner = SimpleNamespace(
        nodes=[object()], validator_wallets=[object()], client=lambda *args: Client()
    )
    policies = [
        {"target_max_age_duration": 8 * sync.DAY_NS, "max_age_duration": 8 * sync.DAY_NS},
        {"target_max_age_duration": 8 * sync.DAY_NS, "max_age_duration": 8 * sync.DAY_NS + 1},
    ]
    monkeypatch.setattr(sync, "query", AsyncMock(side_effect=policies))
    monkeypatch.setattr(sync, "governance_vote", AsyncMock(return_value={}))
    monkeypatch.setattr(sync, "collect_policy_proof", AsyncMock(return_value={}))
    with pytest.raises(Exception, match="target did not increase"):
        asyncio.run(sync.retention_rehearsal(runner, object()))


def test_restart_accepts_unchanged_effective_duration_at_retention_target(monkeypatch):
    policy = {"target_max_age_duration": 10, "max_age_duration": 10}
    monkeypatch.setattr(sync, "query", AsyncMock(return_value=policy))
    monkeypatch.setattr(sync, "command", AsyncMock())
    monkeypatch.setattr(sync, "observer_address", AsyncMock(return_value=object()))
    monkeypatch.setattr(sync, "observer_agreement", AsyncMock(return_value={}))
    monkeypatch.setattr(sync, "collect_policy_proof", AsyncMock(return_value={}))
    result = asyncio.run(
        sync.repeated_restart(object(), object(), "own-container", object(), {"target_after": 10})
    )
    assert len(result) == 2


@pytest.mark.parametrize(
    "manifest",
    [
        {"manifest_version": 1, "snapshot_kind": "full_node_store"},
        {"manifest_version": 2, "snapshot_kind": "application_only"},
    ],
)
def test_snapshot_export_requires_both_fullstore_kind_and_v2(tmp_path, monkeypatch, manifest):
    runner = SimpleNamespace(output_dir=tmp_path, network={"chain_id": "unit-chain"})
    backup = tmp_path / "backup"
    backup.mkdir()
    monkeypatch.setattr(sync, "write_snapshot_profiles", lambda *args: None)

    async def export(runner, base, *args, **kwargs):
        path = Path(args[args.index("--manifest-output") + 1])
        path.write_text(json.dumps(manifest))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(sync, "snapshot_cli", export)
    with pytest.raises(Exception, match="did not export a verified"):
        asyncio.run(
            sync.snapshot_export(runner, tmp_path / "source", tmp_path / "target", "image", backup)
        )


def test_validator_recovery_does_not_assume_missing_pending_job_diagnostics_mean_zero(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        call = AsyncMock(side_effect=[{"jailed": True}, {"jailed": True, "pending_slash_count": 0}])

    runner = SimpleNamespace(validator_wallets=[object()], client=lambda *args: Client())
    monkeypatch.setattr(sync.asyncio, "sleep", AsyncMock())
    value = asyncio.run(sync.wait_validator_ready(runner, object(), "node4"))
    assert "pending_slash_count" in value and Client.call.await_count == 2


def test_evidence_result_object_key_order_does_not_count_as_consensus_divergence(
    historical_context, monkeypatch
):
    runner, broadcast, recovery = historical_context
    first = {"alpha": 1, "beta": 2}
    second = {"beta": 2, "alpha": 1}
    values = iter([first, second] * 3 + [{"block": {"header": {"last_results_hash": "same"}}}] * 6)
    monkeypatch.setattr(sync, "rpc", AsyncMock(side_effect=lambda *args, **kwargs: next(values)))
    proof = asyncio.run(sync.historical_evidence(runner, object(), object(), 90))
    assert proof["included_height"] == 110 and recovery.await_count == 1


def test_native_validator_selection_must_match_restored_app_powers(monkeypatch):
    account = "01" * 32
    address = sync.hashlib.sha256(bytes.fromhex(account)).hexdigest()[:40].upper()
    monkeypatch.setattr(
        sync,
        "rpc",
        AsyncMock(return_value={"validators": [{"address": address, "voting_power": "11"}]}),
    )
    with pytest.raises(Exception, match="native validator powers"):
        asyncio.run(
            sync.verify_native_selection(
                object(),
                [object()],
                [{"account": account, "power": 10}],
                {"checks": [{"height": 40}]},
            )
        )
    sync.rpc.return_value = {"validators": [{"address": address, "voting_power": "10"}]}
    assert asyncio.run(
        sync.verify_native_selection(
            object(), [object()], [{"account": account, "power": 10}], {"checks": [{"height": 40}]}
        )
    ) == [{address: 10}]


def test_evidence_scan_skips_an_item_missing_its_vote_height(monkeypatch):
    monkeypatch.setattr(sync, "height", AsyncMock(side_effect=[10, 11]))
    blocks = [
        {"block": {"evidence": {"evidence": [{"value": {"vote_a": {}}}]}}},
        {"block": {"evidence": {"evidence": [{"value": {"vote_a": {"height": "1"}}}]}}},
    ]
    monkeypatch.setattr(sync, "rpc", AsyncMock(side_effect=blocks))
    monkeypatch.setattr(sync.asyncio, "sleep", AsyncMock())
    assert asyncio.run(sync.find_evidence_block(object(), object(), 10, 1)) == 11


def public_patch_source(tmp_path):
    source = tmp_path / "source"
    (source / "config/state-patches").mkdir(parents=True)
    (source / "config/config.toml").write_text(
        'proxy_app = "old"\n[p2p]\npersistent_peers = ""\n[statesync]\nenable = false\n'
    )
    (source / "config/genesis.json").write_text('{"chain_id":"bundle-replay-test"}')
    (source / "config/xian.toml").write_text("bds_enabled = false\n")
    (source / "config/node_key.json").write_text("do not copy source identity")
    payload = {
        "version": 1,
        "patch_id": "historical-replay-patch",
        "activation_height": 20,
        "governance_contract": "governance",
        "summary": "Replay needs the historical public bundle",
        "uri": "local://bundle-replay-test",
        "chain_id": "bundle-replay-test",
        "changes": [{"key": "con_fixture.mode", "value": "patched", "comment": "historical"}],
    }
    path = source / "config/state-patches/historical.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return source, path


@pytest.mark.parametrize("role", ["replay", "full_node_store"])
def test_observers_receive_the_public_inventory_needed_to_execute_historical_patch(
    tmp_path, monkeypatch, role
):
    from xian.utils.state_patches import ScheduledPatchRecord, StatePatchManager

    source, bundle_file = public_patch_source(tmp_path)
    target = tmp_path / role
    monkeypatch.setattr(sync, "command", AsyncMock(return_value=""))
    inventory = asyncio.run(sync.prepare_home(source, target, "image", "peers"))
    manager = StatePatchManager(None, chain_id="bundle-replay-test")
    manager.load_patches(target / "config/state-patches")
    source_manager = StatePatchManager(None, chain_id="bundle-replay-test")
    source_manager.load_patches(source / "config/state-patches")
    bundle = source_manager.local_bundles["historical-replay-patch"]
    record = ScheduledPatchRecord(
        patch_id=bundle.patch_id,
        proposal_id=1,
        bundle_hash=bundle.bundle_hash,
        activation_height=20,
        governance_contract="governance",
        summary=bundle.summary,
        uri=bundle.uri,
        emergency=False,
        status="approved",
    )
    monkeypatch.setattr(manager, "_read_scheduled_patch_records", lambda **kwargs: [record])
    executions = manager._build_executions(height=20, include_applied=False)
    assert len(executions) == 1
    assert executions[0].applied_changes[0]["value"] == "patched"
    assert (
        target / "config/state-patches/historical.json"
    ).read_bytes() == bundle_file.read_bytes()
    assert inventory[0]["filename"] == "historical.json"
    assert inventory[0]["sha256"] == sync.hashlib.sha256(bundle_file.read_bytes()).hexdigest()
    assert not (target / "config/node_key.json").exists()


@pytest.mark.parametrize("kind", ["directory-link", "file-link", "non-regular", "identity-json"])
def test_public_bundle_copy_refuses_links_nonfiles_and_identity_names(tmp_path, kind):
    source, bundle = public_patch_source(tmp_path)
    directory = bundle.parent
    if kind == "directory-link":
        renamed = source / "config/real-patches"
        directory.rename(renamed)
        directory.symlink_to(renamed, target_is_directory=True)
    elif kind == "file-link":
        external = tmp_path / "external.json"
        bundle.rename(external)
        bundle.symlink_to(external)
    elif kind == "non-regular":
        (directory / "nested.json").mkdir()
    else:
        (directory / "node_key.json").write_bytes(bundle.read_bytes())
    home = tmp_path / "observer"
    (home / "config").mkdir(parents=True)
    with pytest.raises(Exception, match="link|regular|identity"):
        sync.copy_public_state_patch_inventory(source, home)
    assert not (home / "config/state-patches").exists()


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"chain_id": "different-chain"}, "another chain"),
        ({"bundle_hash": "incorrect"}, "incorrect hash"),
        ({"private_key": "unrelated operator key"}, "unrelated metadata"),
        ({"version": 99}, "unsupported"),
    ],
)
def test_public_bundle_copy_uses_canonical_validation_and_rejects_unrelated_metadata(
    tmp_path, changes, reason
):
    source, bundle = public_patch_source(tmp_path)
    payload = json.loads(bundle.read_text())
    bundle.write_text(json.dumps({**payload, **changes}))
    home = tmp_path / "observer"
    (home / "config").mkdir(parents=True)
    with pytest.raises(Exception, match=reason):
        sync.copy_public_state_patch_inventory(source, home)
    assert not (home / "config/state-patches").exists()


def test_public_bundle_copy_ignores_non_json_notes_without_copying_them(tmp_path):
    source, bundle = public_patch_source(tmp_path)
    (bundle.parent / "operator-notes.txt").write_text("not a public runtime bundle")
    home = tmp_path / "observer"
    (home / "config").mkdir(parents=True)
    inventory = sync.copy_public_state_patch_inventory(source, home)
    assert [item["filename"] for item in inventory] == ["historical.json"]
    assert not (home / "config/state-patches/operator-notes.txt").exists()


def test_public_bundle_copy_detects_source_change_and_removes_partial_destination(
    tmp_path, monkeypatch
):
    source, bundle = public_patch_source(tmp_path)
    original = sync.validated_public_state_patch_inventory

    def changing(directory, chain_id):
        result = original(directory, chain_id)
        if directory == bundle.parent:
            payload = json.loads(bundle.read_text())
            payload["summary"] = "changed after validation"
            bundle.write_text(json.dumps(payload))
        return result

    monkeypatch.setattr(sync, "validated_public_state_patch_inventory", changing)
    home = tmp_path / "observer"
    (home / "config").mkdir(parents=True)
    with pytest.raises(Exception, match="changed while copying"):
        sync.copy_public_state_patch_inventory(source, home)
    assert not (home / "config/state-patches").exists()


def test_public_bundle_destination_is_revalidated_before_it_is_accepted(tmp_path, monkeypatch):
    source, bundle = public_patch_source(tmp_path)
    home = tmp_path / "observer"
    (home / "config").mkdir(parents=True)
    original = sync.validated_public_state_patch_inventory

    def changed_destination(directory, chain_id):
        result = original(directory, chain_id)
        if directory == home / "config/state-patches":
            result[0]["sha256"] = "changed destination"
        return result

    monkeypatch.setattr(sync, "validated_public_state_patch_inventory", changed_destination)
    with pytest.raises(Exception, match="does not match"):
        sync.copy_public_state_patch_inventory(source, home)
    assert not (home / "config/state-patches").exists()


def test_missing_historical_inventory_prevents_starting_either_observer(tmp_path, monkeypatch):
    runner = SimpleNamespace(output_dir=tmp_path, run_id="unit")
    monkeypatch.setattr(sync, "prepare_home", AsyncMock(return_value=[]))
    start = AsyncMock()
    monkeypatch.setattr(sync, "create_observer", start)
    with pytest.raises(Exception, match="historical public state-patch inventories"):
        asyncio.run(
            sync.full_store_rehearsal(runner, object(), "image", "network", tmp_path, "peers")
        )
    start.assert_not_called()


def test_public_stage_export_includes_explicit_public_bundle_hashes_for_both_roles(tmp_path):
    import export_localnet_public_proof as public

    fields = json.loads((sync.STACK / "scripts/localnet_public_fields.json").read_text())["e2e"][
        "21-fresh-node-sync"
    ]
    inventory = [{"filename": "historical.json", "sha256": "a" * 64}]
    phase = {
        "name": "21-fresh-node-sync",
        "ok": True,
        "started_at": "start",
        "ended_at": "end",
        "details": {
            "public_state_patch_bundles": {"replay": inventory, "full_node_store": inventory}
        },
    }
    exported = public.phase_payload(phase, phase["name"], fields, "e2e")
    assert (
        exported["details"]["public_state_patch_bundles"]
        == phase["details"]["public_state_patch_bundles"]
    )


def test_observer_inventory_must_match_between_replay_and_restore_roles(tmp_path, monkeypatch):
    runner = SimpleNamespace(output_dir=tmp_path, run_id="unit")
    monkeypatch.setattr(
        sync,
        "prepare_home",
        AsyncMock(
            side_effect=[
                [{"filename": "patch.json", "sha256": "first"}],
                [{"filename": "patch.json", "sha256": "different"}],
            ]
        ),
    )
    start = AsyncMock()
    monkeypatch.setattr(sync, "create_observer", start)
    with pytest.raises(Exception, match="matching historical"):
        asyncio.run(
            sync.full_store_rehearsal(runner, object(), "image", "network", tmp_path, "peers")
        )
    start.assert_not_called()


def test_public_bundle_copy_accepts_correct_declared_hash_and_optional_unscoped_bundle(tmp_path):
    from xian.utils.state_patches import StatePatchManager

    source, bundle = public_patch_source(tmp_path)
    payload = json.loads(bundle.read_text())
    payload.pop("chain_id")
    bundle.write_text(json.dumps(payload))
    manager = StatePatchManager(None, chain_id="bundle-replay-test")
    manager.load_patches(bundle.parent)
    payload["bundle_hash"] = manager.local_bundles[payload["patch_id"]].bundle_hash
    bundle.write_text(json.dumps(payload))
    home = tmp_path / "observer"
    (home / "config").mkdir(parents=True)
    inventory = sync.copy_public_state_patch_inventory(source, home)
    assert inventory[0]["bundle_hash"] == payload["bundle_hash"]
    assert inventory[0]["sha256"] == sync.hashlib.sha256(bundle.read_bytes()).hexdigest()


def test_state_patch_inventory_path_cannot_be_an_unrelated_regular_config_file(tmp_path):
    source = tmp_path / "source"
    (source / "config").mkdir(parents=True)
    (source / "config/genesis.json").write_text('{"chain_id":"test"}')
    (source / "config/state-patches").write_text("unrelated config")
    home = tmp_path / "observer"
    (home / "config").mkdir(parents=True)
    with pytest.raises(Exception, match="directory"):
        sync.copy_public_state_patch_inventory(source, home)
