"""Capture the same private replay format from the five- or three-node harness."""

import asyncio
import importlib
import json
import sys
import tarfile
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
        'proxy_app = "tcp://app:26658"\n[p2p]\npersistent_peers="private-peer"\nseeds="seed"\npex=true\n'
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
