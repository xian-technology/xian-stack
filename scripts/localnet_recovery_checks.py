"""Real process-failure and quorum recovery phases for the five-node harness."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

from localnet_chain_checks import (
    PROBE_SOURCE,
    STACK,
    agreement,
    broadcast,
    command,
    deploy,
    height,
    prepared,
    query,
    receipt,
    require,
    rpc,
    wait_height,
    wallet,
)
from localnet_e2e_support import E2EError


@asynccontextmanager
async def partition(runner, isolated):
    """Split this localnet and restore its aliases and available original IPs."""
    name = "xian-e2e-partition-" + runner.run_id.lower()
    records = []
    created = False
    try:
        await command("docker", "network", "create", name)
        created = True
        for node in isolated:
            container = node.cometbft_container
            info = json.loads(await command("docker", "inspect", container))[0]
            networks = info["NetworkSettings"]["Networks"]
            require(len(networks) == 1, f"{container} must start on exactly one localnet network")
            network, endpoint = next(iter(networks.items()))
            require(network.endswith("_localnet"), f"Unexpected localnet network {network}")
            record = (container, network, endpoint)
            records.append(record)
            await command("docker", "network", "connect", "--alias", node.moniker, name, container)
            await command("docker", "network", "disconnect", network, container)
        yield
    finally:
        for container, network, endpoint in reversed(records):
            current = json.loads(await command("docker", "inspect", container))[0]
            connected = current["NetworkSettings"]["Networks"]
            if network not in connected:
                network_info = json.loads(await command("docker", "network", "inspect", network))[0]
                occupied = {
                    item["IPv4Address"].split("/")[0]
                    for item in (network_info.get("Containers") or {}).values()
                }
                args = ["docker", "network", "connect"]
                # A restarted peer may have received a disconnected node's IP.
                # Preserve DNS aliases; retain the old address when available.
                if endpoint["IPAddress"] not in occupied:
                    args += ["--ip", endpoint["IPAddress"]]
                for alias in endpoint.get("Aliases") or []:
                    args += ["--alias", alias]
                try:
                    await command(*args, network, container)
                except E2EError as exc:
                    # Linux Engine can reject an explicit address on a bridge
                    # whose subnet Docker allocated automatically. Persistent
                    # peers use DNS aliases, so automatic allocation is safe.
                    if "--ip" not in args or (
                        "user specified IP address is supported only when connecting "
                        "to networks with user configured subnets"
                    ) not in str(exc):
                        raise
                    index = args.index("--ip")
                    del args[index : index + 2]
                    await command(*args, network, container)
            if name in connected:
                await command("docker", "network", "disconnect", name, container)
        if created:
            await command("docker", "network", "rm", name)


async def container_height(node):
    source = (
        "import json,urllib.request; "
        "print(json.load(urllib.request.urlopen('http://127.0.0.1:26657/status'))"
        "['result']['sync_info']['latest_block_height'])"
    )
    # The isolated group need not retain host RPC connectivity.
    return int(await command("docker", "exec", node.cometbft_container, "python", "-c", source))


async def quorum_phase(runner, session):
    validators = (await rpc(session, runner.nodes[0], "validators"))["validators"]
    powers = {v["address"]: int(v["voting_power"]) for v in validators}
    node_powers = []
    for node in runner.nodes:
        address = (await rpc(session, node, "status"))["validator_info"]["address"]
        node_powers.append(powers[address])
    total = sum(node_powers)
    require(sum(node_powers[:4]) * 3 > total * 2, "4-node group must hold quorum")
    require(
        sum(node_powers[:3]) * 3 <= total * 2 and sum(node_powers[3:]) * 3 <= total * 2,
        "3/2 groups must both lack quorum",
    )
    checks = []
    async with partition(runner, runner.nodes[4:]):
        start = await height(session, runner.nodes[0])
        await wait_height(session, runner.nodes[0], start + 3)
        checks.append(
            {
                "split": "4/1",
                "start_height": start,
                "end_height": await height(session, runner.nodes[0]),
            }
        )
    await agreement(session, runner.nodes)
    async with partition(runner, runner.nodes[3:]):
        # Allow an in-flight commit to finish before measuring the halt.
        await asyncio.sleep(3)
        before = [await container_height(n) for n in runner.nodes]
        await asyncio.sleep(15)
        after = [await container_height(n) for n in runner.nodes]
        require(before == after, f"Blocks advanced without quorum: {before} -> {after}")
        checks.append({"split": "3/2", "heights_before": before, "heights_after": after})
    consensus = await agreement(session, runner.nodes)
    before_restart = max(consensus["heights"].values())
    await runner.restart_localnet_and_wait_ready(session)
    await wait_height(session, runner.nodes[0], before_restart + 1)
    return {
        "partitions": checks,
        "voting_powers": node_powers,
        "whole_network_restart": True,
        "consensus": await agreement(session, runner.nodes),
    }


async def crash_phase(runner, session):
    name = await deploy(runner, session, "crash", PROBE_SOURCE)
    node = runner.nodes[4]
    signer = wallet(runner, "crash-signer")
    await runner.fund_wallets(session, [signer], amount=1000)
    home = STACK / ".localnet" / node.moniker / ".cometbft"
    arm = home / "e2e-fault.json"
    hit = home / "e2e-fault.hit.json"
    target = await command(
        "docker",
        "exec",
        node.abci_container,
        "python",
        "-c",
        "import sysconfig; print(sysconfig.get_path('purelib')+'/sitecustomize.py')",
    )
    exists = await command(
        "docker",
        "exec",
        node.abci_container,
        "python",
        "-c",
        f"from pathlib import Path; print(Path({target!r}).exists())",
    )
    require(exists == "False", "Refusing to replace an existing sitecustomize.py")
    checks = []
    try:
        await command(
            "docker",
            "cp",
            STACK / "scripts/e2e_hooks/sitecustomize.py",
            f"{node.abci_container}:{target}",
        )
        await runner.restart_node_runtime(session, node)
        for nonce, stage in enumerate(("before_persist", "after_persist", "before_response")):
            key = f"{name}.counts:{stage}"
            hit.unlink(missing_ok=True)
            arm.write_text(json.dumps({"key": key, "value": 1, "stage": stage}))
            tx = prepared(runner, signer, nonce, name, "bump", {"key": stage})
            await broadcast(session, runner.nodes[0], tx)
            committed = await receipt(session, runner.nodes[0], tx)
            deadline = time.monotonic() + 45
            while not hit.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
            require(hit.exists(), f"Crash boundary {stage} was never reached")
            fault = json.loads(hit.read_text())
            require(fault["stage"] == stage, "Wrong crash boundary fired")
            await runner.restart_node_runtime(session, node, target_height=int(committed["height"]))
            consensus = await agreement(session, runner.nodes)
            values = [await query(session, n, "/get/" + key) for n in runner.nodes]
            require(values == [1] * len(runner.nodes), "Crash replay lost or duplicated state")
            nonces = [
                await query(session, n, f"/get/__n.{signer.public_key}:") for n in runner.nodes
            ]
            require(nonces == [nonce] * len(runner.nodes), "Crash replay changed committed nonce")
            checks.append(
                {
                    "stage": stage,
                    "fault": fault,
                    "tx_hash": tx.tx_hash,
                    "height": committed["height"],
                    "consensus": consensus,
                }
            )
    finally:
        arm.unlink(missing_ok=True)
        await command("docker", "start", node.abci_container)
        await command("docker", "exec", node.abci_container, "rm", "-f", target)
        await runner.restart_node_runtime(session, node)
    return {"boundaries": checks, "contract": name}
