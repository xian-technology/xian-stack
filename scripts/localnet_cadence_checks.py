"""Measure actual idle block cadence on the owned five-validator localnet."""

from __future__ import annotations

import asyncio
import math
import time

from localnet_chain_checks import STACK, command, query, require, rpc
from localnet_e2e_support import E2EError
from localnet_history_checks import committed_policy
from localnet_rng_checks import timestamp_nanoseconds
from xian.node_setup import resolve_block_policy


def sample_span(samples):
    require(len(samples) >= 2, "Cadence needs at least two samples")
    previous = samples[0]
    for sample in samples:
        require(len(sample["heights"]) == 5, "Cadence requires five node heights")
        require(
            all(now >= old for now, old in zip(sample["heights"], previous["heights"])),
            "A sampled node height regressed",
        )
        require(
            sample["elapsed_seconds"] >= previous["elapsed_seconds"],
            "The observation clock regressed",
        )
        previous = sample
    return samples[-1]["elapsed_seconds"] - samples[0]["elapsed_seconds"]


def validate_window(samples, *, interval):
    elapsed = sample_span(samples)
    require(elapsed >= max(6, 4 * interval), "Cadence observation is too short")
    increases = [end - start for start, end in zip(samples[0]["heights"], samples[-1]["heights"])]
    initial = samples[0]
    require(
        all(sample["app_hash"] == initial["app_hash"] for sample in samples),
        "The committed root changed during an idle observation",
    )
    if interval == 0:
        require(
            all(sample["heights"] == initial["heights"] for sample in samples),
            "Settled on-demand mode produced an unsolicited block",
        )
    else:
        require(min(increases) >= 3, "Timed idle mode did not advance")
        require(
            max(increases) <= math.ceil(elapsed / interval) + 2,
            "Timed idle mode exceeded its interval: possible proof-block feedback",
        )
    return {
        "elapsed_seconds": elapsed,
        "minimum_blocks": min(increases),
        "maximum_blocks": max(increases),
    }


def validate_cutoff(before, after):
    from localnet_sync_checks import require_retention_progress

    require_retention_progress(before, after)


async def checkpoint(session, nodes):
    from localnet_sync_checks import retention_state

    require(len(nodes) == 5, "Cadence requires five validators")
    views = [
        item[0] for item in await asyncio.gather(*(committed_policy(session, n) for n in nodes))
    ]
    view = views[0]
    require(all(v == view for v in views), "Nodes do not share a committed evidence checkpoint")
    schedule, *proofs = await asyncio.gather(
        query(session, nodes[0], "/get/__evidence_policy"),
        *(rpc(session, n, "block", height=view.height) for n in nodes),
        *(rpc(session, n, "consensus_params", height=view.height + 1) for n in nodes),
    )
    blocks, native = proofs[:5], proofs[5:]
    block = blocks[0]
    header = block["block"]["header"]
    require(
        all(
            other["block_id"] == block["block_id"] and other["block"]["header"] == header
            for other in blocks
        ),
        "Cadence peers do not share the same block header",
    )
    require(
        timestamp_nanoseconds(header["time"]) == view.last_block_nanos,
        "Committed evidence header time differs from the checkpoint",
    )
    state = retention_state(schedule, view)
    require(
        all(value["consensus_params"]["evidence"] == dict(view.evidence) for value in native),
        "Committed application and native evidence parameters disagree",
    )
    return {
        **state,
        "block_hash": block["block_id"]["hash"],
        "header_app_hash": str(header["app_hash"]).lower(),
        "last_commit_flags": [
            int(sig["block_id_flag"])
            for sig in (block["block"].get("last_commit") or {}).get("signatures", [])
        ],
        "tx_count": len(block["block"].get("data", {}).get("txs") or []),
        "native_evidence": dict(view.evidence),
    }


async def ready_checkpoint(session, nodes, *, timeout=30):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            return await checkpoint(session, nodes)
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.25)
    raise E2EError(f"No common committed cadence checkpoint: {last_error}")


async def sample_nodes(session, nodes, started):
    statuses = await asyncio.gather(*(rpc(session, n, "status") for n in nodes))
    require(
        all(not item["sync_info"]["catching_up"] for item in statuses),
        "Cadence measurement requires caught-up validators",
    )
    view, _ = await committed_policy(session, nodes[0])
    return {
        "elapsed_seconds": time.monotonic() - started,
        "heights": [int(item["sync_info"]["latest_block_height"]) for item in statuses],
        "app_hash": view.app_hash,
    }


async def settle(session, nodes, *, timeout=30, quiet_seconds=2):
    started = time.monotonic()
    previous = await sample_nodes(session, nodes, started)
    quiet_since = time.monotonic()
    while time.monotonic() - started < timeout:
        await asyncio.sleep(0.25)
        current = await sample_nodes(session, nodes, started)
        if (current["heights"], current["app_hash"]) != (
            previous["heights"],
            previous["app_hash"],
        ) or len(set(current["heights"])) != 1:
            quiet_since = time.monotonic()
        if time.monotonic() - quiet_since >= quiet_seconds:
            return current
        previous = current
    raise E2EError(
        "On-demand chain never settled; possible proof-block feedback or real state work"
    )


async def empty_headers(session, node, first, last):
    result = []
    for height in range(first, last + 1):
        block = await rpc(session, node, "block", height=height)
        require(not block["block"].get("data", {}).get("txs"), "Idle window contains a transaction")
        result.append({"height": height, "time": block["block"]["header"]["time"]})
    return result


async def observe_window(session, nodes, *, interval):
    before = await ready_checkpoint(session, nodes)
    if interval == 0:
        validate_idle_commit(before)
    started = time.monotonic()
    samples = [await sample_nodes(session, nodes, started)]
    while time.monotonic() - started < max(6, interval * 5):
        await asyncio.sleep(0.25)
        samples.append(await sample_nodes(session, nodes, started))
    validation = validate_window(samples, interval=interval)
    after = await ready_checkpoint(session, nodes)
    if interval == 0:
        validate_idle_commit(after)
    validate_cutoff(before, after)
    headers = await empty_headers(
        session, nodes[0], min(samples[0]["heights"]) + 1, max(samples[-1]["heights"])
    )
    return {
        "before": before,
        "after": after,
        "samples": samples,
        "validation": validation,
        "empty_headers": headers,
    }


def validate_idle_commit(point):
    require(point["tx_count"] == 0, "Settled idle checkpoint must be empty")
    require(
        point["header_app_hash"] == point["app_hash"],
        "Last idle header does not bind the stable committed application root",
    )
    require(
        point["last_commit_flags"] == [2] * 5,
        "Idle fixture requires a full five-validator signing commit",
    )


def configured_text(text, mode):
    from localnet_sync_checks import set_config

    enabled, interval = resolve_block_policy(
        mode=mode, interval="0s" if mode == "on_demand" else "2s"
    )
    return set_config(
        text,
        "consensus",
        {"create_empty_blocks": enabled, "create_empty_blocks_interval": interval},
    )


def original_configs(runner):
    require(len(runner.nodes) == 5, "Cadence requires five validators")
    require(
        len({n.cometbft_container == n.abci_container for n in runner.nodes}) == 1,
        "Cadence requires a consistent runtime topology",
    )
    return {
        STACK / ".localnet" / node.moniker / ".cometbft/config/config.toml": (
            STACK / ".localnet" / node.moniker / ".cometbft/config/config.toml"
        ).read_text()
        for node in runner.nodes
    }


async def install_configs(runner, session, configs):
    containers = [n.cometbft_container for n in runner.nodes]
    await command("docker", "stop", "--time", "30", *containers)
    try:
        for path, text in configs.items():
            path.write_text(text)
    finally:
        await command("docker", "start", *containers)
    return await ready_checkpoint(session, runner.nodes)


async def require_quiet_fixture(session, nodes, accounts):
    mode, pending, mempools, missed = await asyncio.gather(
        query(session, nodes[0], "/get/validators.config:selection_mode"),
        query(session, nodes[0], "/get/validator_penalties.pending_head"),
        asyncio.gather(*(rpc(session, n, "num_unconfirmed_txs") for n in nodes)),
        asyncio.gather(
            *(query(session, nodes[0], f"/get/validators.missed_blocks:{a}") for a in accounts)
        ),
    )
    require(mode == "manual", "Cadence fixture needs manual validator selection")
    require(pending in (None, 0), "Cadence fixture has unfinished slash work")
    require(all(int(m["n_txs"]) == 0 for m in mempools), "Cadence fixture has pending transactions")
    require(
        all(value in (None, 0) for value in missed), "Cadence fixture has open missed-block counts"
    )
    return {"manual_selection": True, "missed_blocks": missed, "empty_mempools": True}


async def paid_probe(runner, session, label):
    result = await runner.submit_contract_call(
        session,
        wallet=runner.founder_wallet,
        node_index=0,
        contract="currency",
        function="transfer",
        kwargs={"amount": 1, "to": runner.founder_wallet.public_key},
        label=label,
    )
    require(result["success"] is True and result["height"] > 0, "Cadence probe did not commit")
    return {"tx_hash": result["tx_hash"], "height": result["height"], "success": True}


async def brief_absence(runner, session, label):
    container = runner.nodes[-1].cometbft_container
    await command("docker", "stop", "--time", "30", container)
    try:
        missing = await paid_probe(runner, session, label + "-brief-absence")
    finally:
        await command("docker", "start", container)
    await ready_checkpoint(session, runner.nodes)
    recovered = await paid_probe(runner, session, label + "-recovered")
    await settle(session, runner.nodes)
    await require_quiet_fixture(
        session, runner.nodes, [w.public_key for w in runner.validator_wallets]
    )
    return {
        "absence_probe": missing,
        "recovered_probe": recovered,
        "idle_after_recovery": await observe_window(session, runner.nodes, interval=0),
    }


async def cadence_rehearsal(runner, session, *, label, expected_pending):
    accounts = [w.public_key for w in runner.validator_wallets]
    before = await ready_checkpoint(session, runner.nodes)
    fixture = await require_quiet_fixture(session, runner.nodes, accounts)
    require(
        before["pending"] is expected_pending,
        "Cadence fixture has the wrong pending/saturated retention state",
    )
    original = original_configs(runner)
    cases = []
    try:
        for mode, interval in (("on_demand", 0), ("idle_interval", 2), ("periodic", 2)):
            changed = {path: configured_text(text, mode) for path, text in original.items()}
            restart = await install_configs(runner, session, changed)
            if interval == 0:
                await settle(session, runner.nodes)
            healthy = await require_quiet_fixture(session, runner.nodes, accounts)
            observation = await observe_window(session, runner.nodes, interval=interval)
            case = {
                "mode": mode,
                "interval_seconds": interval,
                "restart": restart,
                "healthy_fixture": healthy,
                **observation,
            }
            if interval == 0:
                case["brief_absence_recovery"] = await brief_absence(runner, session, label)
            cases.append(case)
    finally:
        restored = await install_configs(runner, session, original)
    validate_cutoff(before, restored)
    return {
        "label": label,
        "expected_pending": expected_pending,
        "fixture": fixture,
        "cases": cases,
        "restored": restored,
    }
