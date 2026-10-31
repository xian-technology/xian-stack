"""Replayable shared-reference, iterator and builtin checks on every validator."""

from localnet_chain_checks import agreement, deploy
from localnet_dictionary_checks import matching_receipts
from localnet_runtime_safety_checks import failed_execution, successful, uniform_state

SOURCE = """result = Variable()
progress = Variable()
ORDER = []
CALLS = []
VALUES = []
Attempt = LogEvent('Attempt', {'value': int})
@construct
def seed():
    progress.set(0)
def receiver():
    ORDER.append('receiver')
    return VALUES
def argument():
    ORDER.append('argument')
    return 7
def default_value(value=[]):
    value.append(1)
    return value
def callback(value):
    CALLS.append(value)
    return value
def negative(value):
    return -value
@export
def probe():
    a = []
    b = a
    copied = [a].copy()
    copied[0].append(1)
    assert b == [1] and copied[0] is a and a is not []
    receiver().append(argument())
    assert ORDER == ['receiver', 'argument'] and VALUES == [7]
    assert default_value() is default_value()
    assert len(default_value()) == 3
    live = [1]
    seen = []
    for value in live:
        seen.append(value)
        if value == 1:
            live.append(2)
    assert seen == [1, 2]
    lazy = map(callback, [0, 1, 2])
    assert CALLS == []
    assert all(lazy) is False and CALLS == [0]
    assert list(lazy) == [1, 2] and list(lazy) == []
    zipped = zip([1], [2], strict=True)
    assert list(zipped) == [(1, 2)] and list(zipped) == []
    data = {'a': 1}
    view = data.keys()
    data['b'] = 2
    assert list(view) == ['a', 'b']
    assert sorted([1, 3, 2], key=negative, reverse=True) == [1, 2, 3]
    assert min([], default=9) == 9 and max([1, 2], key=negative) == 1
    assert sum([1, 2], start=4) == 7
    assert int('0x_ff', 0) == 255 and True + 2 == 3
    assert round(float('2.5')) == 2 and round(float('2.675'), 2) == float('2.67')
    assert round(123, -(10**50)) == 0
    for first in range(10**20):
        break
    assert first == 0
    assert 9007199254740993 > float('9007199254740992')
    assert 'abc'.find('', 4) == -1 and 'abc'.count('', 4) == 0
    assert [1, 2, 3][-(10**50):10**50] == [1, 2, 3]
    assert hashlib.sha256('ff') == hashlib.sha256_hex('ff')
    random.seed()
    chosen = random.choice([a])
    chosen.append(2)
    assert a == [1, 2] and chosen is a
    result.set(a)
    a.append(3)
    assert result.get() == [1, 2]
    progress.set(progress.get() + 1)
    return True
@export
def cyclic_result():
    progress.set(999)
    Attempt({'value': 999})
    value = []
    value.append(value)
    return value
@export
def changed_dictionary():
    progress.set(999)
    Attempt({'value': 999})
    values = {'a': 1}
    for key in values:
        values['b'] = 2
    return True
"""


async def compatibility_checks(runner, session):
    name = await deploy(runner, session, "runtime-compatibility", SOURCE)
    transactions = []
    async with runner.client(runner.founder_wallet, 0, session) as client:
        for function, progress, failure in [
            ("probe", 1, None),
            ("cyclic_result", 1, "cyclic"),
            ("changed_dictionary", 1, "size"),
            ("probe", 2, None),
        ]:
            submission = await client.send_tx(name, function, {}, chi=100_000, wait_for_tx=True)
            if failure:
                outcome = failed_execution(submission, function, fragment=failure)
                if outcome.get("events"):
                    raise AssertionError("Failed compatibility call retained events")
            else:
                successful(submission, function)
            await matching_receipts(runner, session, submission, success=failure is None)
            await agreement(session, runner.nodes, window=1)
            await uniform_state(
                runner, session, {f"{name}.result": [1, 2], f"{name}.progress": progress}
            )
            transactions.append(submission.tx_hash)
    return {
        "contract": name,
        "transactions": transactions,
        "consensus": await agreement(session, runner.nodes),
    }
