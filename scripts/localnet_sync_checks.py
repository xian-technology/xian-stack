"""Bootstrap disposable non-validators by block replay and peer state sync."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from types import SimpleNamespace

from localnet_chain_checks import (
    OBSERVER_COMMAND,
    STACK,
    agreement,
    command,
    config_hashes,
    height,
    query,
    require,
    rpc,
    wait_height,
    write_config,
)


def set_config(text, section, updates):
    pattern = rf"(\[{re.escape(section)}\]\n)(.*?)(?=\n\[|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    require(match is not None, f"Missing config section {section}")
    body = match[2]
    for key, value in updates.items():
        body, count = re.subn(
            rf"^{re.escape(key)}\s*=.*$", f"{key} = {json.dumps(value)}", body, flags=re.MULTILINE
        )
        require(count == 1, f"Missing/duplicate config field {section}.{key}")
    return text[: match.start(2)] + body + text[match.end(2) :]


async def sync_phase(runner, session):
    source = runner.nodes[0]
    info = json.loads(await command("docker", "inspect", source.abci_container))[0]
    image = info["Image"]
    network = next(iter(info["NetworkSettings"]["Networks"]))
    source_home = STACK / ".localnet" / source.moniker / ".cometbft"
    export_code = """import json
from pathlib import Path
from xian.services.state_sync import StateSnapshotManager
p=Path('/root/.cometbft')
g=json.loads((p/'config/genesis.json').read_text())
m=StateSnapshotManager(storage_home=p/'xian',chain_id=g['chain_id'],chunk_size=32768)
print(json.dumps(m.export_snapshot()))
"""
    exported = json.loads(
        await command(
            "docker", "exec", "-i", source.abci_container, "python", "-", input=export_code
        )
    )
    require(exported["chunks"] > 1, "Interrupted state sync requires a multi-chunk snapshot")
    for node in runner.nodes[1:2]:
        # The node creates this directory as root on Linux. Copy through the
        # container API instead of writing into its bind mount as the host user.
        await command(
            "docker",
            "cp",
            str(source_home / "xian/snapshots") + "/.",
            f"{node.abci_container}:/root/.cometbft/xian/snapshots",
        )
    snapshot_height = int(exported["height"])
    await wait_height(session, source, snapshot_height + 2)
    trusted = await rpc(session, source, "block", height=snapshot_height)
    peers = ",".join(
        f"{n['node_id'].lower()}@{n['moniker']}:26656" for n in runner.network["nodes"]
    )
    target = await command(
        "docker",
        "exec",
        source.abci_container,
        "python",
        "-c",
        "import sysconfig; print(sysconfig.get_path('purelib')+'/sitecustomize.py')",
    )
    results = []
    for mode in ("replay", "state_sync_interrupted"):
        home = runner.output_dir / "observers" / mode
        require(
            not home.exists(), f"Observer home already exists: {home}; use a fresh run directory"
        )
        (home / "config").mkdir(parents=True)
        for path in (source_home / "config").iterdir():
            if path.name in {"node_key.json", "priv_validator_key.json"}:
                continue
            destination = home / "config" / path.name
            if path.is_dir():
                shutil.copytree(path, destination)
            else:
                shutil.copy2(path, destination)
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
        config = home / "config/config.toml"
        text = set_config(config.read_text(), "p2p", {"persistent_peers": peers})
        if mode != "replay":
            text = set_config(
                text,
                "statesync",
                {
                    "enable": True,
                    "rpc_servers": "http://node-0:26657,http://node-1:26657",
                    "trust_height": snapshot_height,
                    "trust_hash": trusted["block_id"]["hash"],
                    "discovery_time": "5s",
                    "chunk_fetchers": "1",
                },
            )
            (home / "e2e-fault.json").write_text(json.dumps({"stage": "snapshot_chunk"}))
        text = re.sub(
            r"^proxy_app\s*=.*$", 'proxy_app = "unix:///tmp/abci.sock"', text, flags=re.MULTILINE
        )
        write_config(config, text)
        runtime = home / "config/xian.toml"
        text = runtime.read_text()
        text = re.sub(r"^bds_enabled\s*=.*$", "bds_enabled = false", text, flags=re.MULTILINE)
        text = re.sub(
            r"^parallel_execution_enabled\s*=.*$",
            "parallel_execution_enabled = false",
            text,
            flags=re.MULTILINE,
        )
        write_config(runtime, text)
        container = f"xian-e2e-{runner.run_id.lower()}-" + mode.replace("_", "-")
        try:
            await command(
                "docker",
                "create",
                "--entrypoint",
                "/bin/bash",
                "--name",
                container,
                "--network",
                network,
                "--publish",
                "127.0.0.1::26657",
                "--memory",
                "1536m",
                "--env",
                f"XIAN_E2E_CONFIG_HASHES={config_hashes(home)}",
                "--volume",
                f"{home}:/root/.cometbft",
                image,
                "-c",
                OBSERVER_COMMAND,
            )
            if mode != "replay":
                await command(
                    "docker",
                    "cp",
                    STACK / "scripts/e2e_hooks/sitecustomize.py",
                    f"{container}:{target}",
                )
            await command("docker", "start", container)
            info = json.loads(await command("docker", "inspect", container))[0]
            require(
                info["State"]["Running"] and info["NetworkSettings"]["Ports"].get("26657/tcp"),
                f"Observer exited before RPC startup; inspect observer-{mode}.log",
            )
            port = info["NetworkSettings"]["Ports"]["26657/tcp"][0]["HostPort"]
            observer = SimpleNamespace(moniker=mode, rpc_url=f"http://127.0.0.1:{port}")
            if mode != "replay":
                hit = home / "e2e-fault.hit.json"
                deadline = time.monotonic() + 150
                while not hit.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                require(hit.exists(), "Observer never reached the snapshot interruption point")
                await command("docker", "restart", container)
                # Docker may allocate a different ephemeral host port on restart.
                info = json.loads(await command("docker", "inspect", container))[0]
                port = info["NetworkSettings"]["Ports"]["26657/tcp"][0]["HostPort"]
                observer.rpc_url = f"http://127.0.0.1:{port}"
            current = await height(session, source)
            await wait_height(session, observer, current, timeout=240)
            status = await rpc(session, observer, "status")
            require(
                int(status["validator_info"]["voting_power"]) == 0,
                "Fresh observer unexpectedly has validator voting power",
            )
            check = await agreement(session, [*runner.nodes, observer], window=3)
            sample_keys = [
                "validators.current",
                "governance.proposal_count",
                f"__n.{runner.founder_wallet.public_key}:",
            ]
            for key in sample_keys:
                expected = await query(session, source, "/get/" + key)
                actual = await query(session, observer, "/get/" + key)
                require(actual == expected, f"Observer state mismatch at {key}")
            results.append(
                {
                    "mode": mode,
                    "consensus": check,
                    "voting_power": 0,
                    "snapshot_height": snapshot_height if mode != "replay" else None,
                    "interrupted_after_first_chunk": mode != "replay",
                }
            )
        finally:
            logs = await command("docker", "logs", container, combine_output=True)
            (runner.output_dir / f"observer-{mode}.log").write_text(logs)
            await command("docker", "rm", "-f", "-v", container)
    return {"snapshot": exported, "observers": results}
