"""Real stale-genesis preflight and positive-period staking-clock rehearsal.

The standalone preflight owns a disposable three-validator network. Only the
coordinator executes it, serially before the normal five-validator suite.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from localnet_chain_checks import broadcast, command, query, require, rpc, wait_height
from localnet_e2e_support import normalize_value
from localnet_rng_checks import timestamp_nanoseconds
from localnet_sync_checks import (
    create_observer,
    find_evidence_block,
    governance_vote,
    observer_address,
    protocol_module,
    retention_sample,
    stop_backup_source,
)
from xian_py import transaction as tr
from xian_py.wallet import Wallet
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import decode

DAY_NS = 86_400_000_000_000
# The preflight pre-signs dependent H1 calls before any state commits and H3's
# denial before its proposal window. Their combined H1 budget is checked below.
PREFLIGHT_TX_CHI = 100_000
TEST_BASE_BLOCK_WINDOW = 5
STALE_DAYS = 100
INITIAL_PREVOTE_STEP = "1/0/4"
PUBLIC_RECORD_FIELDS = {
    "id",
    "unbond_id",
    "owner",
    "validator",
    "amount",
    "kind",
    "created_block",
    "created_at",
    "unlock_at",
    "claimed",
    "unbonding_period_days",
}
NIL_VOTE = re.compile(r"Vote\{(\d+):([0-9A-F]{12}) (\d+)/(\d+)/\S+\(Prevote\) ([0-9A-F]{12}) .+\}")


def number(value):
    if isinstance(value, dict):
        value = value["__fixed__"]
    return ContractingDecimal(str(0 if value is None else value))


def contract_time_nanos(value):
    if isinstance(value, str):
        # Public state queries format contract datetimes as ISO calendar strings.
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        # The contract ABI's naive calendar value is UTC, not the host timezone.
        instant = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    else:
        value = normalize_value(value)
        instant = datetime(*value["__time__"], tzinfo=UTC)
    elapsed = instant - datetime(1970, 1, 1, tzinfo=UTC)
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000_000 + elapsed.microseconds * 1000


def validate_initial_state(consensus, native, app_powers, nodes, low):
    wanted = {node["address"]: 10 for node in nodes}
    actual = {entry["address"]: int(entry["voting_power"]) for entry in native["validators"]}
    require(len(nodes) == 3 and actual == wanted, "Actual InitChain validator powers differ")
    require(app_powers == {node["account"]: 10 for node in nodes}, "Application powers differ")
    state = consensus["round_state"]
    proposer = state["proposer"]["address"]
    require(proposer == min(wanted) and proposer != low["address"], "Unexpected initial proposer")
    require(
        state["height/round/step"] == INITIAL_PREVOTE_STEP, "Expected actual round0 prevote step"
    )
    index = next(
        i for i, entry in enumerate(native["validators"]) if entry["address"] == low["address"]
    )
    rounds = state["height_vote_set"]
    require(int(rounds[0]["round"]) == 0, "Missing actual round0 votes")
    vote = NIL_VOTE.fullmatch(rounds[0]["prevotes"][index])
    require(vote is not None, "No actual LOW NIL prevote was observed")
    require(
        tuple(vote.groups()) == (str(index), low["address"][:12], "1", "00", "000000000000"),
        "Actual LOW round0 NIL prevote differs",
    )
    return {
        "powers": actual,
        "proposer": proposer,
        "low": low["address"],
        "height_round_step": state["height/round/step"],
        "nil_prevote_verified": True,
    }


def validate_stale_headers(headers, genesis_nanos, wall_start_nanos, wall_end_nanos):
    require([header["height"] for header in headers] == ["1", "2", "3"], "Wrong preflight heights")
    times = [timestamp_nanoseconds(header["time"]) for header in headers]
    require(times[0] == genesis_nanos, "The actual H1 timestamp was not the old genesis time")
    require(wall_start_nanos - genesis_nanos >= 99 * DAY_NS, "Genesis was not genuinely stale")
    require(times[0] < times[1] <= times[2], "Successor clock did not advance monotonically")
    skew = 60_000_000_000
    require(
        wall_start_nanos - skew <= times[1] <= wall_end_nanos + skew, "H2 was not near current time"
    )
    return {"headers": headers, "stale_days": STALE_DAYS, "wall_skew_bound_nanos": skew}


def validate_anchor(record, created_height, anchor_header, days):
    require(int(record["created_block"]) == created_height, "Unbond creation height changed")
    require(int(anchor_header["height"]) == created_height + 1, "Wrong successor anchor height")
    require(
        int(record["unbonding_period_days"]) == days and days > 0,
        "Captured positive period changed",
    )
    # Contract Datetime uses microseconds; never hide the ABI precision with a tolerance.
    anchor = timestamp_nanoseconds(anchor_header["time"]) // 1000 * 1000
    unlock = contract_time_nanos(record["unlock_at"])
    require(unlock == anchor + days * DAY_NS, "Unbond did not use the exact successor VM clock")
    require(record["claimed"] is False, "Early unbond was claimed")
    return {
        "anchor_height": created_height + 1,
        "anchor_vm_nanos": anchor,
        "captured_days": days,
        "unlock_nanos": unlock,
    }


def validate_locked_finances(before, after, record, amount):
    amount = number(amount)
    require(
        number(after["custody"]) == number(before["custody"]) + amount,
        "Unbond principal left custody",
    )
    require(
        number(after["self_bond"]) == number(before["self_bond"]), "Self-bond principal changed"
    )
    require(
        number(record["amount"]) == amount and record["claimed"] is False,
        "Pending principal changed",
    )
    require(
        number(after["pending_total"]) == number(before["pending_total"]) + amount,
        "Pending total differs",
    )
    require(after["heap_size"] == before["heap_size"] + 1, "Pending heap/index entry disappeared")
    return {"before": before, "after": after, "locked_principal": str(amount)}


async def financial_state(session, node, owner):
    paths = {
        "self_bond": f"validators.self_bond:{owner}",
        "custody": "currency.balances:validators",
        "pending_total": f"validator_penalties.unbond_totals:{owner}",
        "heap_size": f"validator_penalties.unbond_heap_sizes:{owner}",
    }
    values = await asyncio.gather(
        *(query(session, node, "/get/" + path) for path in paths.values())
    )
    result = {key: str(number(value)) for key, value in zip(paths, values, strict=True)}
    result["heap_size"] = int(result["heap_size"])
    return result


def safe_record(record):
    return normalize_value(
        {key: value for key, value in record.items() if key in PUBLIC_RECORD_FIELDS}
    )


def safe_receipt(receipt):
    fields = {"height", "tx_hash", "success", "chi_used", "chi_supplied"}
    return {key: value for key, value in receipt.items() if key in fields}


async def pending_index_record(session, node, owner, predicate):
    cursor = None
    seen = set()
    found = []
    for _ in range(10):
        path = f"/validators_pending_unbonds_page/{owner}/limit=100"
        if cursor is not None:
            path += f"/cursor={cursor}"
        page = await query(session, node, path)
        found.extend(record for record in page["unbonds"] if predicate(record))
        cursor = page["next_cursor"]
        if cursor is None:
            require(len(found) == 1, "Cannot identify exactly one pending unbond index entry")
            return found[0]
        require(cursor not in seen, "Unbond page cursor repeated")
        seen.add(cursor)
    raise RuntimeError("Unbond discovery exceeded its bounded page budget")


async def created_unbond(session, node, owner, created_height):
    return await pending_index_record(
        session,
        node,
        owner,
        lambda record: (
            record["owner"] == owner
            and int(record["created_block"]) == created_height
            and record["kind"] == "self_bond"
        ),
    )


async def anchored_record(session, node, unbond_id, *, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = await query(session, node, f"/get/validators.pending_unbonds:{unbond_id}")
        if record is not None and record.get("unlock_at") is not None:
            return record
        await asyncio.sleep(0.1)
    raise RuntimeError("Automatic successor unbond anchor did not appear")


async def verify_positive_record(runner, session, proof):
    node, owner, unbond_id = runner.nodes[0], proof["owner"], proof["unbond_id"]
    record = await anchored_record(session, node, unbond_id)
    require(
        safe_record(record) == proof["record"], "Policy change shortened or changed captured unbond"
    )
    require(
        record["claimed"] is False and number(record["amount"]) == number(1),
        "The retained positive unbond principal changed",
    )
    indexed = await pending_index_record(
        session,
        node,
        owner,
        lambda entry: entry["owner"] == owner and int(entry["id"]) == unbond_id,
    )
    require(
        int(indexed["id"]) == unbond_id, "Positive unbond is missing from its owner pending index"
    )
    return {"record": safe_record(record), "owner_pending_index_verified": True}


async def verify_positive_unbond_retained(runner, session, proof):
    verified = await verify_positive_record(runner, session, proof)
    after = await financial_state(session, runner.nodes[0], proof["owner"])
    financial = validate_locked_finances(proof["financial"]["before"], after, verified["record"], 1)
    return {**verified, "financial": financial}


async def early_claim(runner, session, unbond_id):
    result = await runner.submit_contract_call(
        session,
        wallet=runner.founder_wallet,
        node_index=0,
        contract="validators",
        function="claim_unbond",
        kwargs={"unbond_id": unbond_id},
        label="positive-unbond-early-claim",
        expect_success=False,
        expected_message_fragment="locked",
    )
    require(
        result.get("success") is False and result.get("height") is not None,
        "Early claim must have an actual failed block receipt",
    )
    return result


async def positive_unbond_rehearsal(runner, session):
    node, owner = runner.nodes[0], runner.founder_wallet.public_key
    days = int(await query(session, node, "/get/validators.config:unbonding_period_days"))
    require(days > 0, "Positive unbond rehearsal requires the original positive period")
    await runner.fund_wallets(session, runner.validator_wallets[1:4], amount=100_000)
    before = await financial_state(session, node, owner)
    receipts = []
    for contract, function, kwargs in [
        ("currency", "approve", {"amount": 1, "to": "validators"}),
        ("validators", "bond_self", {"amount": 1}),
        ("validators", "unbond_self", {"amount": 1}),
    ]:
        receipts.append(
            await runner.submit_contract_call(
                session,
                wallet=runner.founder_wallet,
                node_index=0,
                contract=contract,
                function=function,
                kwargs=kwargs,
                label="positive-unbond-" + function,
            )
        )
    created_height = int(receipts[-1]["height"])
    await wait_height(session, node, created_height + 1)
    record = await created_unbond(session, node, owner, created_height)
    unbond_id = int(record["id"])
    record = await anchored_record(session, node, unbond_id)
    block, successor = await asyncio.gather(
        *(rpc(session, node, "block", height=h) for h in (created_height, created_height + 1))
    )
    anchor = validate_anchor(record, created_height, successor["block"]["header"], days)
    denied = await early_claim(runner, session, unbond_id)
    proof = {
        "owner": owner,
        "unbond_id": unbond_id,
        "record": safe_record(record),
        "anchor": anchor,
        "creation_header": block["block"]["header"],
        "anchor_header": successor["block"]["header"],
        "receipts": [safe_receipt(item) for item in receipts],
        "early_claim": safe_receipt(denied),
        "original_policy": {"unbonding_period_days": days},
        "financial": validate_locked_finances(
            before, await financial_state(session, node, owner), record, 1
        ),
    }
    try:
        zero = await governance_vote(
            runner, session, "update_policy", {"unbonding_period_days": 0}, "positive-unbond-zero"
        )
        require(
            await query(session, node, "/get/validators.config:unbonding_period_days") == 0,
            "Temporary zero-period policy did not commit",
        )
        proof["zero_policy_vote"] = zero
        proof["zero_policy_claim"] = safe_receipt(await early_claim(runner, session, unbond_id))
        proof["zero_policy_retained"] = await verify_positive_unbond_retained(
            runner, session, proof
        )
    finally:
        proof["restore_policy_vote"] = await governance_vote(
            runner,
            session,
            "update_policy",
            {"unbonding_period_days": days},
            "positive-unbond-restore",
        )
        restored = int(await query(session, node, "/get/validators.config:unbonding_period_days"))
        require(restored == days, "Original positive unbond policy was not restored")
        proof["restored_policy"] = {"unbonding_period_days": restored}
    proof["restored_retained"] = await verify_positive_unbond_retained(runner, session, proof)
    return normalize_value(proof)


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)


def rebase_test_evidence_window(genesis, founder_private_key, private_dir):
    from contracting.storage.driver import Driver
    from xian.evidence_policy import EVIDENCE_POLICY_KEY, EvidencePolicy
    from xian.execution_policy import VM_EXECUTION_MODE
    from xian.genesis_builder import build_cometbft_genesis, finalize_genesis_block

    old = genesis["abci_genesis"]
    with tempfile.TemporaryDirectory(dir=private_dir) as temporary:
        with Driver(vm_profile=VM_EXECUTION_MODE, storage_home=Path(temporary)) as driver:
            for entry in old["genesis"]:
                driver.set(entry["key"], entry["value"])
            policy = EvidencePolicy.from_state(driver.get(EVIDENCE_POLICY_KEY))
            driver.set(
                EVIDENCE_POLICY_KEY,
                replace(policy, max_age_num_blocks=TEST_BASE_BLOCK_WINDOW).to_state(),
            )
            rebuilt = finalize_genesis_block(
                driver,
                founder_private_key=founder_private_key,
                number=old["number"],
                nanos=old["nanos"],
            )
    return build_cometbft_genesis(
        chain_id=genesis["chain_id"],
        abci_genesis=rebuilt,
        validators=genesis["validators"],
        genesis_time=genesis["genesis_time"],
    )


def prepare_fixture(output, image):
    from xian.fee_policy import resolve_chain_fee_policy
    from xian.genesis_builder import build_local_network_genesis
    from xian.node_setup import (
        NodeConfigOptions,
        build_node_key,
        generate_validator_material,
        materialize_cometbft_home,
        render_node_configs,
    )

    require(not output.exists(), "Preflight output must be a fresh isolated directory")
    output.mkdir(parents=True, mode=0o700)
    (output / "private").mkdir(mode=0o700)
    prefix = "xian-idle-unbond-" + uuid.uuid4().hex[:12]
    materials = [generate_validator_material() for _ in range(3)]
    peers = [build_node_key() for _ in materials]
    nodes = [
        {
            "index": index,
            "account": material["validator_public_key_hex"],
            "address": material["priv_validator_key"]["address"],
            "container": prefix + "-node-" + str(index),
            "home": output / "private" / ("node-" + str(index)),
            "node_id": peers[index]["node_id"].lower(),
        }
        for index, material in enumerate(materials)
    ]
    founder = Wallet(private_key=materials[0]["validator_private_key_hex"])
    old_time = (datetime.now(UTC) - timedelta(days=STALE_DAYS)).isoformat().replace("+00:00", "Z")
    validators = [
        {
            "account_public_key": node["account"],
            "name": node["container"],
            "power": 10,
            "priv_validator_key": material["priv_validator_key"],
        }
        for node, material in zip(nodes, materials, strict=True)
    ]
    genesis = build_local_network_genesis(
        chain_id=prefix,
        founder_private_key=founder.private_key,
        validators=validators,
        contracts_dir=Path(__file__).resolve().parents[2] / "xian-configs/contracts",
        network="local",
        genesis_time=old_time,
        runtime_features={"zk": False},
        fee_policy={"tx_fee_mode": "paid_metered", "max_tx_chi": 200000, "max_block_chi": 500000},
        validator_constructor_overrides={"unbonding_period_days": 14},
    )
    genesis = rebase_test_evidence_window(genesis, founder.private_key, output / "private")
    policy = resolve_chain_fee_policy(committed_value=None, genesis=genesis)
    policy.validate_tx({"payload": {"chi_supplied": PREFLIGHT_TX_CHI}})
    policy.validate_block_total(3 * PREFLIGHT_TX_CHI)
    for node, material, peer in zip(nodes, materials, peers, strict=True):
        options = NodeConfigOptions(
            moniker=node["container"],
            block_policy_mode="periodic",
            block_policy_interval="1s",
            p2p_persistent_peers=tuple(
                other["node_id"] + "@" + other["container"] + ":26656"
                for other in nodes
                if other is not node
            ),
            p2p_external_address=node["container"] + ":26656",
            bds_enabled=False,
        )
        configs = render_node_configs(options=options)
        configs["cometbft"]["p2p"].update(addr_book_strict=False, allow_duplicate_ip=True)
        configs["cometbft"]["consensus"].update(
            timeout_propose="2s",
            timeout_prevote="500ms",
            timeout_precommit="500ms",
            timeout_commit="8s",
            skip_timeout_commit=False,
        )
        materialize_cometbft_home(
            home=node["home"],
            config=configs["cometbft"],
            xian_config=configs["xian"],
            genesis=genesis,
            priv_validator_key=material["priv_validator_key"],
            node_key=peer,
        )
    low = next(node for node in nodes if node["address"] != min(n["address"] for n in nodes))
    return SimpleNamespace(
        nodes=nodes,
        low=low,
        founder=founder,
        network=prefix,
        genesis=genesis,
        genesis_nanos=timestamp_nanoseconds(old_time),
        image=image,
        output=output,
        fee_policy=policy.to_state(),
        running={},
    )


async def poll(operation, predicate, label, *, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = await operation()
        except Exception:  # RPC may not yet expose an uncommitted height.
            value = None
        if value is not None and predicate(value):
            return value
        await asyncio.sleep(0.1)
    raise RuntimeError(label + " did not become observable before its deadline")


def prepared_setup(fixture):
    calls = [
        ("currency", "approve", {"amount": 1, "to": "validators"}),
        ("validators", "bond_self", {"amount": 1}),
        ("validators", "unbond_self", {"amount": 1}),
        ("validators", "claim_unbond", {"unbond_id": 1}),
    ]
    return [
        tr.prepare_transaction(
            tr.create_tx(
                {
                    "chain_id": fixture.genesis["chain_id"],
                    "sender": fixture.founder.public_key,
                    "nonce": nonce,
                    "contract": contract,
                    "function": function,
                    "kwargs": kwargs,
                    "chi_supplied": PREFLIGHT_TX_CHI,
                },
                fixture.founder,
                vm_profile="xian_vm_v2",
            )
        )
        for nonce, (contract, function, kwargs) in enumerate(calls)
    ]


async def start_fixture_node(fixture, node, created):
    running = await create_observer(
        node["home"], node["container"], fixture.image, fixture.network, node["container"]
    )
    created.append(node)
    running.account_public_key = node["account"]
    running.abci_container = node["container"]
    fixture.running[node["index"]] = running
    await poll(lambda: rpc(fixture.session, running, "status"), lambda _: True, "Node RPC")
    return running


def dump_round_step(dump):
    state = dump["round_state"]
    return "/".join(str(int(state[key])) for key in ("height", "round", "step"))


async def initial_precondition(fixture, low):
    actual_policy = await query(fixture.session, low, "/get/__fee_policy")
    require(actual_policy == fixture.fee_policy, "Actual InitChain fee caps differ from genesis")
    require(
        PREFLIGHT_TX_CHI <= int(actual_policy["max_tx_chi"])
        and 3 * PREFLIGHT_TX_CHI <= int(actual_policy["max_block_chi"]),
        "The three H1 transactions exceed actual chain chi caps",
    )
    powers = {
        node["account"]: int(
            await query(fixture.session, low, "/get/validators.powers:" + node["account"])
        )
        for node in fixture.nodes
    }
    # /validators is restricted to committed heights and cannot serve H1 yet.
    # Stock's full RoundState gives the current set, proposer and votes atomically.
    dump = await poll(
        lambda: rpc(fixture.session, low, "dump_consensus_state"),
        lambda result: dump_round_step(result) == INITIAL_PREVOTE_STEP,
        "Actual LOW round0 prevote",
    )
    state = dump["round_state"]
    native = state["validators"]
    consensus = {
        "round_state": {
            "height/round/step": dump_round_step(dump),
            "proposer": native["proposer"],
            "height_vote_set": state["votes"],
        }
    }
    return validate_initial_state(consensus, native, powers, fixture.nodes, fixture.low)


async def require_mempool(session, node, transactions):
    result = await rpc(session, node, "unconfirmed_txs", limit=100)
    hashes = {
        hashlib.sha256(base64.b64decode(item, validate=True)).hexdigest().upper()
        for item in result.get("txs") or []
    }
    wanted = {tx.tx_hash for tx in transactions}
    require(hashes == wanted, "Signed setup transactions are not all in the lone node mempool")
    return sorted(hashes)


def execution_result(receipt):
    data = receipt["tx_result"]["data"]
    if isinstance(data, dict):
        return data
    return decode(base64.b64decode(data, validate=True).decode(), vm_profile="xian_vm_v2")


def checked_h1_unbond(receipts):
    require(
        all(int(item["height"]) == 1 and int(item["tx_result"]["code"]) == 0 for item in receipts),
        "All approve/bond/unbond transactions must actually execute at H1",
    )
    record = execution_result(receipts[-1])["result"]
    require(record["unlock_at"] is None, "Positive H1 unbond was not deferred")
    require(
        int(record["id"]) == 1 and int(record["created_block"]) == 1,
        "Stale-genesis fixture did not create the first unbond at H1",
    )
    require(
        int(record["unbonding_period_days"]) == 14, "H1 unbond did not capture its positive period"
    )
    return record


def checked_h3_claim(receipt):
    require(int(receipt["height"]) == 3, "Early claim did not actually execute at H3")
    require(
        int(receipt["tx_result"]["code"]) != 0, "Stale-genesis early claim unexpectedly paid out"
    )
    execution = execution_result(receipt)
    require(
        "locked" in str(execution.get("result", "")).lower(), "Claim failed for an unrelated reason"
    )
    return {"height": 3, "tx_hash": receipt["hash"], "success": False, "immature": True}


async def transaction_receipt(session, node, tx):
    return await poll(
        lambda: rpc(session, node, "tx", hash="0x" + tx.tx_hash),
        lambda _: True,
        "Finalized transaction",
    )


async def block_at(session, node, height):
    return await poll(
        lambda: rpc(session, node, "block", height=height), lambda _: True, "Committed block"
    )


async def three_node_agreement(session, nodes, *, height=4, results_height=3):
    await asyncio.gather(*(wait_height(session, node, height) for node in nodes))
    blocks = [await rpc(session, node, "block", height=height) for node in nodes]
    identifiers = {
        (block["block_id"]["hash"], block["block"]["header"]["app_hash"]) for block in blocks
    }
    require(len(identifiers) == 1, "Stale-genesis peers diverged in committed block/root")
    results = [await rpc(session, node, "block_results", height=results_height) for node in nodes]
    canonical = [json.dumps(result, sort_keys=True) for result in results]
    require(len(set(canonical)) == 1, "Stale-genesis peers produced different claim receipts")
    block_hash, app_hash = next(iter(identifiers))
    return {
        "height": height,
        "block_hash": block_hash,
        "app_hash": app_hash,
        "claim_results_sha256": hashlib.sha256(canonical[0].encode()).hexdigest(),
    }


async def security_checkpoint_proof(session, node, anchor_header):
    async def capture():
        state = await retention_sample(session, node)
        require(state["height"] >= 4, "Security proof requires the committed post-claim state")
        native = await rpc(session, node, "consensus_params", height=state["height"] + 1)
        return state, native

    state, native = await poll(capture, lambda _: True, "Security checkpoint parameters")
    schedule = state["schedule"]
    require(
        schedule["previous_security_checkpoint_height"] == 0
        and schedule["security_checkpoint_height"] == 2,
        "Stale H1 to fresh H2 did not retain the exact prior security epoch",
    )
    require(
        schedule["security_checkpoint_nanos"] == timestamp_nanoseconds(anchor_header["time"]),
        "Security checkpoint clock differs from actual H2",
    )
    require(
        native["consensus_params"]["evidence"] == state["evidence"],
        "Native evidence window differs from the committed coupled policy",
    )
    return {
        "height": state["height"],
        "schedule": schedule,
        "effective_evidence": state["evidence"],
        "block_cutoff_height": state["block_cutoff_height"],
    }


def validate_coupled_evidence_ages(state, genesis_nanos):
    nominal = state["schedule"]["max_age_num_blocks"]
    age_blocks = state["height"] - 1
    age_nanos = state["last_block_nanos"] - genesis_nanos
    require(
        nominal == TEST_BASE_BLOCK_WINDOW,
        "Coupled evidence fixture has the wrong nominal block window",
    )
    require(age_blocks > nominal, "H1 evidence has not exceeded the nominal block-age window")
    require(
        age_nanos > int(state["evidence"]["max_age_duration"]),
        "H1 evidence has not exceeded nominal time age",
    )
    require(
        age_blocks <= int(state["evidence"]["max_age_num_blocks"]),
        "Effective coupled block window no longer protects H1",
    )
    return {
        "height": state["height"],
        "age_blocks": age_blocks,
        "age_nanos": age_nanos,
        "nominal_block_window": nominal,
        "effective_evidence": state["evidence"],
    }


async def final_progress_probe(fixture, reporter):
    await wait_height(fixture.session, reporter, 8)
    tx = tr.prepare_transaction(
        tr.create_tx(
            {
                "chain_id": fixture.genesis["chain_id"],
                "sender": fixture.founder.public_key,
                "nonce": 4,
                "contract": "currency",
                "function": "approve",
                "kwargs": {"amount": 0, "to": fixture.founder.public_key},
                "chi_supplied": PREFLIGHT_TX_CHI,
            },
            fixture.founder,
            vm_profile="xian_vm_v2",
        )
    )
    await broadcast(fixture.session, reporter, tx)
    result = await transaction_receipt(fixture.session, reporter, tx)
    require(int(result["tx_result"]["code"]) == 0, "Final pre-evidence progress probe failed")
    target = int(result["height"]) + 1
    state = await poll(
        lambda: retention_sample(fixture.session, reporter),
        lambda item: item["height"] >= target,
        "Final paid probe successor",
    )
    return state, {"height": int(result["height"]), "tx_hash": tx.tx_hash, "success": True}


def evidence_adapter(fixture):
    protocol = protocol_module()
    helper = object.__new__(protocol.ProtocolSafetyRunner)
    helper.nodes = [fixture.running[index] for index in range(3)]
    helper.network = {"chain_id": fixture.genesis["chain_id"]}
    helper.load_priv_validator_key = lambda index: json.loads(
        (fixture.nodes[index]["home"] / "config/priv_validator_key.json").read_text()
    )
    return helper


async def slash_checkpoint(session, node, owner):
    financial = await financial_state(session, node, owner)
    dao = str(number(await query(session, node, "/get/currency.balances:dao")))
    return {"financial": financial, "dao_balance": dao}


def validate_pending_slash(before, after, record):
    expected = number("0.05")
    require(
        number(before["financial"]["custody"]) - number(after["financial"]["custody"]) == expected,
        "Historical evidence did not slash exact pending principal",
    )
    require(
        number(after["dao_balance"]) - number(before["dao_balance"]) == expected,
        "Historical evidence DAO credit differs from the exact slash",
    )
    require(
        number(record["amount"]) == number("0.95") and record["claimed"] is False,
        "Historical evidence did not preserve remaining locked principal",
    )
    require(
        number(after["financial"]["pending_total"]) == number("0.95")
        and after["financial"]["heap_size"] == 1
        and number(after["financial"]["self_bond"]) == number(0),
        "Pending slash financial/index state differs",
    )
    return {
        "before": before,
        "after": after,
        "slashed": str(expected),
        "remaining_record": safe_record(record),
    }


async def coupled_evidence_rehearsal(fixture, reporter):
    state, probe = await final_progress_probe(fixture, reporter)
    ages = validate_coupled_evidence_ages(state, fixture.genesis_nanos)
    native = await rpc(fixture.session, reporter, "consensus_params", height=state["height"] + 1)
    require(
        native["consensus_params"]["evidence"] == state["evidence"],
        "Native coupled evidence parameters disagree",
    )
    owner = fixture.founder.public_key
    for key, wanted in (
        ("duplicate_vote_slash_bps", 500),
        ("slash_destination", "dao"),
        ("duplicate_vote_jail", True),
    ):
        require(
            await query(fixture.session, reporter, "/get/validators.config:" + key) == wanted,
            "Canonical duplicate-vote penalty precondition differs",
        )
    before = await slash_checkpoint(fixture.session, reporter, owner)
    helper = evidence_adapter(fixture)
    evidence = await helper.broadcast_duplicate_vote_evidence(
        fixture.session,
        reporter_node_index=fixture.low["index"],
        target_node_index=0,
        evidence_height=1,
    )
    response = evidence["response"]
    require(
        "error" not in response and response.get("result", {}).get("hash"),
        "Native Comet rejected valid protected H1 evidence",
    )
    included = await find_evidence_block(fixture.session, reporter, state["height"], 1)
    validator = await poll(
        lambda: query(fixture.session, reporter, "/validators_validator/" + owner),
        lambda record: record.get("jailed") is True and record.get("pending_slash_count") == 0,
        "Committed historical penalty completion",
    )
    nodes = [fixture.running[index] for index in range(3)]
    consensus = await three_node_agreement(
        fixture.session, nodes, height=included + 3, results_height=included
    )
    expected = {node["address"]: 10 for node in fixture.nodes[1:]}
    sets = [
        await rpc(fixture.session, node, "validators", height=included + 3, per_page=100)
        for node in nodes
    ]
    require(
        all(
            {entry["address"]: int(entry["voting_power"]) for entry in group["validators"]}
            == expected
            for group in sets
        ),
        "Historical penalty did not complete its delayed native validator update",
    )
    record = await anchored_record(fixture.session, reporter, 1)
    after = await slash_checkpoint(fixture.session, reporter, owner)
    anchor_block = await rpc(fixture.session, reporter, "block", height=2)
    remaining_anchor = validate_anchor(record, 1, anchor_block["block"]["header"], 14)
    await pending_index_record(
        fixture.session,
        reporter,
        owner,
        lambda entry: entry["owner"] == owner and int(entry["id"]) == 1,
    )
    return {
        "progress_probe": probe,
        "ages": ages,
        "evidence_hash": response["result"]["hash"],
        "included_height": included,
        "jailed": validator["jailed"],
        "consensus": consensus,
        "active_native_powers": expected,
        "remaining_anchor": remaining_anchor,
        "owner_pending_index_verified": True,
        "financial": validate_pending_slash(before, after, record),
    }


class PreflightReplayRunner:
    """Adapt only the already-owned runtime for the common private exporter."""

    def __init__(self, fixture):
        self.nodes = [fixture.running[index] for index in range(3)]
        self.output_dir = fixture.output / "private"
        self.network = {"chain_id": fixture.genesis["chain_id"]}

    async def stop_node_runtime(self, node):
        await stop_backup_source(node.abci_container)

    async def start_node_runtime(self, session, node):
        await command("docker", "start", node.abci_container)
        address = await observer_address(node.abci_container, node.moniker)
        node.rpc_url = address.rpc_url
        await poll(lambda: rpc(session, node, "status"), lambda _: True, "Replay donor RPC")


async def capture_preflight_corpus(fixture):
    from localnet_replay_corpus import export_corpus

    runner = PreflightReplayRunner(fixture)
    corpus = await export_corpus(
        runner, fixture.session, node=runner.nodes[0], home=fixture.nodes[0]["home"]
    )
    corpus.chmod(0o700)
    for filename in ("blocks.tar.gz", "manifest.json"):
        (corpus / filename).chmod(0o600)
    manifest = json.loads((corpus / "manifest.json").read_text())
    return {
        "chain_id": manifest["chain_id"],
        "height": int(manifest["height"]),
        "app_hash": manifest["app_hash"],
        "archive_sha256": hashlib.sha256((corpus / "blocks.tar.gz").read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256((corpus / "manifest.json").read_bytes()).hexdigest(),
        "replay_performed": False,
    }


async def execute_stale_case(fixture, created):
    low = await start_fixture_node(fixture, fixture.low, created)
    precondition = await initial_precondition(fixture, low)
    before = await financial_state(fixture.session, low, fixture.founder.public_key)
    transactions = prepared_setup(fixture)
    for tx in transactions[:3]:
        await broadcast(fixture.session, low, tx)
    queued = await require_mempool(fixture.session, low, transactions[:3])
    peers = [
        await start_fixture_node(fixture, node, created)
        for node in fixture.nodes
        if node is not fixture.low
    ]
    await block_at(fixture.session, low, 2)
    # H2's observed immutable block precedes this enqueue. The stock commit
    # timeout gives the transaction a real H3 proposal window, checked below.
    await broadcast(fixture.session, low, transactions[3])
    receipts = [await transaction_receipt(fixture.session, low, tx) for tx in transactions]
    commit = await rpc(fixture.session, low, "commit", height=1)
    commit_round = int(commit["signed_header"]["commit"]["round"])
    require(commit_round > 0, "The empty round0 proposal unexpectedly committed")
    initial_record = checked_h1_unbond(receipts[:3])
    denial = checked_h3_claim(receipts[3])
    await asyncio.gather(*(wait_height(fixture.session, node, 3) for node in [low, *peers]))
    blocks = [await block_at(fixture.session, low, height) for height in [1, 2, 3]]
    headers = [block["block"]["header"] for block in blocks]
    clocks = validate_stale_headers(
        headers, fixture.genesis_nanos, fixture.wall_start, time.time_ns()
    )
    record = await anchored_record(fixture.session, low, 1)
    anchor = validate_anchor(record, 1, headers[1], 14)
    after = await financial_state(fixture.session, low, fixture.founder.public_key)
    financial = validate_locked_finances(before, after, record, 1)
    consensus = await three_node_agreement(fixture.session, [low, *peers])
    security = await security_checkpoint_proof(fixture.session, low, headers[1])
    coupled = await coupled_evidence_rehearsal(fixture, low)
    corpus = await capture_preflight_corpus(fixture)
    return {
        "ok": True,
        "kind": "stale-genesis-unbond",
        "consensus": consensus,
        "security_checkpoint": security,
        "coupled_evidence": coupled,
        "replay_corpus": corpus,
        "precondition": precondition,
        "queued_transaction_hashes": queued,
        "h1_commit_round": commit_round,
        "fee_policy": fixture.fee_policy,
        "clocks": clocks,
        "initial_record": safe_record(initial_record),
        "record": safe_record(record),
        "anchor": anchor,
        "early_claim": denial,
        "financial": financial,
    }


async def cleanup_fixture(fixture, created):
    failures = []
    for node in reversed(created):
        try:
            logs = await command("docker", "logs", node["container"], combine_output=True)
            private_json(
                fixture.output / "private" / (node["container"] + ".log.json"), {"log": logs}
            )
        except Exception:
            failures.append("logs")
        try:
            await command("docker", "rm", "-f", "-v", node["container"])
        except Exception:
            failures.append("container")
    try:
        await command("docker", "network", "rm", fixture.network)
    except Exception:
        failures.append("network")
    require(not failures, "Owned preflight cleanup did not finish completely")


async def stale_unbond_preflight(*, image, output_dir):
    output = Path(output_dir)
    fixture = prepare_fixture(output, image)
    fixture.wall_start = time.time_ns()
    created = []
    await command("docker", "network", "create", fixture.network)
    try:
        async with aiohttp.ClientSession() as session:
            fixture.session = session
            proof = await execute_stale_case(fixture, created)
        private_json(output / "public-proof.json", normalize_value(proof))
        return proof
    finally:
        await cleanup_fixture(fixture, created)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    proof = asyncio.run(stale_unbond_preflight(image=args.image, output_dir=args.output_dir))
    print(json.dumps({"ok": proof["ok"], "proof": str(args.output_dir / "public-proof.json")}))


if __name__ == "__main__":
    main()
