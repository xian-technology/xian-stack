ledger = Hash(default_value=0)
last_shadow = Variable()


@construct
def seed():
    last_shadow.set(0)


@export
def read_shadowed_now() -> int:
    now = 7
    return now


@export
def record_storage_shadow(account: str) -> int:
    ledger = {account: 41}
    value = ledger[account] + 1
    last_shadow.set(value)
    return value


@export
def accept_nested(nested: dict, to: str) -> str:
    return to
