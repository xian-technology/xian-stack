"""Live regressions for contract authority, shared metering, and stored values."""

from localnet_chain_checks import (
    agreement,
    broadcast,
    deploy,
    prepared,
    query,
    require,
    rpc,
    wallet,
)
from localnet_e2e_support import short_hash

CHILD_SOURCE = """value = Variable()
@construct
def seed():
    value.set(0)
@export
def bump():
    value.set(value.get() + 1)
    return value.get()
"""

FACTORY_SOURCE = """import submission
@export
def spawn(name: str, code: str):
    submission.submit_contract(name=name, code=code, owner=ctx.this)
@export
def bump(target: str):
    child = importlib.import_module(target)
    return child.bump()
@export
def transfer_owner(target: str, replacement: str):
    submission.change_owner(contract=target, new_owner=replacement)
@export
def spawn_pair(prefix: str, code: str):
    submission.submit_contract(name=prefix + '_one', code=code)
    submission.submit_contract(name=prefix + '_two', code=code)
"""

METADATA_SOURCE = """@export
def owner(target: str, replacement: str):
    Contract.set_owner(target, replacement)
@export
def developer(target: str, replacement: str):
    Contract.set_developer(target, replacement)
@export
def deploy_direct(name: str, code: str):
    Contract.deploy(
        name=name, code=code, owner=None, constructor_args={},
        developer=ctx.this, deployer=ctx.this, initiator=ctx.signer,
    )
"""

METER_SOURCE = """total = Variable()
@construct
def seed():
    value = 0
    while value < 8000:
        value += 1
    total.set(value)
@export
def read():
    return total.get()
"""

STORAGE_SOURCE = """value = Variable()
progress = Variable()
InvalidStore = LogEvent('InvalidStore', {'attempt': str})
@construct
def seed():
    value.set('unchanged')
    progress.set(0)
@export
def invalid_set():
    progress.set(99)
    InvalidStore({'attempt': 'set'})
    value.set(set([1, 2]))
@export
def invalid_frozenset():
    progress.set(99)
    InvalidStore({'attempt': 'frozenset'})
    value.set(frozenset([1, 2]))
@export
def invalid_key():
    progress.set(99)
    InvalidStore({'attempt': 'key'})
    value.set({1: 2})
@export
def recover():
    progress.set(progress.get() + 1)
    return {1: 2}
@export
def return_set():
    return set([1, 2])
"""


def failed_execution(submission, label, *, fragment=None, budget=None):
    require(submission.finalized, f"{label}: failure was not committed")
    require(
        submission.receipt and not submission.receipt.success, f"{label}: unexpectedly succeeded"
    )
    execution = submission.receipt.execution
    require(execution["chi_used"] > 0, f"{label}: failed work was not charged")
    if fragment is not None:
        require(
            fragment in str(execution.get("result", "")).lower(), f"{label}: wrong failure reason"
        )
    if budget is not None:
        require(execution["chi_used"] == budget, f"{label}: did not charge the exhausted budget")
    return execution


def successful(submission, label):
    require(
        submission.finalized and submission.receipt and submission.receipt.success,
        f"{label}: expected successful committed transaction",
    )
    return submission.receipt.execution


async def uniform_state(runner, session, expected):
    for node in runner.nodes:
        for key, value in expected.items():
            actual = await query(session, node, f"/get/{key}")
            require(actual == value, f"{node.moniker}: state {key} changed: {actual!r}")


async def metadata_checks(runner, session, client, factory, child, probe):
    expected = {
        f"{child}.__owner__": factory,
        f"{child}.__developer__": factory,
        f"{child}.value": 1,
    }
    results = {}
    for field in ("owner", "developer"):
        call = await client.send_tx(
            probe,
            field,
            {"target": child, "replacement": runner.founder_wallet.public_key},
            chi=500_000,
            wait_for_tx=True,
        )
        results[field] = failed_execution(
            call, f"unauthorized {field}", fragment="restricted to submission"
        )
        name = f"{probe}_{field}"
        code = (
            f"@construct\ndef seed():\n    Contract.set_{field}({child!r}, 'unauthorized')\n"
            "@export\ndef ready():\n    return True\n"
        )
        attempt = await client.deploy_contract(
            name=name, source=code, chi=500_000, wait_for_tx=True
        )
        results[f"constructor_{field}"] = failed_execution(
            attempt, f"constructor {field}", fragment="restricted to submission"
        )
        require(await client.get_contract_source(name) is None, "Rejected constructor persisted")
        await agreement(session, runner.nodes, window=1)
        await uniform_state(runner, session, expected)
    direct_name = probe + "_direct"
    direct = await client.send_tx(
        probe,
        "deploy_direct",
        {"name": direct_name, "code": CHILD_SOURCE},
        chi=500_000,
        wait_for_tx=True,
    )
    results["direct_deployment"] = failed_execution(
        direct, "direct deployment host call", fragment="restricted to submission"
    )
    await agreement(session, runner.nodes, window=1)
    await uniform_state(runner, session, {f"{direct_name}.__code__": None})
    require(
        await client.get_contract_source(direct_name) is None,
        "Unauthorized host deployment persisted",
    )
    return results


async def authority_checks(runner, session):
    factory = await deploy(runner, session, "runtime-factory", FACTORY_SOURCE)
    child = f"{factory}_owned"
    probe = await deploy(runner, session, "runtime-metadata", METADATA_SOURCE)
    async with runner.client(runner.founder_wallet, 0, session) as client:
        successful(
            await client.send_tx(
                factory,
                "spawn",
                {"name": child, "code": CHILD_SOURCE},
                chi=500_000,
                wait_for_tx=True,
            ),
            "owned child deployment",
        )
        denied = failed_execution(
            await client.send_tx(
                child,
                "bump",
                {},
                chi=100_000,
                wait_for_tx=True,
            ),
            "direct call to contract-owned child",
            fragment="caller is not the owner",
        )
        await agreement(session, runner.nodes, window=1)
        await uniform_state(runner, session, {f"{child}.value": 0})
        successful(
            await client.send_tx(
                factory,
                "bump",
                {"target": child},
                chi=100_000,
                wait_for_tx=True,
            ),
            "owner contract call",
        )
        metadata = await metadata_checks(runner, session, client, factory, child, probe)
        successful(
            await client.send_tx(
                factory,
                "transfer_owner",
                {"target": child, "replacement": runner.founder_wallet.public_key},
                chi=100_000,
                wait_for_tx=True,
            ),
            "authorized owner transfer",
        )
        successful(
            await client.send_tx(child, "bump", {}, chi=100_000, wait_for_tx=True), "new owner call"
        )
        await agreement(session, runner.nodes, window=1)
        await uniform_state(
            runner,
            session,
            {f"{child}.__owner__": runner.founder_wallet.public_key, f"{child}.value": 2},
        )
    return {"factory": factory, "child": child, "denied_call": denied, "metadata": metadata}


async def constructor_budget_checks(runner, session):
    factory = await deploy(runner, session, "runtime-factory", FACTORY_SOURCE)
    prefix = "con_budget_" + short_hash(runner.run_id)
    budget = 5000
    nested = "import submission\n" + METER_SOURCE.replace(
        "    value = 0",
        f"    submission.submit_contract(name={prefix + '_inner'!r}, code=code)\n    value = 0",
    ).replace("def seed():", "def seed(code: str):")
    results = {}
    async with runner.client(runner.founder_wallet, 0, session) as client:
        successful(
            await client.deploy_contract(
                name=prefix + "_single",
                source=METER_SOURCE,
                chi=budget,
                wait_for_tx=True,
            ),
            "single bounded constructor",
        )
        attempts = [
            (
                "serial",
                factory,
                "spawn_pair",
                {"prefix": prefix, "code": METER_SOURCE},
                [prefix + "_one", prefix + "_two"],
            ),
            (
                "nested",
                "submission",
                "submit_contract",
                {
                    "name": prefix + "_outer",
                    "code": nested,
                    "constructor_args": {"code": METER_SOURCE},
                },
                [prefix + "_outer", prefix + "_inner"],
            ),
            (
                "exhausted",
                "submission",
                "submit_contract",
                {"name": prefix + "_failed", "code": METER_SOURCE.replace("8000", "2000000")},
                [prefix + "_failed"],
            ),
        ]
        for label, contract, function, kwargs, absent in attempts:
            submission = await client.send_tx(
                contract, function, kwargs, chi=budget, wait_for_tx=True
            )
            results[label] = failed_execution(
                submission, label, fragment="out of chi", budget=budget
            )
            await agreement(session, runner.nodes, window=1)
            await uniform_state(runner, session, {f"{name}.__code__": None for name in absent})
            for name in absent:
                require(
                    await client.get_contract_source(name) is None, f"{label}: partial deployment"
                )
    return {"budget": budget, "single_contract": prefix + "_single", "failures": results}


async def stored_value_checks(runner, session):
    name = await deploy(runner, session, "runtime-storage-values", STORAGE_SOURCE)
    failures = {}
    async with runner.client(runner.founder_wallet, 0, session) as client:
        for index, function in enumerate(("invalid_set", "invalid_frozenset", "invalid_key")):
            bad = await client.send_tx(name, function, {}, chi=100_000, wait_for_tx=True)
            failures[function] = failed_execution(bad, function)
            require(not failures[function].get("events"), "Invalid stored value retained an event")
            await agreement(session, runner.nodes, window=1)
            await uniform_state(
                runner, session, {f"{name}.value": "unchanged", f"{name}.progress": index}
            )
            good = await client.send_tx(name, "recover", {}, chi=100_000, wait_for_tx=True)
            successful(good, "post-invalid-value recovery")
            require(
                int(good.receipt.raw["result"]["height"])
                > int(bad.receipt.raw["result"]["height"]),
                "Recovery did not finalize in a later block",
            )
            await agreement(session, runner.nodes, window=1)
            await uniform_state(
                runner, session, {f"{name}.value": "unchanged", f"{name}.progress": index + 1}
            )
        successful(
            await client.send_tx(name, "return_set", {}, chi=100_000, wait_for_tx=True),
            "set return value",
        )
    return {
        "contract": name,
        "failures": failures,
        "consensus": await agreement(session, runner.nodes),
    }


async def zero_chi_check(runner, session):
    signer = wallet(runner, "zero-chi-admission")
    await runner.fund_wallets(session, [signer], amount=100)
    tx = prepared(
        runner,
        signer,
        0,
        "currency",
        "transfer",
        {"amount": 1, "to": runner.founder_wallet.public_key},
        chi=0,
    )
    rejected = []
    for node in runner.nodes:
        rejection = await broadcast(session, node, tx, accepted=False)
        # Native static validation rejects zero as an absent required value;
        # Python validation reaches the fee-policy positive-budget check.
        message = rejection.get("log", "").lower()
        require(
            "chi_supplied must be greater than zero" in message
            or "payload key 'chi_supplied' is missing" in message,
            f"{node.moniker}: zero-chi transaction rejected for wrong reason: {rejection}",
        )
        rejected.append({"node": node.moniker, "rejection": rejection})
    # A rejected admission must not reserve or consume nonce zero.
    async with runner.client(signer, 0, session) as client:
        valid = await client.send_tx(
            "currency",
            "transfer",
            {"amount": 1, "to": runner.founder_wallet.public_key},
            chi=100,
            nonce=0,
            wait_for_tx=True,
        )
        successful(valid, "post-zero-chi transfer at nonce zero")
    await agreement(session, runner.nodes, window=1)
    await uniform_state(runner, session, {f"__n.{signer.public_key}:": 0})
    return {"rejected": rejected, "recovery_tx": valid.tx_hash}


REGISTRAR_SOURCE = """import currency
import validators
@export
def register(amount: Any):
    currency.approve(amount=amount, to='validators')
    return validators.register()
"""


async def invalid_registration_check(runner, session):
    name = await deploy(runner, session, "invalid-validator-registrar", REGISTRAR_SOURCE)
    async with runner.client(runner.founder_wallet, 0, session) as client:
        fee = await client.get_state("validators", "registration_fee")
        successful(
            await client.send_tx(
                "currency", "transfer", {"amount": fee, "to": name}, chi=100_000, wait_for_tx=True
            ),
            "fund validator registration probe",
        )
        keys = [
            f"currency.balances:{name}",
            "currency.balances:validators",
            f"validators.pending_registrations:{name}",
            f"validators.holdings:{name}",
            f"currency.approvals:{name}:validators",
        ]
        before = {key: await query(session, runner.nodes[0], f"/get/{key}") for key in keys}
        validators_before = (await rpc(session, runner.nodes[0], "validators"))["validators"]
        submission = await client.send_tx(
            name, "register", {"amount": fee}, chi=500_000, wait_for_tx=True
        )
        failure = failed_execution(
            submission,
            "contract validator registration",
            fragment="validators must register from their own account",
        )
    await agreement(session, runner.nodes, window=1)
    await uniform_state(runner, session, before)
    for node in runner.nodes:
        after = (await rpc(session, node, "validators"))["validators"]
        expected = {(item["address"], item["voting_power"]) for item in validators_before}
        require(
            {(item["address"], item["voting_power"]) for item in after} == expected,
            "Invalid registration changed consensus validators",
        )
    return {"contract": name, "failure": failure, "validator_count": len(validators_before)}
