"""Consensus checks for dictionary equality, failed-key rollback and recovery."""

from localnet_chain_checks import agreement, deploy, receipt, require
from localnet_runtime_safety_checks import failed_execution, successful, uniform_state

SOURCE = """result = Variable()
progress = Variable()
Attempt = LogEvent('Attempt', {'value': int})
@construct
def seed():
    progress.set(0)
@export
def probe():
    values = {True: 1, 1: 2}
    values[1] = 3
    pairs = dict([(True, 1), (1, 2)])
    exact = {9007199254740993: 1, float('9007199254740992'): 2}
    items = [[1], [2]]
    items.remove([True])
    answer = [len(values), values[True], len(pairs), pairs[1], len(exact), items]
    result.set(answer)
    progress.set(progress.get() + 1)
    return answer
@export
def invalid():
    progress.set(999)
    Attempt({'value': 999})
    values = {}
    values[([],)] = 1
    return len(values)
"""


async def matching_receipts(runner, session, submission, *, success):
    outcomes = [
        (await receipt(session, node, submission, success=success))["tx_result"]
        for node in runner.nodes
    ]
    require(all(item == outcomes[0] for item in outcomes), "Dictionary receipts disagree")


async def dictionary_checks(runner, session):
    name = await deploy(runner, session, "dictionary-compatibility", SOURCE)
    expected = [1, 3, 1, 2, 2, [[2]]]
    transactions = []
    async with runner.client(runner.founder_wallet, 0, session) as client:
        for function, progress in [("probe", 1), ("invalid", 1), ("probe", 2)]:
            submission = await client.send_tx(name, function, {}, chi=100_000, wait_for_tx=True)
            if function == "invalid":
                failed = failed_execution(submission, function, fragment="unhashable")
                require(not failed.get("events"), "Invalid dictionary key retained an event")
            else:
                successful(submission, function)
            await matching_receipts(runner, session, submission, success=function != "invalid")
            await agreement(session, runner.nodes, window=1)
            await uniform_state(
                runner,
                session,
                {
                    f"{name}.result": expected,
                    f"{name}.progress": progress,
                },
            )
            transactions.append(submission.tx_hash)
    return {
        "contract": name,
        "transactions": transactions,
        "consensus": await agreement(session, runner.nodes),
    }
