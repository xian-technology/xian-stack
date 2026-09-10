"""Live execution parity, nonce lifecycle, and block/transaction boundary checks."""

from __future__ import annotations

import asyncio
import base64
import json
import math

from localnet_chain_checks import (
    PROBE_SOURCE,
    agreement,
    broadcast,
    config_path,
    deploy,
    edit_toml,
    execution,
    prepared,
    query,
    receipt,
    require,
    rpc,
    wait_height,
    wallet,
    write_config,
)
from localnet_recovery_checks import partition
from localnet_sync_checks import set_config


async def mixed_phase(runner, session):
    serial = runner.nodes[4]
    originals = {}
    for node in runner.nodes:
        path = config_path(node)
        originals[path] = edit_toml(
            path,
            parallel_execution_enabled=node != serial,
            parallel_execution_min_transactions=4,
        )
    signers = [wallet(runner, f"mixed-{i}") for i in range(24)]
    recipients = [wallet(runner, f"mixed-recipient-{i}") for i in range(24)]
    try:
        await runner.restart_localnet_and_wait_ready(session)
        await runner.fund_wallets(session, signers, amount=1000)
        txs = [
            prepared(
                runner, w, 0, "currency", "transfer", {"amount": 1, "to": recipient.public_key}
            )
            for w, recipient in zip(signers, recipients, strict=True)
        ]
        async with partition(runner, runner.nodes[3:]):
            await asyncio.sleep(3)
            for tx in txs:
                await broadcast(session, runner.nodes[0], tx)
        receipts = [await receipt(session, runner.nodes[0], tx) for tx in txs]
        heights = {int(r["height"]) for r in receipts}
        await agreement(session, runner.nodes, window=max(heights) - min(heights) + 2)
        metadata = {}
        for node in runner.nodes:
            status = await query(session, node, "/perf_status")
            blocks = [b for b in status["recent_blocks"] if int(b["height"]) in heights]
            require(
                len(blocks) == len(heights), f"Missing mixed-execution profile on {node.moniker}"
            )
            accepted = sum(b["metadata"].get("parallel_speculative_accepted", 0) for b in blocks)
            require(
                accepted == 0 if node == serial else accepted > 0,
                f"Expected execution path was not exercised on {node.moniker}: {accepted}",
            )
            metadata[node.moniker] = {
                "speculative_accepted": accepted,
                "configured_parallel": node != serial,
            }
        for recipient in recipients:
            amount = await query(session, serial, f"/get/currency.balances:{recipient.public_key}")
            value = amount.get("__fixed__", amount) if isinstance(amount, dict) else amount
            require(float(value) == 1, "Mixed execution transfer total is wrong")
        return {
            "metadata": metadata,
            "transaction_count": len(txs),
            "consensus": await agreement(session, runner.nodes),
        }
    finally:
        for path, original in originals.items():
            write_config(path, original)
        await runner.restart_localnet_and_wait_ready(session)


async def nonce_phase(runner, session):
    signer, recipient = wallet(runner, "nonce-signer"), wallet(runner, "nonce-recipient")
    await runner.fund_wallets(session, [signer], amount=100)

    def transfer(nonce, amount):
        return prepared(
            runner,
            signer,
            nonce,
            "currency",
            "transfer",
            {"amount": amount, "to": recipient.public_key},
            chi=100,
        )

    first, second = transfer(0, 70), transfer(1, 70)
    checks = {}
    async with partition(runner, runner.nodes[3:]):
        await asyncio.sleep(3)
        checks["gap_rejected"] = await broadcast(session, runner.nodes[0], second, accepted=False)
        await broadcast(session, runner.nodes[0], first)
        checks["conflicting_nonce_rejected"] = await broadcast(
            session, runner.nodes[0], transfer(0, 71), accepted=False
        )
        await broadcast(session, runner.nodes[0], second)
        # Restart only the submitting node while consensus is unable to commit.
        await runner.stop_node_runtime(runner.nodes[0])
        await runner.start_node_runtime(session, runner.nodes[0])
        checks["first_after_restart"] = await broadcast(
            session, runner.nodes[0], first, allow_cached=True
        )
        checks["second_after_restart"] = await broadcast(
            session, runner.nodes[0], second, allow_cached=True
        )
    checks["first_receipt"] = await receipt(session, runner.nodes[0], first)
    checks["second_receipt"] = await receipt(session, runner.nodes[0], second, success=False)
    require(
        "not enough coins" in json.dumps(execution(checks["second_receipt"])).lower(),
        "Second transfer failed for an unexpected reason",
    )
    third = transfer(2, 1)
    await broadcast(session, runner.nodes[0], third)
    await receipt(session, runner.nodes[0], third)
    await agreement(session, runner.nodes)
    for node in runner.nodes:
        require(
            await query(session, node, f"/get/__n.{signer.public_key}:") == 2,
            "Nonce did not recover after pending restart and failed execution",
        )
        balance = await query(session, node, f"/get/currency.balances:{recipient.public_key}")
        require(
            float(balance.get("__fixed__", balance) if isinstance(balance, dict) else balance)
            == 71,
            "Pending restart lost or duplicated a payment",
        )
    checks["committed_replay_rejected"] = await broadcast(
        session, runner.nodes[0], transfer(0, 2), accepted=False
    )
    return {"checks": checks, "consensus": await agreement(session, runner.nodes)}


async def limits_phase(runner, session):
    originals = {}
    try:
        # Full-size blocks take longer to gossip at the configured P2P bandwidth
        # than the ordinary three-second proposal window on a shared host.
        for node in runner.nodes:
            path = config_path(node).with_name("config.toml")
            originals[path] = path.read_text()
            write_config(
                path,
                set_config(
                    originals[path],
                    "consensus",
                    {
                        "timeout_propose": "30s",
                        "timeout_prevote": "10s",
                        "timeout_precommit": "10s",
                    },
                ),
            )
        await runner.restart_localnet_and_wait_ready(session, require_additional_block=False)
        await agreement(session, runner.nodes, window=1)
        result = await limits_workload(runner, session)
        result["proposal_timeout"] = "30s"
        return result
    finally:
        for path, original in originals.items():
            write_config(path, original)
        await runner.restart_localnet_and_wait_ready(session, require_additional_block=False)
        await agreement(session, runner.nodes, window=1)


async def limits_workload(runner, session):
    name = await deploy(runner, session, "block-limits", PROBE_SOURCE)
    params = await rpc(session, runner.nodes[0], "consensus_params")
    max_block = int(params["consensus_params"]["block"]["max_bytes"])
    # Node admission defaults to the same 4 MiB ceiling as localnet CometBFT.
    # Read the actual CometBFT setting; the E2E configuration owns both sides.
    import tomllib

    comet = tomllib.loads(config_path(runner.nodes[0]).with_name("config.toml").read_text())
    max_tx = int(comet["mempool"]["max_tx_bytes"])
    require(
        4096 <= max_tx <= 4 * 1024 * 1024 and max_block <= 32 * 1024 * 1024,
        "Boundary workload requires bounded localnet limits",
    )
    count = math.ceil(max_block / (max_tx * 0.75)) + 2
    signers = [wallet(runner, f"limits-{i}") for i in range(count + 2)]
    await runner.fund_wallets(session, signers, amount=1000)

    def sized(w, size):
        empty = prepared(runner, w, 0, name, "sized", {"blob": ""})
        length = (size - len(empty.submitted_bytes)) // 2
        return prepared(runner, w, 0, name, "sized", {"blob": "x" * length})

    exact = sized(signers[0], max_tx)
    require(len(exact.submitted_bytes) == max_tx, "Could not construct exact-size transaction")
    oversized = sized(signers[-1], max_tx + 2)
    rejection = await broadcast(session, runner.nodes[0], oversized, accepted=False)
    txs = [exact] + [sized(w, int(max_tx * 0.75)) for w in signers[1:count]]
    total = sum(len(tx.submitted_bytes) for tx in txs)
    require(total > max_block, "Boundary workload does not exceed one block")
    async with partition(runner, runner.nodes[3:]):
        await asyncio.sleep(3)
        for tx in txs:
            await broadcast(session, runner.nodes[0], tx)
    receipts = [await receipt(session, runner.nodes[0], tx, timeout=180) for tx in txs]
    heights = sorted({int(r["height"]) for r in receipts})
    require(len(heights) >= 2, "Oversized aggregate workload fit in one block")
    blocks = []
    for h in heights:
        block = (await rpc(session, runner.nodes[0], "block", height=h))["block"]
        tx_bytes = sum(len(base64.b64decode(tx)) for tx in block["data"]["txs"] or [])
        require(tx_bytes <= max_block, f"Block {h} exceeds configured byte limit")
        metadata = await rpc(session, runner.nodes[0], "blockchain", minHeight=h, maxHeight=h)
        block_size = int(metadata["block_metas"][0]["block_size"])
        require(block_size <= max_block, f"Serialized block {h} exceeds the configured limit")
        blocks.append({"height": h, "transaction_bytes": tx_bytes, "block_bytes": block_size})
    # Meter exhaustion must fail the call while preserving later progress.
    exhausted = prepared(runner, signers[-2], 0, name, "work", {"rounds": 100000}, chi=100)
    await broadcast(session, runner.nodes[0], exhausted)
    failed = await receipt(session, runner.nodes[0], exhausted, success=False)
    exhausted_execution = execution(failed)
    require(exhausted_execution["chi_used"] == 100, "Compute test did not exhaust its budget")
    failure_text = str(exhausted_execution.get("result", "")).lower()
    require(
        "out of chi" in failure_text,
        "Execution limit case failed for an unexpected reason",
    )
    for node in runner.nodes:
        require(
            await query(session, node, f"/get/{name}.counts:work-started") is None,
            "Compute exhaustion persisted a partial contract write",
        )
    recovery = prepared(runner, signers[-2], 1, name, "bump", {"key": "after-limit"})
    await broadcast(session, runner.nodes[0], recovery)
    await receipt(session, runner.nodes[0], recovery)
    await wait_height(session, runner.nodes[0], max(heights) + 1)
    return {
        "contract": name,
        "max_tx_bytes": max_tx,
        "max_block_bytes": max_block,
        "aggregate_bytes": total,
        "blocks": blocks,
        "oversized_rejection": rejection,
        "execution_limit_receipt": failed,
        "consensus": await agreement(session, runner.nodes),
    }
