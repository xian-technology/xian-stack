"""Capture the same private replay format from the five- or three-node harness."""

import asyncio
import base64
import importlib
import json
import sys
import tarfile
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))


def export_fixture(tmp_path, monkeypatch):
    module = importlib.import_module("localnet_replay_corpus")
    home = tmp_path / "owned-home"
    (home / "config").mkdir(parents=True)
    (home / "data/blockstore.db").mkdir(parents=True)
    (home / "data/state.db").mkdir()
    (home / "data/cs.wal").mkdir()
    (home / "config/config.toml").write_text(
        'proxy_app = "tcp://app:26658"\n[p2p]\npersistent_peers="private-peer"\nseeds="seed"\n'
        'pex=true\nexternal_address="xian-idle-unbond-private-node-0:26656"\n'
    )
    (home / "config/xian.toml").write_text("bds_enabled = true\n")
    (home / "config/genesis.json").write_text('{"chain_id":"clock-preflight"}')
    (home / "config/priv_validator_key.json").write_text("VALIDATOR_PRIVATE_SENTINEL")
    (home / "config/node_key.json").write_text("NODE_PRIVATE_SENTINEL")
    (home / "data/priv_validator_state.json").write_text("SIGNING_HIGHWATER_SENTINEL")
    (home / "data/cs.wal/wal").write_text("PRIVATE_WAL_SENTINEL")
    (home / "data/blockstore.db/CURRENT").write_bytes(b"native-blocks")
    (home / "data/state.db/CURRENT").write_bytes(b"native-consensus")
    nodes = [SimpleNamespace(moniker=f"node-{i}", abci_container=f"owned-{i}") for i in range(3)]
    runner = SimpleNamespace(
        nodes=nodes,
        output_dir=tmp_path / "private-output",
        network={"chain_id": "clock-preflight"},
        stop_node_runtime=AsyncMock(),
        start_node_runtime=AsyncMock(),
    )
    runner.output_dir.mkdir()
    captured = []

    async def command(*args):
        captured.append(args)
        if args[:2] == ("docker", "cp"):
            assert runner.stop_node_runtime.await_count == 1
            assert runner.start_node_runtime.await_count == 0
            Path(args[3]).write_text(json.dumps({"height": 2, "hash": "b" * 64}))
            return ""
        if args[:2] == ("docker", "inspect"):
            return json.dumps([{"Image": "sha256:" + "f" * 64}])
        assert args[:2] == ("git", "-C")
        return "1" * 40 if args[3:] == ("rev-parse", "HEAD") else ""

    async def rpc(session, node, method, **kwargs):
        if method == "block_results":
            return {"txs_results": [{"code": 0, "data": "public-result"}]}
        assert method == "blockchain" and kwargs == {"minHeight": 2, "maxHeight": 3}
        return {
            "block_metas": [
                {"header": {"height": "2", "app_hash": "a" * 64}},
                {"header": {"height": "3", "app_hash": "b" * 64}},
            ]
        }

    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module, "rpc", rpc)
    monkeypatch.setattr(module, "wait_height", AsyncMock())
    monkeypatch.setattr(
        module,
        "inspect_replay_image",
        AsyncMock(
            return_value={"image_id": "sha256:" + "f" * 64, "execution_policy": {"mode": "v2"}}
        ),
    )
    return module, runner, home, captured


def assert_private_corpus(module, runner, home, output):
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["height"] == 2 and manifest["app_hash"] == "b" * 64
    assert manifest["expected_app_hashes"] == {"1": "a" * 64, "2": "b" * 64}
    assert set(manifest["expected_results"]) == {"1", "2"}
    assert manifest["dirty_repositories"] == []
    assert set(manifest["repositories"]) == set(module.REPOS)
    with tarfile.open(output / "blocks.tar.gz") as archive:
        names = archive.getnames()
        assert not any(
            "priv_validator" in name or "node_key" in name or "cs.wal" in name for name in names
        )
        assert archive.extractfile("home/data/blockstore.db/CURRENT").read() == b"native-blocks"
        assert archive.extractfile("home/data/state.db/CURRENT").read() == b"native-consensus"
        config = archive.extractfile("home/config/config.toml").read().decode()
        assert 'proxy_app = "unix:///tmp/abci.sock"' in config
        assert (
            'persistent_peers = ""' in config and 'seeds = ""' in config and "pex = false" in config
        )
        assert tomllib.loads(config)["p2p"]["external_address"] == ""
        assert archive.extractfile("home/config/xian.toml").read() == b"bds_enabled = false\n"
    assert (home / "config/priv_validator_key.json").read_text() == "VALIDATOR_PRIVATE_SENTINEL"
    runner.start_node_runtime.assert_awaited_once()
    assert module.wait_height.await_args.args[-1] == 3


def test_three_node_preflight_exports_explicit_owned_jailed_donor(tmp_path, monkeypatch):
    module, runner, home, commands = export_fixture(tmp_path, monkeypatch)
    donor = runner.nodes[0]
    output = asyncio.run(module.export_corpus(runner, object(), node=donor, home=home))
    assert_private_corpus(module, runner, home, output)
    runner.stop_node_runtime.assert_awaited_once_with(donor)
    assert any(
        args[:3] == ("docker", "cp", "owned-0:/root/.cometbft/xian/__latest_block.json")
        for args in commands
    )


def test_five_node_default_keeps_the_last_validator_and_canonical_home(tmp_path, monkeypatch):
    module, runner, home, _ = export_fixture(tmp_path, monkeypatch)
    runner.nodes.extend(
        SimpleNamespace(moniker=f"node-{i}", abci_container=f"owned-{i}") for i in (3, 4)
    )
    canonical = tmp_path / ".localnet/node-4/.cometbft"
    canonical.parent.mkdir(parents=True)
    home.rename(canonical)
    monkeypatch.setattr(module, "STACK", tmp_path)
    output = asyncio.run(module.export_corpus(runner, object()))
    assert_private_corpus(module, runner, canonical, output)
    runner.stop_node_runtime.assert_awaited_once_with(runner.nodes[4])


def test_failed_capture_restarts_the_same_owned_donor_before_raising(tmp_path, monkeypatch):
    module, runner, home, _ = export_fixture(tmp_path, monkeypatch)
    (home / "data").rename(home / "missing-data")
    donor = runner.nodes[0]
    with pytest.raises(FileNotFoundError):
        asyncio.run(module.export_corpus(runner, object(), node=donor, home=home))
    runner.stop_node_runtime.assert_awaited_once_with(donor)
    runner.start_node_runtime.assert_awaited_once()
    assert runner.start_node_runtime.await_args.args[-1] is donor
    assert not (runner.output_dir / "replay-corpus/manifest.json").exists()


def test_capture_records_dirty_source_identity_without_rewriting_the_ledger(tmp_path, monkeypatch):
    module, runner, home, _ = export_fixture(tmp_path, monkeypatch)
    original = module.command

    async def dirty_status(*args):
        if (
            args[0] == "git"
            and args[3:] == ("status", "--porcelain")
            and Path(args[2]).name == "xian-stack"
        ):
            return " M scripts/localnet_replay_corpus.py"
        return await original(*args)

    monkeypatch.setattr(module, "command", dirty_status)
    output = asyncio.run(module.export_corpus(runner, object(), node=runner.nodes[0], home=home))
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["dirty_repositories"] == ["xian-stack"]
    assert manifest["expected_app_hashes"] == {"1": "a" * 64, "2": "b" * 64}
    runner.start_node_runtime.assert_awaited_once()


def test_capture_can_resume_an_empty_output_directory_left_by_an_interrupted_copy(
    tmp_path, monkeypatch
):
    module, runner, home, _ = export_fixture(tmp_path, monkeypatch)
    (runner.output_dir / "replay-corpus").mkdir()
    output = asyncio.run(module.export_corpus(runner, object(), node=runner.nodes[0], home=home))
    assert_private_corpus(module, runner, home, output)


def verification_fixture(tmp_path, monkeypatch, *, exited=False):
    module = importlib.import_module("localnet_replay_corpus")
    source = tmp_path / "source/home"
    (source / "config").mkdir(parents=True)
    (source / "data/blockstore.db").mkdir(parents=True)
    config = source / "config/config.toml"
    config.write_text(
        'proxy_app = "unix:///tmp/abci.sock"\n[p2p]\npersistent_peers="private-peer"\n'
        'seeds="private-seed"\npex=true\nexternal_address="xian-idle-unbond-private-node-0:26656"\n'
    )
    if exited:
        config.write_text(
            'proxy_app = "unix:///tmp/abci.sock"\n[p2p]\npersistent_peers=""\n'
            'seeds=""\npex=false\nexternal_address=""\n'
        )
    (source / "config/xian.toml").write_text("bds_enabled = false\n")
    (source / "config/genesis.json").write_text('{"chain_id":"private-corpus"}')
    (source / "data/blockstore.db/CURRENT").write_bytes(b"immutable-native-ledger")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    with tarfile.open(corpus / "blocks.tar.gz", "w:gz") as archive:
        archive.add(source, arcname="home")
    digest = module.result_digest([])
    policy = {
        "execution_revision": "xvm-execution-15",
        "gas_schedule": "xvm-gas-7",
        "bytecode_version": "xvm-2",
        "set_codec": "xian-set-1",
    }
    (corpus / "manifest.json").write_text(
        json.dumps(
            {
                "height": 2,
                "app_hash": "b" * 64,
                "expected_results": {"1": digest, "2": digest},
                "expected_app_hashes": {"1": "a" * 64, "2": "b" * 64},
                "execution_policy": policy,
            }
        )
    )
    output = tmp_path / "output"
    events = []
    inspections = 0

    async def command(*args, **kwargs):
        nonlocal inspections
        events.append(args)
        if args[:2] == ("docker", "run") and args[-1] == "init":
            # The real scratch files must already be offline before stock init
            # or any later container can resolve the captured private hostname.
            scratch = output / "home"
            actual = tomllib.loads((scratch / "config/config.toml").read_text())
            assert actual["p2p"] == {
                "persistent_peers": "",
                "seeds": "",
                "pex": False,
                "external_address": "",
            }
            assert actual["proxy_app"] == "unix:///tmp/abci.sock"
            assert (
                scratch / "data/blockstore.db/CURRENT"
            ).read_bytes() == b"immutable-native-ledger"
            assert (scratch / "config/genesis.json").read_bytes() == (
                source / "config/genesis.json"
            ).read_bytes()
        elif args[:2] == ("docker", "run"):
            records = [
                {"height": height, "digest": digest, "app_hash": letter * 64}
                for height, letter in [(1, "a"), (2, "b")]
            ]
            (output / "home/replay-results.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records)
            )
        elif args[:2] == ("docker", "inspect"):
            inspections += 1
            running = not exited or (exited == "during-poll" and inspections == 1)
            return json.dumps(
                [
                    {
                        "Image": "sha256:review-image",
                        "State": {"Running": running, "ExitCode": 0 if running else 1},
                        "NetworkSettings": {"Ports": {"26657/tcp": [{"HostPort": "45678"}]}},
                    }
                ]
            )
        elif args[:2] == ("docker", "logs"):
            return "private replay diagnostics"
        elif args[:2] == ("docker", "exec"):
            return "aarch64"
        return ""

    rpc = AsyncMock(
        return_value={
            "response": {
                "last_block_height": "2",
                "last_block_app_hash": base64.b64encode(bytes.fromhex("b" * 64)).decode(),
            }
        }
    )
    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module, "rpc", rpc)
    monkeypatch.setattr(
        module, "inspect_replay_image", AsyncMock(return_value={"execution_policy": policy})
    )
    return module, corpus, output, source, events, rpc


def test_old_corpus_private_hostname_is_removed_only_in_disposable_verification_copy(
    tmp_path, monkeypatch
):
    module, corpus, output, source, events, _ = verification_fixture(tmp_path, monkeypatch)
    archive_before = (corpus / "blocks.tar.gz").read_bytes()
    config_before = (source / "config/config.toml").read_bytes()
    result = asyncio.run(module.verify_corpus(corpus, "image", output))
    assert result["ok"] and result["compared_blocks"] == 2
    assert (corpus / "blocks.tar.gz").read_bytes() == archive_before
    assert (source / "config/config.toml").read_bytes() == config_before
    assert any(args[:2] == ("docker", "rm") for args in events)


def test_offline_peer_normalization_is_idempotent_and_preserves_other_configuration():
    module = importlib.import_module("localnet_replay_corpus")
    text = (
        'proxy_app="unix:///tmp/abci.sock"\n[p2p]\nexternal_address="private-node:26656"\n'
        'persistent_peers="old-peer"\nseeds="old-seed"\npex=true\n'
        "max_num_inbound_peers=41\n[consensus]\ncreate_empty_blocks=false\n"
        'create_empty_blocks_interval="7s"\n'
    )
    normalized = module.offline_p2p_config(text)
    parsed = tomllib.loads(normalized)
    assert parsed["p2p"] == {
        "persistent_peers": "",
        "seeds": "",
        "pex": False,
        "external_address": "",
        "max_num_inbound_peers": 41,
    }
    assert parsed["proxy_app"] == "unix:///tmp/abci.sock"
    assert parsed["consensus"] == tomllib.loads(text)["consensus"]
    assert module.offline_p2p_config(normalized) == normalized


@pytest.mark.parametrize("exited", [True, "during-poll"])
def test_exited_replay_fails_before_rpc_retry_and_preserves_private_container_log(
    tmp_path, monkeypatch, exited
):
    from localnet_e2e_support import E2EError

    module, corpus, output, _, events, rpc = verification_fixture(
        tmp_path, monkeypatch, exited=exited
    )
    rpc.side_effect = OSError("container RPC unavailable")
    sleep = AsyncMock(side_effect=AssertionError("Exited replay must not wait before failing"))
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(E2EError, match="replay container exited"):
        asyncio.run(module.verify_corpus(corpus, "image", output))
    rpc.assert_not_awaited()
    sleep.assert_not_awaited()
    assert (output / "container.log").read_text() == "private replay diagnostics"
    assert (output / "container.log").stat().st_mode & 0o777 == 0o600
    assert any(args[:2] == ("docker", "rm") for args in events)
