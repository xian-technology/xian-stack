"""Execute authored application flows and their failure paths on every validator."""

import asyncio
import copy
import json
from uuid import uuid4

from contract_suite_cases import (
    ROLES,
    Case,
    cases,
    constructors,
    initial_state,
    select_state,
    sources,
)
from localnet_chain_checks import agreement, receipt, require, rpc, wallet
from localnet_e2e_support import decode_state_response, normalize_value
from localnet_rng_checks import transaction_random_value
from localnet_runtime_safety_checks import failed_execution, successful
from xian_runtime_types.encoding import decode_wire_numbers, normalize_wire_numbers


async def query(session, node, path):
    response = (await rpc(session, node, "abci_query", path=json.dumps(path)))["response"]
    require(int(response.get("code", 0)) == 0, f"query {path} failed: {response}")
    return decode_state_response(response)


def check_execution(case, submission, contracts):
    require(submission.accepted, f"{case.label}: CheckTx rejected: {submission.message}")
    if case.error is not None:
        execution = failed_execution(submission, case.label, fragment=case.error)
        require(not execution["events"], f"{case.label}: failed call retained events")
        prefixes = tuple(name + "." for name in contracts)
        require(
            not any(item["key"].startswith(prefixes) for item in execution["state"]),
            f"{case.label}: failed call retained authored state",
        )
    else:
        execution = successful(submission, case.label)
        require(
            normalize_wire_numbers(execution["result"]) == normalize_wire_numbers(case.result),
            f"{case.label}: wrong result: {execution['result']!r}",
        )
        events = [
            dict(
                contract=item["contract"],
                event=item["event"],
                data={**item["data"], **item["data_indexed"]},
            )
            for item in execution["events"]
        ]
        # Resumed runs may carry raw event tags, while the V2 SDK returns typed values.
        event_values = decode_wire_numbers(normalize_wire_numbers(events))
        require(
            normalize_value(event_values) == list(case.events),
            f"{case.label}: wrong events: {events!r}",
        )
    require(execution["chi_used"] > 0, f"{case.label}: work was not charged")
    return execution


async def matching_receipts(runner, session, submission, *, success):
    results = await asyncio.gather(
        *(receipt(session, node, submission, success=success) for node in runner.nodes)
    )
    outcomes = [item["tx_result"] for item in results]
    require(all(item == outcomes[0] for item in outcomes), "Contract suite receipts disagree")
    return int(results[0].get("height", 0))


async def check_state(runner, session, expected, label):
    semaphore = asyncio.Semaphore(20)

    async def check(node, selector, value):
        async with semaphore:
            actual = await query(session, node, f"/get/{selector.split('/')[0]}")
        actual = select_state(actual, selector)
        require(
            normalize_value(actual) == normalize_value(value),
            f"{label}: {node.moniker}: state {selector}: {actual!r} != {value!r}",
        )

    await asyncio.gather(
        *(check(node, key, value) for node in runner.nodes for key, value in expected.items())
    )


# The suite's largest contract deploys for ~8,400 chi on the rev-11 VM.
SUITE_DEPLOY_CHI = 50_000


async def deploy_suite(runner, session, names, accounts):
    transactions = {}
    arguments = constructors(accounts)
    async with runner.client(runner.founder_wallet, 0, session) as client:
        for role, source in sources(names).items():
            submission = await client.deploy_contract(
                name=names[role],
                source=source,
                args=arguments.get(role, {}),
                chi=SUITE_DEPLOY_CHI,
                wait_for_tx=True,
            )
            successful(submission, f"deploy-{role}")
            await matching_receipts(runner, session, submission, success=True)
            transactions[role] = submission.tx_hash
    return transactions


async def transaction_random_checks(runner, session, names, signer, expected):
    transactions = []
    for index in range(2):
        async with runner.client(signer, index, session) as client:
            submitted = await client.send_tx(
                names["commitments"], "transaction_random", {}, chi=200_000, wait_for_tx=True
            )
        successful(submitted, "transaction-rng")
        height = await matching_receipts(runner, session, submitted, success=True)
        block = await rpc(session, runner.nodes[index], "block", height=height)
        # The SDK exposes the signature in memory; never retain it or signed payloads
        # in phase evidence. The node seeds from nanos + signature, not the tx hash.
        signature = submitted.receipt.transaction["metadata"]["signature"]
        value = transaction_random_value(block, signature)
        case = Case(
            f"transaction-rng-{index}",
            names["commitments"],
            "transaction_random",
            result=value,
            state={f"{names['commitments']}.result": value},
        )
        execution = check_execution(case, submitted, names.values())
        expected.update(case.state)
        await check_state(runner, session, expected, case.label)
        transactions.append(
            {
                "case": case.label,
                "actor": "alice",
                "node_index": index,
                "tx_hash": submitted.tx_hash,
                "height": height,
                "success": True,
                "chi_used": execution["chi_used"],
            }
        )
    return transactions


async def contract_suite_checks(runner, session):
    # A phase retry uses fresh application state, even after a partially committed attempt.
    # Names are persisted in the phase report; replay consumes the actual transactions.
    prefix = "con_suite_" + uuid4().hex[:12]
    names = {role: f"{prefix}_{role}" for role in ROLES}
    wallets = {
        actor: wallet(runner, "contract-suite-" + actor) for actor in ("alice", "bob", "carol")
    }
    accounts = {actor: value.public_key for actor, value in wallets.items()}
    funding = await runner.fund_wallets(session, list(wallets.values()), amount=100_000)
    deployments = await deploy_suite(runner, session, names, accounts)
    scenarios = cases(names, accounts, wallets["alice"].sign_msg("contract-suite authorization"))
    expected = initial_state(names, accounts, scenarios)
    await check_state(runner, session, expected, "constructors")
    transactions = []
    for index, case in enumerate(scenarios):
        node_index = index % len(runner.nodes)
        async with runner.client(wallets[case.actor], node_index, session) as client:
            submission = await client.send_tx(
                case.contract, case.function, case.kwargs, chi=case.chi, wait_for_tx=True
            )
        execution = check_execution(case, submission, names.values())
        height = await matching_receipts(runner, session, submission, success=case.error is None)
        if case.error is None:
            expected.update(copy.deepcopy(case.state))
        await check_state(runner, session, expected, case.label)
        transactions.append(
            {
                "case": case.label,
                "actor": case.actor,
                "node_index": node_index,
                "tx_hash": submission.tx_hash,
                "height": height,
                "success": case.error is None,
                "chi_used": execution["chi_used"],
            }
        )
    transactions.extend(
        await transaction_random_checks(runner, session, names, wallets["alice"], expected)
    )
    return {
        "contracts": names,
        "accounts": accounts,
        "funding": funding,
        "deployments": deployments,
        "scenario_count": len(transactions),
        "success_count": sum(case.error is None for case in scenarios) + 2,
        "expected_failure_count": sum(case.error is not None for case in scenarios),
        "transactions": transactions,
        "final_state": expected,
        "consensus": await agreement(session, runner.nodes),
    }
