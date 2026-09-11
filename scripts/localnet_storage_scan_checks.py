"""Consensus regression: persist native and foreign scan order on every validator."""

from localnet_chain_checks import agreement, deploy, query, require

DATA_SOURCE = """
values = Hash()
result = Variable()
@export
def populate(seed: int):
    values['z'] = seed + 5
    values['deleted'] = 99
    values['d'] = seed + 4
    values['b'] = seed + 2
    values['a'] = seed + 1
    values['c'] = seed + 3
    values['deleted'] = None
    scanned = values.all()
    result.set(scanned)
    return scanned
"""


def reader_source(data_contract):
    return f"""
import {data_contract} as data
foreign = ForeignHash(foreign_contract='{data_contract}', foreign_name='values')
result = Variable()
@export
def probe(target: str, seed: int):
    local = data.populate(seed=seed)
    dynamic = ForeignHash(foreign_contract=target, foreign_name='values')
    scanned = [local, foreign.all(), dynamic.all()]
    result.set(scanned)
    return scanned
"""


async def storage_scan_phase(runner, session):
    data = await deploy(runner, session, "scan-data", DATA_SOURCE)
    reader = await deploy(runner, session, "scan-reader", reader_source(data))
    hashes = []
    for seed in range(12):
        async with runner.client(runner.founder_wallet, seed % len(runner.nodes), session) as client:
            submission = await client.send_tx(
                reader, "probe", {"target": data, "seed": seed}, chi=100_000, wait_for_tx=True
            )
            require(
                submission.finalized and submission.receipt.success,
                f"Storage scan transaction failed: {submission.message}",
            )
            hashes.append(submission.tx_hash)
        await agreement(session, runner.nodes, window=1)
        expected = [[seed + offset for offset in range(1, 6)]] * 3
        for node in runner.nodes:
            actual = await query(session, node, f"/get/{reader}.result")
            require(actual == expected, f"Incorrect persisted scan order on {node.moniker}: {actual}")
    return {"transaction_hashes": hashes, "consensus": await agreement(session, runner.nodes)}
