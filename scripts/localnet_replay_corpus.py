"""Export a public localnet block corpus and verify it on a native node image.

The corpus contains CometBFT block/state databases and public configuration,
never application LMDB or validator private keys. Verification executes every
block again and compares the resulting state root and transaction outputs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from localnet_chain_checks import (
    OBSERVER_COMMAND,
    STACK,
    command,
    config_hashes,
    require,
    rpc,
    wait_height,
    write_config,
)
from localnet_replay_capture import result_digest
from localnet_sync_checks import set_config

REPOS = ("xian-stack", "xian-abci", "xian-contracting", "xian-py", "xian-configs")


async def export_corpus(runner, session):
    node = runner.nodes[4]
    output = runner.output_dir / "replay-corpus"
    output.mkdir(exist_ok=True)
    home = STACK / ".localnet" / node.moniker / ".cometbft"
    await runner.stop_node_runtime(node)
    try:
        latest = json.loads((home / "xian/__latest_block.json").read_text())
        with tempfile.TemporaryDirectory(dir=output) as directory:
            staging = Path(directory) / "home"
            shutil.copytree(
                home / "config",
                staging / "config",
                ignore=shutil.ignore_patterns(
                    "priv_validator_key.json",
                    "node_key.json",
                ),
            )
            shutil.copytree(
                home / "data",
                staging / "data",
                ignore=shutil.ignore_patterns(
                    "priv_validator_state.json",
                    "cs.wal",
                ),
            )
            config = staging / "config/config.toml"
            write_config(
                config,
                set_config(
                    config.read_text(),
                    "p2p",
                    {
                        "persistent_peers": "",
                        "seeds": "",
                        "pex": False,
                    },
                ),
            )
            write_config(
                config,
                re.sub(
                    r"^proxy_app\s*=.*$",
                    'proxy_app = "unix:///tmp/abci.sock"',
                    config.read_text(),
                    flags=re.MULTILINE,
                ),
            )
            runtime = staging / "config/xian.toml"
            text = re.sub(
                r"^bds_enabled\s*=.*$",
                "bds_enabled = false",
                runtime.read_text(),
                flags=re.MULTILINE,
            )
            write_config(runtime, text)
            with tarfile.open(output / "blocks.tar.gz", "w:gz") as archive:
                archive.add(staging, arcname="home")
    finally:
        await runner.start_node_runtime(session, node)
    expected = {}
    for height in range(1, int(latest["height"]) + 1):
        block = await rpc(session, runner.nodes[0], "block_results", height=height)
        expected[str(height)] = result_digest(block.get("txs_results"))
    expected_roots = {}
    await wait_height(session, runner.nodes[0], int(latest["height"]) + 1)
    for start in range(2, int(latest["height"]) + 2, 20):
        metadata = await rpc(
            session,
            runner.nodes[0],
            "blockchain",
            minHeight=start,
            maxHeight=min(start + 19, int(latest["height"]) + 1),
        )
        for block in metadata["block_metas"]:
            header = block["header"]
            expected_roots[str(int(header["height"]) - 1)] = header["app_hash"].lower()
    require(set(expected_roots) == set(expected), "Missing source block application roots")
    revisions = {}
    dirty_repositories = []
    for repo in REPOS:
        path = STACK.parent / repo
        revisions[repo] = await command("git", "-C", path, "rev-parse", "HEAD")
        if await command("git", "-C", path, "status", "--porcelain"):
            dirty_repositories.append(repo)
    manifest = {
        "height": latest["height"],
        "app_hash": latest["hash"].lower(),
        "chain_id": runner.network["chain_id"],
        "expected_results": expected,
        "expected_app_hashes": expected_roots,
        "repositories": revisions,
        "dirty_repositories": dirty_repositories,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return output


async def verify_corpus(corpus, image, output):
    manifest = json.loads((corpus / "manifest.json").read_text())
    require(not output.exists(), f"Replay output already exists: {output}")
    output.mkdir(parents=True)
    with tarfile.open(corpus / "blocks.tar.gz") as archive:
        archive.extractall(output, filter="data")
    home = output / "home"
    require(not (home / "xian").exists(), "Replay corpus must not contain application state")
    require(
        not (home / "config/priv_validator_key.json").exists(),
        "Replay corpus must not contain a validator private key",
    )
    await command(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "cometbft",
        "--volume",
        f"{home}:/root/.cometbft",
        image,
        "init",
    )
    hooks = output / "hooks"
    hooks.mkdir()
    shutil.copy2(STACK / "scripts/e2e_replay_hooks/sitecustomize.py", hooks / "sitecustomize.py")
    shutil.copy2(STACK / "scripts/localnet_replay_capture.py", hooks / "localnet_replay_capture.py")
    expected_files = json.loads(config_hashes(home))
    for path in (home / "data").rglob("*"):
        if path.is_file():
            expected_files[str(path.relative_to(home))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    for path in hooks.iterdir():
        expected_files[f"/e2e-hooks/{path.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    container = "xian-e2e-replay-" + str(time.time_ns())
    try:
        await command(
            "docker",
            "run",
            "--detach",
            "--entrypoint",
            "/bin/bash",
            "--name",
            container,
            "--publish",
            "127.0.0.1::26657",
            "--memory",
            "1536m",
            "--volume",
            f"{home}:/root/.cometbft",
            "--volume",
            f"{hooks}:/e2e-hooks:ro",
            "--env",
            "PYTHONPATH=/e2e-hooks",
            "--env",
            f"XIAN_E2E_CONFIG_HASHES={json.dumps(expected_files, sort_keys=True)}",
            image,
            "-c",
            OBSERVER_COMMAND,
        )
        info = json.loads(await command("docker", "inspect", container))[0]
        port = info["NetworkSettings"]["Ports"]["26657/tcp"][0]["HostPort"]
        node = SimpleNamespace(moniker="offline-replay", rpc_url=f"http://127.0.0.1:{port}")
        deadline = time.monotonic() + 600
        final = None
        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline:
                try:
                    final = (await rpc(session, node, "abci_info"))["response"]
                    if int(final["last_block_height"]) == int(manifest["height"]):
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
        require(
            final is not None and int(final["last_block_height"]) == int(manifest["height"]),
            "Offline block replay did not reach the corpus height",
        )
        require(
            base64.b64decode(final["last_block_app_hash"]).hex() == manifest["app_hash"],
            "Offline replay application root differs from the source network",
        )
        records = [
            json.loads(line) for line in (home / "replay-results.jsonl").read_text().splitlines()
        ]
        actual = {str(record["height"]): record["digest"] for record in records}
        require(len(actual) == len(records), "A replayed height was executed more than once")
        require(
            actual == manifest["expected_results"],
            "Re-executed transaction outcomes, fees, or events differ from source blocks",
        )
        actual_roots = {str(record["height"]): record["app_hash"] for record in records}
        require(
            actual_roots == manifest["expected_app_hashes"],
            "Replayed block application roots differ",
        )
        architecture = await command("docker", "exec", container, "uname", "-m")
        result = {
            "ok": True,
            "height": manifest["height"],
            "app_hash": manifest["app_hash"],
            "compared_blocks": len(actual),
            "architecture": architecture,
            "image": info["Image"],
            "dirty_source_repositories": manifest.get("dirty_repositories", []),
        }
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        logs = await command("docker", "logs", container, combine_output=True)
        (output / "container.log").write_text(logs)
        await command("docker", "rm", "-f", "-v", container)


async def corpus_phase(runner, session):
    corpus = await export_corpus(runner, session)
    info = json.loads(await command("docker", "inspect", runner.nodes[0].abci_container))[0]
    result = await verify_corpus(corpus, info["Image"], runner.output_dir / "native-replay")
    return {"corpus": str(corpus), "replay": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(verify_corpus(args.corpus.resolve(), args.image, args.output.resolve())),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
