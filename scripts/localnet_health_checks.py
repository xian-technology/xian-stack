"""Validate the live topology and common committed state before running workloads."""

import asyncio
import hashlib
import time

import aiohttp
from localnet_chain_checks import agreement, require, rpc
from localnet_e2e_support import E2EError


def validator_powers(validators):
    return {v["address"].upper(): int(v.get("voting_power", v.get("power"))) for v in validators}


def check_topology(moniker, observation, *, chain_id, node_id, peers, validator_powers):
    require(observation["chain_id"] == chain_id, f"{moniker}: wrong chain ID")
    require(observation["node_id"] == node_id, f"{moniker}: wrong node ID")
    require(not observation["catching_up"], f"{moniker}: still catching up")
    require(
        observation["peers"] == peers,
        f"{moniker}: expected peers {sorted(peers)}, got {sorted(observation['peers'])}",
    )
    require(
        observation["validator_powers"] == validator_powers,
        f"{moniker}: unexpected validator set or voting power: {observation['validator_powers']}",
    )


async def observe_node(session, node, status, checkpoint):
    net_info, validators = await asyncio.gather(
        rpc(session, node, "net_info"),
        rpc(session, node, "validators", height=checkpoint),
    )
    return {
        "chain_id": status["node_info"]["network"],
        "node_id": status["node_info"]["id"].lower(),
        "catching_up": status["sync_info"]["catching_up"],
        "height": int(status["sync_info"]["latest_block_height"]),
        "app_hash": status["sync_info"]["latest_app_hash"],
        "peers": {peer["node_info"]["id"].lower() for peer in (net_info["peers"] or [])},
        "validator_powers": validator_powers(validators["validators"]),
    }


async def wait_for_topology(runner, session, expected_ids, expected_powers):
    deadline = time.monotonic() + runner.args.rpc_timeout_seconds
    peers = set(expected_ids.values())
    last_error = None
    while time.monotonic() < deadline:
        try:
            statuses = await asyncio.gather(
                *(rpc(session, node, "status") for node in runner.nodes)
            )
            checkpoint = min(int(status["sync_info"]["latest_block_height"]) for status in statuses)
            observations = await asyncio.gather(
                *(
                    observe_node(session, node, status, checkpoint)
                    for node, status in zip(runner.nodes, statuses, strict=True)
                )
            )
            for node, observation in zip(runner.nodes, observations, strict=True):
                node_id = expected_ids[node.moniker]
                check_topology(
                    node.moniker,
                    observation,
                    chain_id=runner.network["chain_id"],
                    node_id=node_id,
                    peers=peers - {node_id},
                    validator_powers=expected_powers,
                )
            return [
                node_report(node, observed)
                for node, observed in zip(runner.nodes, observations, strict=True)
            ]
        except (aiohttp.ClientError, E2EError, TimeoutError) as exc:
            last_error = exc
        await asyncio.sleep(0.5)
    raise E2EError(f"Localnet topology did not converge: {last_error}")


def node_report(node, observation):
    peers = observation["peers"]
    return {
        **observation,
        "moniker": node.moniker,
        "peers": sorted(peers),
        "peer_count": len(peers),
        "validator_count": len(observation["validator_powers"]),
    }


async def healthy_network(runner, session, genesis):
    expected_ids = {node["moniker"]: node["node_id"].lower() for node in runner.network["nodes"]}
    expected_powers = validator_powers(genesis["validators"])
    validator_addresses = {
        hashlib.sha256(bytes.fromhex(node.account_public_key)).hexdigest()[:40].upper()
        for node in runner.nodes
    }
    require(len(set(expected_ids.values())) == len(runner.nodes), "Genesis node IDs must be unique")
    require(
        len(validator_addresses) == len(runner.nodes)
        and validator_addresses == expected_powers.keys(),
        "Genesis validator keys do not match the five configured nodes",
    )
    require(genesis["chain_id"] == runner.network["chain_id"], "Wrong genesis chain ID")
    statuses = await wait_for_topology(runner, session, expected_ids, expected_powers)
    consensus = await agreement(session, runner.nodes, window=runner.args.app_hash_window)
    return {"nodes": statuses, "consensus": consensus}
